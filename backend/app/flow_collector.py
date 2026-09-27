"""IPFIX-Collector (Phase 25): UDP auf der Hub-Tunnel-IP, speichert 5-Minuten-Aggregate in ``flow_aggregates``.

Start: ``python -m app.flow_collector`` (eigener Container mit ``network_mode: service:wireguard-hub``).
Datagramme werden sofort geparst und in einem Zähler summiert; gespeichert wird jede Minute. Unbekannte Quellen
(kein Gerät bzw. Export nicht aktiv) werden verworfen.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db import system_session, utcnow
from app.services.flows import Aggregator, DeviceMap, IpfixParser, store

log = logging.getLogger("flows")
FLUSH_S = 60.0


class Receiver(asyncio.DatagramProtocol):
    def __init__(self, dmap: DeviceMap) -> None:
        self.dmap = dmap
        self.parser = IpfixParser()
        self.agg = Aggregator()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        target = self.dmap.by_ip.get(addr[0])
        if target is None:
            return
        try:
            self.agg.add(target[0], utcnow(), self.parser.parse(addr[0], data))
        except Exception:  # noqa: BLE001 - kaputte Pakete ignorieren
            log.debug("IPFIX von %s nicht lesbar", addr[0])


async def flush(rx: Receiver) -> int:
    async with system_session() as db:
        if rx.dmap.loaded_at is None or (utcnow() - rx.dmap.loaded_at).total_seconds() > 60:
            await rx.dmap.refresh(db)
        n = await store(db, rx.dmap, rx.agg.take())
        await db.commit()
    return n


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    dmap = DeviceMap()
    async with system_session() as db:
        await dmap.refresh(db)
    rx = Receiver(dmap)
    await asyncio.get_running_loop().create_datagram_endpoint(lambda: rx, local_addr=("0.0.0.0", s.flow_port))
    log.info("IPFIX-Collector auf UDP %s", s.flow_port)
    while True:
        await asyncio.sleep(FLUSH_S)
        try:
            await flush(rx)
        except Exception:  # noqa: BLE001 - Collector darf nicht aussteigen
            log.exception("Flow-Aggregate speichern fehlgeschlagen")


if __name__ == "__main__":
    asyncio.run(main())
