"""Geräte-Werkzeuge: Hardware-Selbsttest (Labortest-Vorbereitung)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceSelftest, PairingStatus

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
