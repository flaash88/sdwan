"""Plattform-Betrieb für MSP-Admins (Phase 21): Sicherungsstatus, Sicherung anfordern, Plattform-Alarme."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.config import get_settings
from app.deps import Ctx, SuperCtx
from app.models import PlatformAlert, PlatformBackup
from app.services import platform_events

router = APIRouter(prefix="/platform", tags=["platform"])


def _out(b: PlatformBackup) -> dict[str, Any]:
    return {"id": str(b.id), "status": b.status, "trigger": b.trigger, "filename": b.filename, "size": b.size, "sha256": b.sha256,
            "targets": b.targets or {}, "contents": b.contents or {}, "error": b.error, "started_by": b.started_by,
            "created_at": b.created_at, "finished_at": b.finished_at}


@router.get("/backups")
async def backups(ctx: Ctx = SuperCtx) -> dict[str, Any]:
    s = get_settings()
    rows = (await ctx.db.execute(select(PlatformBackup).order_by(PlatformBackup.created_at.desc()).limit(30))).scalars().all()
    last_ok = next((r for r in rows if r.status == "ok"), None)
    return {"config": {"configured": bool(s.platform_backup_age_recipient.strip()), "hour_utc": s.platform_backup_hour_utc,
                       "directory": s.platform_backup_dir, "keep_days": s.platform_backup_keep_days,
                       "remote": s.platform_backup_rclone_remote or None, "influx": s.platform_backup_influx},
            "last_ok": _out(last_ok) if last_ok else None, "backups": [_out(r) for r in rows]}


@router.post("/backups", status_code=202)
async def request_backup(ctx: Ctx = SuperCtx) -> dict[str, Any]:
    """Sicherung anfordern – der Worker führt sie innerhalb einer Minute aus (nur er hat die Volumes)."""
    if (await ctx.db.execute(select(PlatformBackup.id).where(PlatformBackup.status.in_(("queued", "running"))).limit(1))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Es läuft bereits eine Sicherung")
    b = PlatformBackup(status="queued", trigger="manual", started_by=ctx.user.email)
    ctx.db.add(b)
    await ctx.db.flush()
    await ctx.audit("platform.backup.request", target_type="platform_backup", target_id=b.id)
    await ctx.db.commit()
    return _out(b)


@router.get("/alerts")
async def alerts(ctx: Ctx = SuperCtx) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(PlatformAlert).order_by(PlatformAlert.created_at.desc()).limit(50))).scalars().all()
    return [platform_events.out(a) for a in rows]
