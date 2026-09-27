"""Offboarding: Gerät sauber aus der Verwaltung nehmen; Archiv der Offboarding-Backups (90 Tage)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx
from app.models import Device, OffboardingArchive
from app.services import offboarding as ob

router = APIRouter(tags=["offboarding"])


class OffboardIn(BaseModel):
    mode: Literal["clean", "platform_only"] = "clean"
    confirm_name: str
    keep_local_access: bool = True  # Vor-Ort-Zugang behalten (Default)


@router.get("/devices/{device_id}/offboarding/preview")
async def offboarding_preview(device_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return await ob.preview(ctx.db, dev)


@router.post("/devices/{device_id}/offboard")
async def offboard(device_id: uuid.UUID, data: OffboardIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    """Admin/MSP-Admin; Bestätigung durch den exakten Gerätenamen. Bei Fehler vor Schritt 6 bleibt das Gerät bestehen."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if data.confirm_name != dev.name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Bestätigung: Gerätename stimmt nicht überein")
    try:
        res = await ob.offboard(ctx.db, dev, data.mode, ctx.user.email, keep_local_access=data.keep_local_access)
    except ob.OffboardError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await ctx.audit("device.offboard", target_type="device", target_id=dev.id, success=res["ok"],
                    details={"name": dev.name, "mode": data.mode, "keep_local_access": data.keep_local_access, "steps": res["steps"], "archive_id": res["archive_id"]})
    if not res["ok"]:
        await ctx.db.commit()  # Protokoll/Teilschritte (z. B. Backup) bleiben erhalten, Gerät bleibt bestehen
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "Offboarding abgebrochen – Gerät bleibt in der Plattform", "steps": res["steps"]})
    await ctx.db.delete(dev)
    await ctx.db.commit()
    return res


def _arch_out(a: OffboardingArchive) -> dict[str, Any]:
    return {"id": str(a.id), "device_name": a.device_name, "serial": a.serial, "model": a.model, "routeros_version": a.routeros_version,
            "mode": a.mode, "has_backup": a.content is not None, "backup_created_at": a.backup_created_at, "sha256": a.sha256,
            "steps": a.steps, "created_by": a.created_by, "created_at": a.created_at, "expires_at": a.expires_at}


@router.get("/offboarding-archives")
async def list_archives(ctx: Ctx = AdminCtx) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(OffboardingArchive).order_by(OffboardingArchive.created_at.desc()))).scalars().all()
    return [_arch_out(a) for a in rows]


@router.get("/offboarding-archives/{archive_id}/download")
async def download_archive(archive_id: uuid.UUID, ctx: Ctx = AdminCtx) -> Response:
    a = await get_or_404(ctx.db, OffboardingArchive, archive_id, "Archiv")
    if a.content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kein Backup im Archiv")
    await ctx.audit("offboarding.archive.download", target_type="offboarding_archive", target_id=a.id, details={"device": a.device_name})
    await ctx.db.commit()
    return Response(a.content, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="offboarding-{a.device_name}.rsc"'})
