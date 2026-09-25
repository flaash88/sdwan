"""Backups & Firmware (Phase 9)."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import ConfigBackup, Device, FirmwareJob, FirmwareJobItem, PairingStatus
from app.routeros import RouterOSError
from app.services.backup import BackupError, make_diff, take_backup
from app.services.firmware import CHANNELS, FINAL, check_updates, plan_batches

router = APIRouter(tags=["backups", "firmware"])


def _b_out(b: ConfigBackup, with_content: bool = False) -> dict:
    out = {"id": str(b.id), "device_id": str(b.device_id), "trigger": b.trigger, "created_by": b.created_by, "routeros_version": b.routeros_version,
           "sha256": b.sha256, "size": b.size, "pinned": b.pinned, "note": b.note, "created_at": b.created_at,
           "added": b.diff.get("added"), "removed": b.diff.get("removed"), "previous_id": b.diff.get("previous_id")}
    if with_content:
        out["content"] = b.content
        out["diff"] = b.diff.get("lines", [])
    return out


# ----------------------------------------------------------------------------- Backups
@router.get("/devices/{device_id}/backups")
async def list_backups(device_id: uuid.UUID, ctx: Ctx = ReadCtx, limit: int = Query(default=100, le=500)) -> list[dict]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = (await ctx.db.execute(select(ConfigBackup).where(ConfigBackup.device_id == dev.id).order_by(ConfigBackup.created_at.desc()).limit(limit))).scalars()
    return [_b_out(b) for b in rows]


@router.post("/devices/{device_id}/backups", status_code=201)
async def create_backup(device_id: uuid.UUID, ctx: Ctx = TechCtx, note: str | None = None) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        b, _ = await take_backup(ctx.db, dev, "manual", note=note, created_by=ctx.user.email)
    except (BackupError, RouterOSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("backup.create", target_type="device", target_id=dev.id, details={"backup": str(b.id), "note": note})
    await ctx.db.commit()
    return _b_out(b)


@router.get("/backups/{backup_id}")
async def get_backup(backup_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    return _b_out(await get_or_404(ctx.db, ConfigBackup, backup_id, "Backup"), with_content=True)


@router.get("/backups/{backup_id}/diff")
async def diff_backup(backup_id: uuid.UUID, ctx: Ctx = ReadCtx, against: uuid.UUID | None = None, context: int = Query(default=3, ge=0, le=50)) -> dict:
    b = await get_or_404(ctx.db, ConfigBackup, backup_id, "Backup")
    other_id = against or (uuid.UUID(b.diff["previous_id"]) if b.diff.get("previous_id") else None)
    if other_id is None:
        return {"from": None, "to": str(b.id), "added": 0, "removed": 0, "lines": []}
    other = await get_or_404(ctx.db, ConfigBackup, other_id, "Backup")
    if other.device_id != b.device_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Backups verschiedener Geräte")
    old, new = (other, b) if other.created_at <= b.created_at else (b, other)
    return {"from": str(old.id), "to": str(new.id), **make_diff(old.content, new.content, context)}


@router.get("/backups/{backup_id}/download", response_class=PlainTextResponse)
async def download_backup(backup_id: uuid.UUID, ctx: Ctx = ReadCtx) -> PlainTextResponse:
    b = await get_or_404(ctx.db, ConfigBackup, backup_id, "Backup")
    dev = await ctx.db.get(Device, b.device_id)
    name = f"{dev.name if dev else b.device_id}-{b.created_at:%Y%m%d-%H%M}.rsc"
    await ctx.audit("backup.download", target_type="device", target_id=b.device_id, details={"backup": str(b.id)})
    await ctx.db.commit()
    return PlainTextResponse(b.content, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.patch("/backups/{backup_id}")
async def pin_backup(backup_id: uuid.UUID, ctx: Ctx = TechCtx, pinned: bool = True, note: str | None = None) -> dict:
    b = await get_or_404(ctx.db, ConfigBackup, backup_id, "Backup")
    b.pinned = pinned
    if note is not None:
        b.note = note
    await ctx.audit("backup.pin", target_type="device", target_id=b.device_id, details={"backup": str(b.id), "pinned": pinned})
    await ctx.db.commit()
    return _b_out(b)


@router.get("/backups")
async def backup_overview(ctx: Ctx = ReadCtx) -> list[dict]:
    """Letztes Backup je Gerät (Übersicht)."""
    sub = select(ConfigBackup.device_id, func.max(ConfigBackup.created_at).label("last")).group_by(ConfigBackup.device_id).subquery()
    rows = (await ctx.db.execute(select(ConfigBackup).join(sub, (ConfigBackup.device_id == sub.c.device_id) & (ConfigBackup.created_at == sub.c.last)))).scalars()
    last = {b.device_id: b for b in rows}
    counts = dict((await ctx.db.execute(select(ConfigBackup.device_id, func.count()).group_by(ConfigBackup.device_id))).all())
    devices = (await ctx.db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired).order_by(Device.name))).scalars()
    return [{"device_id": str(d.id), "device": d.name, "count": counts.get(d.id, 0), "last": _b_out(last[d.id]) if d.id in last else None} for d in devices]


# ----------------------------------------------------------------------------- Firmware
class JobIn(BaseModel):
    name: str = Field(default="RouterOS-Update", max_length=200)
    device_ids: list[uuid.UUID] = Field(min_length=1)
    channel: str = "stable"
    batch_size: int = Field(default=5, ge=1, le=100)
    batch_interval_s: int = Field(default=300, ge=0, le=86400)
    max_failures: int = Field(default=1, ge=0, le=1000)
    upgrade_routerboard: bool = False


def _job_out(j: FirmwareJob, items: list[FirmwareJobItem] | None = None, names: dict | None = None) -> dict:
    out = {"id": str(j.id), "name": j.name, "channel": j.channel, "batch_size": j.batch_size, "batch_interval_s": j.batch_interval_s,
           "max_failures": j.max_failures, "upgrade_routerboard": j.upgrade_routerboard, "status": j.status, "current_batch": j.current_batch,
           "next_batch_at": j.next_batch_at, "created_by": j.created_by, "created_at": j.created_at, "finished_at": j.finished_at, "last_error": j.last_error}
    if items is not None:
        out["items"] = [{"device_id": str(i.device_id), "device": (names or {}).get(i.device_id), "batch_no": i.batch_no, "status": i.status,
                         "from_version": i.from_version, "to_version": i.to_version, "error": i.error, "started_at": i.started_at, "finished_at": i.finished_at} for i in items]
        out["summary"] = {s: sum(1 for i in items if i.status == s) for s in ("queued", "updating", "rebooting", "success", "skipped", "failed", "cancelled")}
    return out


@router.post("/firmware/check")
async def firmware_check(ctx: Ctx = TechCtx, channel: str | None = None) -> list[dict]:
    """Prüft alle gepairten Geräte auf verfügbare Updates (parallel)."""
    if channel and channel not in CHANNELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"channel: {CHANNELS}")
    devices = (await ctx.db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired).order_by(Device.name))).scalars().all()
    sem = asyncio.Semaphore(20)

    async def one(d: Device) -> dict:
        async with sem:
            try:
                info = await check_updates(d, channel)
            except RouterOSError as exc:
                return {"device_id": str(d.id), "device": d.name, "error": str(exc)}
        d.facts = {**(d.facts or {}), "update": info}
        return {"device_id": str(d.id), "device": d.name, **info}

    res = await asyncio.gather(*(one(d) for d in devices))
    await ctx.db.commit()
    return list(res)


@router.get("/firmware/overview")
async def firmware_overview(ctx: Ctx = ReadCtx) -> list[dict]:
    devices = (await ctx.db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired).order_by(Device.name))).scalars()
    return [{"device_id": str(d.id), "device": d.name, "status": d.status.value, "model": d.model, "routeros_version": d.routeros_version,
             "update": (d.facts or {}).get("update")} for d in devices]


@router.post("/firmware/jobs", status_code=201)
async def create_job(data: JobIn, ctx: Ctx = TechCtx) -> dict:
    if data.channel not in CHANNELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"channel: {CHANNELS}")
    devices = (await ctx.db.execute(select(Device).where(Device.id.in_(data.device_ids)))).scalars().all()
    if len(devices) != len(set(data.device_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gerät nicht gefunden")
    tenants = {d.tenant_id for d in devices}
    job = FirmwareJob(tenant_id=tenants.pop() if len(tenants) == 1 else None, created_by=ctx.user.email,
                      **data.model_dump(exclude={"device_ids"}))
    ctx.db.add(job)
    await ctx.db.flush()
    order = sorted(devices, key=lambda d: (d.name,))
    for dev_id, batch in plan_batches([d.id for d in order], data.batch_size):
        dev = next(d for d in order if d.id == dev_id)
        ctx.db.add(FirmwareJobItem(tenant_id=dev.tenant_id, job_id=job.id, device_id=dev.id, batch_no=batch))
    await ctx.audit("firmware.job.create", tenant_id=job.tenant_id, target_type="firmware_job", target_id=job.id,
                    details={**data.model_dump(mode="json"), "batches": (len(devices) + data.batch_size - 1) // data.batch_size})
    await ctx.db.commit()
    return _job_out(job)


async def _job_visible(ctx: Ctx, job_id: uuid.UUID) -> FirmwareJob:
    job = await get_or_404(ctx.db, FirmwareJob, job_id, "Job")
    if ctx.tenant_id and job.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job nicht gefunden")
    return job


@router.get("/firmware/jobs")
async def list_jobs(ctx: Ctx = ReadCtx) -> list[dict]:
    q = select(FirmwareJob).order_by(FirmwareJob.created_at.desc()).limit(50)
    if ctx.tenant_id:
        q = q.where(FirmwareJob.tenant_id == ctx.tenant_id)
    jobs = (await ctx.db.execute(q)).scalars().all()
    out = []
    for j in jobs:
        items = (await ctx.db.execute(select(FirmwareJobItem).where(FirmwareJobItem.job_id == j.id))).scalars().all()
        o = _job_out(j, items)
        o.pop("items")
        out.append(o)
    return out


@router.get("/firmware/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    job = await _job_visible(ctx, job_id)
    items = (await ctx.db.execute(select(FirmwareJobItem).where(FirmwareJobItem.job_id == job.id).order_by(FirmwareJobItem.batch_no))).scalars().all()
    names = {d.id: d.name for d in (await ctx.db.execute(select(Device).where(Device.id.in_([i.device_id for i in items])))).scalars()}
    return _job_out(job, items, names)


@router.post("/firmware/jobs/{job_id}/{action}")
async def job_action(job_id: uuid.UUID, action: str, ctx: Ctx = TechCtx) -> dict:
    job = await _job_visible(ctx, job_id)
    items = (await ctx.db.execute(select(FirmwareJobItem).where(FirmwareJobItem.job_id == job.id))).scalars().all()
    if action == "pause" and job.status == "running":
        job.status = "paused"
    elif action == "resume" and job.status == "paused":
        failed = sum(1 for i in items if i.status == "failed")
        job.max_failures = job.max_failures + failed if job.max_failures else 0  # bekannte Fehler akzeptieren
        job.status, job.last_error, job.next_batch_at = "running", None, None
    elif action == "cancel" and job.status in ("running", "paused"):
        for i in items:
            if i.status == "queued":
                i.status = "cancelled"
        job.status, job.finished_at = "cancelled", utcnow()  # laufende Reboots werden nicht weiter verfolgt
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Aktion {action} im Status {job.status} nicht möglich")
    await ctx.audit(f"firmware.job.{action}", tenant_id=job.tenant_id, target_type="firmware_job", target_id=job.id)
    await ctx.db.commit()
    return _job_out(job)
