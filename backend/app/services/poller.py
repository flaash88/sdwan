"""Geräte-Polling über den Management-Tunnel: Erreichbarkeit + Systeminfos."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from sqlalchemy import select

from app import events
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, PairingStatus
from app.routeros import RouterOSError, connect_device
from app.services import registry

log = logging.getLogger(__name__)



async def poll_device(device: Device) -> dict[str, Any]:
    async with connect_device(device) as api:
        t0 = time.perf_counter()
        res = await api.resource()
        rtt_ms = round((time.perf_counter() - t0) * 1000, 1)
        ident = await api.call("/system/identity/print")
        result: dict[str, Any] = {"resource": res, "identity": ident[0].get("name") if ident else None, "mgmt_rtt_ms": rtt_ms}
        for hook in registry.poll_hooks():
            try:
                extra = await hook(device, api, res)
                if extra:
                    result.update(extra)
            except RouterOSError as exc:
                log.info("Poll-Hook %s für %s fehlgeschlagen: %s", getattr(hook, "__name__", hook), device.name, exc)
        return result


async def _finish_reboot(dev: Device) -> None:
    """Neustart-Markierung entfernen, sobald das Gerät nach dem Neustart wieder antwortet (Uptime kleiner als
    die seither vergangene Zeit) oder die Unterdrückungszeit abgelaufen ist."""
    from app.routeros.util import parse_duration

    info = (dev.facts or {}).get("reboot")
    if not info:
        return
    now = utcnow()
    try:
        at, until = dt.datetime.fromisoformat(info["at"]), dt.datetime.fromisoformat(info["until"])
    except (KeyError, ValueError):
        at = until = now
    up = parse_duration(dev.uptime) if getattr(dev, "_poll_ok", False) else None
    back = up is not None and up < (now - at).total_seconds() + 60 and (now - at).total_seconds() > 5
    if back or now >= until:
        facts = dict(dev.facts or {})
        facts.pop("reboot", None)
        dev.facts = facts
        await events.publish(dev.tenant_id, "device.reboot", {"id": str(dev.id), "name": dev.name, "state": "done" if back else "expired"})


async def poll_all(only: set[str] | None = None) -> None:
    """Pollt alle gepairten Geräte (oder nur ``only`` – Live-Modus)."""
    s = get_settings()
    async with system_session() as db:
        q = select(Device).where(Device.pairing_status == PairingStatus.paired)
        if only is not None:
            if not only:
                return
            import uuid as _uuid

            q = q.where(Device.id.in_([_uuid.UUID(x) for x in only]))
        devices = list((await db.execute(q)).scalars())
        sem = asyncio.Semaphore(50)

        async def one(dev: Device) -> None:
            async with sem:
                old = dev.status
                try:
                    data = await asyncio.wait_for(poll_device(dev), timeout=s.routeros_timeout * 3)
                    res = data["resource"]
                    dev.status = DeviceStatus.online
                    dev.last_seen_at = utcnow()
                    dev._poll_ok = True  # type: ignore[attr-defined]  # nur dieser Durchlauf liefert frische Werte
                    dev.uptime = str(res.get("uptime"))
                    dev.routeros_version = str(res.get("version", dev.routeros_version))
                    dev.model = str(res.get("board-name", dev.model))
                    dev.architecture = str(res.get("architecture-name", dev.architecture))
                    dev.identity = data.get("identity") or dev.identity
                    dev.facts = {
                        **(dev.facts or {}),
                        "cpu_load": res.get("cpu-load"),
                        "free_memory": res.get("free-memory"),
                        "total_memory": res.get("total-memory"),
                        "cpu_count": res.get("cpu-count"),
                        **{k: v for k, v in data.items() if k not in ("resource", "identity")},
                        "_poll_failures": 0,
                    }
                except (RouterOSError, TimeoutError, OSError) as exc:
                    log.info("Device %s (%s) nicht erreichbar: %s", dev.name, dev.tunnel_ip, exc)
                    dev._poll_ok = False  # type: ignore[attr-defined]
                    fails = int((dev.facts or {}).get("_poll_failures") or 0) + 1
                    dev.facts = {**(dev.facts or {}), "_poll_failures": fails}
                    grace = dt.timedelta(seconds=s.offline_after_seconds)
                    tunnel_down = dev.last_handshake_at is None or utcnow() - dev.last_handshake_at > dt.timedelta(seconds=200)
                    # Offline: 2 Fehlversuche in Folge, WireGuard-Tunnel ohne Handshake, oder Kulanzzeit überschritten
                    if fails >= 2 or tunnel_down or dev.last_seen_at is None or utcnow() - dev.last_seen_at > grace:
                        dev.status = DeviceStatus.offline
                await _finish_reboot(dev)
                if dev.status != old:
                    await events.publish(dev.tenant_id, "device.status", {"id": str(dev.id), "name": dev.name, "status": dev.status.value, "previous": old.value})
                    from app.services.state_log import record_state_change

                    await record_state_change(db, dev, old, dev.status)
                await events.publish(dev.tenant_id, "device.poll", {
                    "id": str(dev.id), "status": dev.status.value, "uptime": dev.uptime,
                    "cpu_load": (dev.facts or {}).get("cpu_load"), "last_seen_at": dev.last_seen_at,
                })

        await asyncio.gather(*(one(d) for d in devices))
        for post in registry.post_poll_hooks():
            try:
                await post(db, devices)
            except Exception:  # noqa: BLE001
                log.exception("Post-Poll-Hook %s fehlgeschlagen", getattr(post, "__name__", post))
        await db.commit()
