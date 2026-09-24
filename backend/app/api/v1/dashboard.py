from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.deps import Ctx, ReadCtx
from app.models import Device, DeviceStatus, PairingStatus, Site

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def summary(ctx: Ctx = ReadCtx) -> dict:
    db = ctx.db
    by_status = dict((await db.execute(select(Device.status, func.count()).group_by(Device.status))).all())
    pending = await db.scalar(select(func.count()).select_from(Device).where(Device.pairing_status == PairingStatus.pending))
    sites = await db.scalar(select(func.count()).select_from(Site))
    total = sum(by_status.values())
    return {
        "devices_total": total,
        "devices_online": by_status.get(DeviceStatus.online, 0),
        "devices_offline": by_status.get(DeviceStatus.offline, 0),
        "devices_unknown": by_status.get(DeviceStatus.unknown, 0),
        "devices_pending": pending,
        "sites_total": sites,
    }
