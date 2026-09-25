"""Geräte-Werkzeuge: Hardware-Selbsttest (Labortest-Vorbereitung), Neustart, IP-Adressen."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app import events
from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceSelftest, DeviceStatus, PairingStatus

router = APIRouter(prefix="/devices", tags=["devices"])


def _selftest_out(t: DeviceSelftest | None) -> dict | None:
    if t is None:
        return None
    return {"status": t.status, "ran_at": t.ran_at, "ran_by": t.ran_by, "duration_ms": t.duration_ms, **t.result}


@router.get("/{device_id}/selftest")
async def get_selftest(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict | None:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    t = (await ctx.db.execute(select(DeviceSelftest).where(DeviceSelftest.device_id == dev.id))).scalar_one_or_none()
    return _selftest_out(t)


@router.post("/{device_id}/selftest")
async def run_selftest_endpoint(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict | None:
    """Selbsttest ausführen – rein lesend, ändert nichts am Router."""
    from app.services.selftest import run_selftest

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if dev.pairing_status != PairingStatus.paired:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gerät ist nicht verbunden")
    res = await run_selftest(dev)
    t = (await ctx.db.execute(select(DeviceSelftest).where(DeviceSelftest.device_id == dev.id))).scalar_one_or_none()
    if t is None:
        t = DeviceSelftest(tenant_id=dev.tenant_id, device_id=dev.id)
        ctx.db.add(t)
    t.status, t.duration_ms, t.ran_at, t.ran_by = res["status"], res["duration_ms"], utcnow(), ctx.user.email
    t.result = {k: v for k, v in res.items() if k not in ("status", "duration_ms")}
    await ctx.audit("device.selftest", target_type="device", target_id=dev.id, success=res["status"] != "error",
                    details={"status": res["status"], "summary": res.get("summary")})
    await ctx.db.commit()
    return _selftest_out(t)


# Verbindungsabbruch direkt nach /system/reboot ist erwartet (der Router trennt die API-Sitzung)
_DISCONNECT = ("closed", "reset", "eof", "broken pipe", "timed out", "timeout", "connection")


@router.post("/{device_id}/reboot")
async def reboot(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    """Router neu starten. Der Offline-Alarm wird für 5 Minuten unterdrückt (siehe alerts.reboot_suppressed)."""
    from app.routeros import RouterOSError, connect_device
    from app.services.alerts import REBOOT_SUPPRESS

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if dev.pairing_status != PairingStatus.paired:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gerät ist nicht verbunden")
    try:
        async with connect_device(dev) as api:
            await api.call("/system/reboot")
    except RouterOSError as exc:
        if not any(k in str(exc).lower() for k in _DISCONNECT):
            await ctx.audit("device.reboot", target_type="device", target_id=dev.id, success=False, details={"error": str(exc)})
            await ctx.db.commit()
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Neustart fehlgeschlagen: {exc}") from exc
    now = utcnow()
    info = {"at": now.isoformat(), "by": ctx.user.email, "until": (now + REBOOT_SUPPRESS).isoformat()}
    dev.facts = {**(dev.facts or {}), "reboot": info}
    await ctx.audit("device.reboot", target_type="device", target_id=dev.id, details={"suppress_offline_until": info["until"]})
    await ctx.db.commit()
    await events.publish(dev.tenant_id, "device.reboot", {"id": str(dev.id), "name": dev.name, "state": "rebooting", **info})
    return {"ok": True, **info}


@router.get("/{device_id}/addresses")
async def addresses(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    """IP-Adressen je Interface – live vom Router, sonst der Stand der letzten Abfrage."""
    from app.routeros import RouterOSError, connect_device
    from app.services.device_info import read_addresses

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    cached = (dev.facts or {}).get("addresses")
    if dev.pairing_status == PairingStatus.paired and dev.status != DeviceStatus.offline:
        try:
            async with connect_device(dev) as api:
                return {"source": "live", "at": utcnow(), "addresses": await read_addresses(api)}
        except RouterOSError:
            pass
    return {"source": "cache" if cached is not None else "none", "at": dev.last_seen_at, "addresses": cached or []}
