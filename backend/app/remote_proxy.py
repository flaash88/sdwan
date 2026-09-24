"""TCP-Proxy für Remote-Access-Sessions (eigener Dienst ``remote-proxy``).

Start: ``python -m app.remote_proxy``

Gleicht alle 2 s die aktiven Sessions aus der DB ab, öffnet pro Session einen Listener auf dem
zugewiesenen Port und leitet Verbindungen ausschließlich an die Tunnel-IP des Geräts weiter.
Quell-IP-Prüfung, Ablaufzeit und Verbindungs-Audit werden hier durchgesetzt.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import signal
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from app.audit import audit
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, RemoteSession
from app.routeros.client import assert_tunnel_address

log = logging.getLogger("remote-proxy")


@dataclass
class Listener:
    session_id: uuid.UUID
    server: asyncio.Server
    expires_at: float
    conns: set[asyncio.Task] = field(default_factory=set)


class ProxyManager:
    def __init__(self, bind_host: str = "0.0.0.0") -> None:
        self.bind_host = bind_host
        self.listeners: dict[uuid.UUID, Listener] = {}

    async def reconcile(self) -> None:
        async with system_session() as db:
            rows = (await db.execute(select(RemoteSession, Device).join(Device, Device.id == RemoteSession.device_id)
                                     .where(RemoteSession.status == "active"))).all()
        active = {s.id: (s, d) for s, d in rows if s.expires_at > utcnow()}
        for sid in list(self.listeners):
            if sid not in active:
                await self.stop(sid)
        for sid, (sess, dev) in active.items():
            if sid not in self.listeners:
                await self.start(sess, dev)

    async def start(self, sess: RemoteSession, dev: Device) -> None:
        s = get_settings()
        target = dev.tunnel_ip
        if s.remote_proxy_target_override and s.routeros_backend == "simulator":
            target = s.remote_proxy_target_override
        else:
            assert_tunnel_address(target)  # niemals ins öffentliche Internet verbinden
        net = ipaddress.ip_network(sess.allowed_cidr, strict=False)
        sid, port, tport, tenant_id, user = sess.id, sess.listen_port, sess.target_port, sess.tenant_id, sess.user_email

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            peer = writer.get_extra_info("peername")[0]
            lst = self.listeners.get(sid)
            task = asyncio.current_task()
            if lst and task:
                lst.conns.add(task)
            try:
                if ipaddress.ip_address(peer) not in net:
                    await self._audit(tenant_id, dev.id, "remote.denied", sid, user, {"source": peer, "allowed": str(net)}, False)
                    return
                await self._pipe(reader, writer, target, tport, sid, tenant_id, dev.id, user, peer)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                if lst and task:
                    lst.conns.discard(task)

        try:
            server = await asyncio.start_server(handle, self.bind_host, port)
        except OSError as exc:
            log.error("Port %s für Session %s nicht verfügbar: %s", port, sid, exc)
            return
        self.listeners[sid] = Listener(sid, server, sess.expires_at.timestamp())
        log.info("Session %s: %s:%s -> %s:%s", sid, self.bind_host, port, target, tport)

    async def stop(self, sid: uuid.UUID) -> None:
        lst = self.listeners.pop(sid, None)
        if lst is None:
            return
        lst.server.close()
        for t in list(lst.conns):
            t.cancel()
        with contextlib.suppress(Exception):
            await lst.server.wait_closed()
        log.info("Session %s beendet", sid)

    async def _pipe(self, reader, writer, host, port, sid, tenant_id, device_id, user, peer) -> None:  # noqa: ANN001
        started = time.time()
        counters = {"in": 0, "out": 0}
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
        except (OSError, TimeoutError) as exc:
            await self._audit(tenant_id, device_id, "remote.connect_failed", sid, user, {"source": peer, "error": str(exc)}, False)
            return
        await self._audit(tenant_id, device_id, "remote.connect", sid, user, {"source": peer}, True)

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> None:
            try:
                while data := await src.read(65536):
                    counters[key] += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        try:
            await asyncio.gather(copy(reader, up_w, "in"), copy(up_r, writer, "out"))
        finally:
            dur = round(time.time() - started, 1)
            async with system_session() as db:
                sess = await db.get(RemoteSession, sid)
                if sess:
                    sess.connections += 1
                    sess.bytes_in += counters["in"]
                    sess.bytes_out += counters["out"]
                await audit(db, "remote.disconnect", tenant_id=tenant_id, target_type="device", target_id=device_id,
                            details={"session": str(sid), "user": user, "source": peer, "duration_s": dur, "bytes_in": counters["in"], "bytes_out": counters["out"]})
                await db.commit()

    async def _audit(self, tenant_id, device_id, action, sid, user, details, ok) -> None:  # noqa: ANN001
        async with system_session() as db:
            await audit(db, action, tenant_id=tenant_id, target_type="device", target_id=device_id,
                        details={"session": str(sid), "user": user, **details}, success=ok)
            await db.commit()

    async def close_all(self) -> None:
        for sid in list(self.listeners):
            await self.stop(sid)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mgr = ProxyManager()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("Remote-Proxy gestartet, Port-Pool %s", get_settings().remote_proxy_port_range)
    while not stop.is_set():
        try:
            await mgr.reconcile()
        except Exception:  # noqa: BLE001
            log.exception("Reconcile fehlgeschlagen")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=2)
    await mgr.close_all()


if __name__ == "__main__":
    asyncio.run(main())
