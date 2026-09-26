"""Syslog-Empfänger (Phase 18): UDP auf der Hub-Tunnel-IP, schreibt in ``syslog_messages``.

Start: ``python -m app.syslog_receiver`` (eigener Container mit ``network_mode: service:wireguard-hub``).
Nachrichten werden gepuffert und alle 2 s gespeichert; die Zuordnung Quell-IP -> Gerät wird jede Minute erneuert.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app.config import get_settings
from app.db import system_session, utcnow
from app.services.syslog import DeviceMap, store

log = logging.getLogger("syslog")
FLUSH_S = 2.0
MAX_BUFFER = 20000


class Receiver(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.buffer: list[tuple[str, bytes, dt.datetime]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        if len(self.buffer) < MAX_BUFFER:  # Überlast: verwerfen statt Speicher zu sprengen
            self.buffer.append((addr[0], data[:4096], utcnow()))


async def flush(rx: Receiver, dmap: DeviceMap) -> int:
    batch, rx.buffer = rx.buffer, []
    if not batch:
        return 0
    async with system_session() as db:
        if dmap.loaded_at is None or (utcnow() - dmap.loaded_at).total_seconds() > 60:
            await dmap.refresh(db)
        n = await store(db, dmap, batch)
        await db.commit()
    return n


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    loop = asyncio.get_running_loop()
    rx = Receiver()
    await loop.create_datagram_endpoint(lambda: rx, local_addr=("0.0.0.0", s.syslog_port))
    log.info("Syslog-Empfänger auf UDP %s", s.syslog_port)
    dmap = DeviceMap()
    while True:
        await asyncio.sleep(FLUSH_S)
        try:
            await flush(rx, dmap)
        except Exception:  # noqa: BLE001 - Empfänger darf nicht aussteigen
            log.exception("Syslog speichern fehlgeschlagen")


if __name__ == "__main__":
    asyncio.run(main())
