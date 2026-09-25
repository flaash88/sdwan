from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.deps import Ctx, ReadCtx
from app.models import Device, DeviceStatus, PairingStatus, Site, VrrpInstance, WanLink

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


@router.get("/fleet-state")
async def fleet_state(ctx: Ctx = ReadCtx) -> dict:
    """Je Gerät: aktiver WAN, VRRP-Rolle und ob der Standort über Backup läuft.

    Backup = im Failover trägt ein WAN mit schlechterer Priorität die Default-Route (wie Alarm
    ``wan_backup_active``) ODER eine VRRP-Instanz ist Master (wie Alarm ``vrrp_master``).
    """
    db = ctx.db
    devices = (await db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired))).scalars().all()
    links = (await db.execute(select(WanLink).order_by(WanLink.slot))).scalars().all()
    insts = (await db.execute(select(VrrpInstance).order_by(VrrpInstance.name))).scalars().all()
    by_dev: dict = {}
    for lk in links:
        by_dev.setdefault(lk.device_id, {"links": [], "vrrp": []})["links"].append(lk)
    for i in insts:
        by_dev.setdefault(i.device_id, {"links": [], "vrrp": []})["vrrp"].append(i)
    out: dict[str, dict] = {}
    backup_sites: set = set()
    for d in devices:
        x = by_dev.get(d.id, {"links": [], "vrrp": []})
        enabled = [lk for lk in x["links"] if lk.enabled]
        best = min((lk.priority for lk in enabled), default=1)
        act = next((lk for lk in sorted(enabled, key=lambda lk: (lk.priority, lk.slot)) if lk.active), None)
        wan_backup = bool(act and d.wan_mode == "failover" and act.priority > best)
        masters = [i for i in x["vrrp"] if i.enabled and i.state == "master"]
        since = [t for t in ([act.active_since] if wan_backup and act else []) + [i.last_change_at for i in masters] if t]
        roles = {i.state for i in x["vrrp"] if i.enabled}
        on_backup = wan_backup or bool(masters)
        if on_backup:
            backup_sites.add(d.site_id or d.id)
        out[str(d.id)] = {
            "wan_mode": d.wan_mode,
            "wan_links": len(enabled),
            "active_wan": {"slot": act.slot, "name": act.name, "interface": act.interface, "backup": wan_backup,
                           "latency_ms": act.last_latency_ms} if act else None,
            "vrrp_role": "master" if masters else ("backup" if "backup" in roles else (next(iter(roles)) if roles else None)),
            "vrrp": [{"id": str(i.id), "name": i.name, "vrid": i.vrid, "state": i.state, "enabled": i.enabled} for i in x["vrrp"]],
            "on_backup": on_backup,
            "backup_since": min(since).isoformat() if since else None,
        }
    sites_total = await db.scalar(select(func.count()).select_from(Site))
    return {"devices": out, "sites_on_backup": len(backup_sites), "sites_total": sites_total}
