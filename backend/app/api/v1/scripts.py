"""Script-Bibliothek und Massen-Ausführung (Phase 17)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import ROLE_RANK, Device, Role, Script, ScriptRun, ScriptRunItem, ScriptVersion
from app.services.scripts import VARIABLES, ScriptError, plan_batches, render_for, validate_content, warnings
from app.services.targets import resolve_targets

router = APIRouter(tags=["scripts"])


class ScriptIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    category: Literal["read", "change"] = "read"
    content: str = Field(min_length=1, max_length=20000)
    note: str | None = Field(default=None, max_length=500)


class TargetsIn(BaseModel):
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []


class RunIn(TargetsIn):
    script_id: uuid.UUID
    batch_size: int = Field(default=5, ge=1, le=100)
    max_failures: int = Field(default=1, ge=1, le=1000)
    confirm_name: str | None = None  # bei „ändernd“ Pflicht: exakter Script-Name


def _is_admin(ctx: Ctx) -> bool:
    return ROLE_RANK[ctx.role] >= ROLE_RANK[Role.admin]


def _out(s: Script) -> dict[str, Any]:
    return {"id": str(s.id), "name": s.name, "description": s.description, "category": s.category, "version": s.version,
            "content": s.content, "builtin": s.builtin, "scope": "global" if s.tenant_id is None else "tenant",
            "updated_at": s.updated_at, "warnings": warnings(s.content)}


def _check_write(ctx: Ctx, s: Script | None, category: str) -> None:
    if category == "change" and not _is_admin(ctx):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ändernde Scripts pflegen nur Admins")
    if s is not None:
        if s.builtin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Vordefiniertes Script – bitte kopieren")
        if s.tenant_id is None and not ctx.is_superuser:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Scripts pflegt nur der MSP")
        if s.category == "change" and not _is_admin(ctx):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Ändernde Scripts pflegen nur Admins")


def _validate(text: str) -> list[str]:
    try:
        return validate_content(text)
    except ScriptError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/scripts")
async def list_scripts(ctx: Ctx = ReadCtx) -> dict[str, Any]:
    rows = (await ctx.db.execute(select(Script).order_by(Script.name))).scalars().all()
    return {"scripts": [_out(s) for s in rows], "variables": list(VARIABLES)}


@router.post("/scripts", status_code=201)
async def create_script(data: ScriptIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    _check_write(ctx, None, data.category)
    _validate(data.content)
    s = Script(tenant_id=ctx.tenant_id, name=data.name, description=data.description, category=data.category, content=data.content,
               version=1, builtin=False, updated_at=utcnow())
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.db.add(ScriptVersion(script_id=s.id, version=1, content=s.content, category=s.category, note=data.note or "initial", created_by=ctx.user.email))
    await ctx.audit("script.create", target_type="script", target_id=s.id, details={"name": s.name, "category": s.category, "content": s.content})
    await ctx.db.commit()
    return _out(s)


@router.patch("/scripts/{script_id}")
async def update_script(script_id: uuid.UUID, data: ScriptIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    s = await get_or_404(ctx.db, Script, script_id, "Script")
    _check_write(ctx, s, data.category)
    _validate(data.content)
    changed = (data.content, data.category) != (s.content, s.category)
    s.name, s.description = data.name, data.description
    if changed:
        s.version += 1
        s.content, s.category = data.content, data.category
        ctx.db.add(ScriptVersion(script_id=s.id, version=s.version, content=s.content, category=s.category, note=data.note, created_by=ctx.user.email))
    s.updated_at = utcnow()
    await ctx.audit("script.update", target_type="script", target_id=s.id, details={"version": s.version, "content": s.content})
    await ctx.db.commit()
    return _out(s)


@router.delete("/scripts/{script_id}", status_code=204, response_model=None)
async def delete_script(script_id: uuid.UUID, ctx: Ctx = TechCtx) -> None:
    s = await get_or_404(ctx.db, Script, script_id, "Script")
    _check_write(ctx, s, s.category)
    await ctx.audit("script.delete", target_type="script", target_id=s.id, details={"name": s.name})
    await ctx.db.delete(s)
    await ctx.db.commit()


@router.post("/scripts/{script_id}/copy", status_code=201)
async def copy_script(script_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    src = await get_or_404(ctx.db, Script, script_id, "Script")
    _check_write(ctx, None, src.category)
    s = Script(tenant_id=ctx.tenant_id, name=f"{src.name} (Kopie)", description=src.description, category=src.category, content=src.content,
               version=1, builtin=False, updated_at=utcnow())
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.db.add(ScriptVersion(script_id=s.id, version=1, content=s.content, category=s.category, note=f"Kopie von {src.name}", created_by=ctx.user.email))
    await ctx.audit("script.copy", target_type="script", target_id=s.id, details={"from": str(src.id)})
    await ctx.db.commit()
    return _out(s)


@router.get("/scripts/{script_id}/versions")
async def versions(script_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    s = await get_or_404(ctx.db, Script, script_id, "Script")
    rows = (await ctx.db.execute(select(ScriptVersion).where(ScriptVersion.script_id == s.id).order_by(ScriptVersion.version.desc()))).scalars()
    return [{"version": v.version, "content": v.content, "category": v.category, "note": v.note, "created_by": v.created_by, "created_at": v.created_at}
            for v in rows]


async def _targets(ctx: Ctx, data: TargetsIn, script: Script) -> list[Device]:
    tenant = ctx.tenant_id if ctx.tenant_id else (script.tenant_id if script.tenant_id else None)
    devs = await resolve_targets(ctx.db, data.model_dump(), tenant)
    if not devs:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Keine Geräte im Ziel")
    return devs


@router.post("/scripts/{script_id}/preview")
async def preview(script_id: uuid.UUID, data: TargetsIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Gerenderte Befehle je Gerät (vor der Ausführung)."""
    s = await get_or_404(ctx.db, Script, script_id, "Script")
    out = []
    for d in await _targets(ctx, data, s):
        try:
            out.append({"device_id": str(d.id), "device": d.name, "status": d.status.value, "rendered": await render_for(ctx.db, s.content, d), "error": None})
        except ScriptError as exc:
            out.append({"device_id": str(d.id), "device": d.name, "status": d.status.value, "rendered": None, "error": str(exc)})
    return {"script": _out(s), "devices": out}


def _run_out(r: ScriptRun, items: list[tuple[ScriptRunItem, Device]] | None = None) -> dict[str, Any]:
    o: dict[str, Any] = {"id": str(r.id), "script_id": str(r.script_id) if r.script_id else None, "script_name": r.script_name,
                         "script_version": r.script_version, "category": r.category, "content": r.content, "status": r.status,
                         "batch_size": r.batch_size, "max_failures": r.max_failures, "current_batch": r.current_batch,
                         "created_by": r.created_by, "created_at": r.created_at, "finished_at": r.finished_at, "last_error": r.last_error}
    if items is not None:
        o["items"] = [{"id": str(i.id), "device_id": str(d.id), "device": d.name, "batch_no": i.batch_no, "status": i.status, "rendered": i.rendered,
                       "output": i.output, "error": i.error, "backup_id": str(i.backup_id) if i.backup_id else None,
                       "started_at": i.started_at, "finished_at": i.finished_at} for i, d in items]
        o["summary"] = {st: sum(1 for i, _ in items if i.status == st) for st in ("queued", "running", "success", "failed", "skipped", "cancelled")}
    return o


async def _items(ctx: Ctx, run: ScriptRun) -> list[tuple[ScriptRunItem, Device]]:
    rows = await ctx.db.execute(select(ScriptRunItem, Device).join(Device, Device.id == ScriptRunItem.device_id)
                                .where(ScriptRunItem.run_id == run.id).order_by(ScriptRunItem.batch_no, Device.name))
    return [(i, d) for i, d in rows.all()]


@router.post("/script-runs", status_code=201)
async def start_run(data: RunIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    s = await get_or_404(ctx.db, Script, data.script_id, "Script")
    if s.category == "change":
        if not _is_admin(ctx):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Ändernde Scripts dürfen nur Admins ausführen")
        if data.confirm_name != s.name:
            raise HTTPException(status.HTTP_409_CONFLICT, "Ändernde Scripts: zur Bestätigung den exakten Script-Namen angeben")
    devices = await _targets(ctx, data, s)
    rendered: dict[uuid.UUID, str] = {}
    for d in devices:
        try:
            rendered[d.id] = await render_for(ctx.db, s.content, d)
        except ScriptError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{d.name}: {exc}") from exc
    tenants = {d.tenant_id for d in devices}
    run = ScriptRun(tenant_id=tenants.pop() if len(tenants) == 1 else None, script_id=s.id, script_name=s.name, script_version=s.version,
                    category=s.category, content=s.content, targets={k: [str(x) for x in v] for k, v in data.model_dump(include={"device_ids", "site_ids", "tags"}).items()},
                    batch_size=data.batch_size, max_failures=data.max_failures, created_by=ctx.user.email)
    ctx.db.add(run)
    await ctx.db.flush()
    for dev_id, batch in plan_batches([d.id for d in devices], data.batch_size):
        dev = next(d for d in devices if d.id == dev_id)
        ctx.db.add(ScriptRunItem(tenant_id=dev.tenant_id, run_id=run.id, device_id=dev_id, batch_no=batch, rendered=rendered[dev_id]))
    # Audit mit vollem Script-Text
    await ctx.audit("script.run", target_type="script_run", target_id=run.id,
                    details={"script": s.name, "version": s.version, "category": s.category, "devices": len(devices), "content": s.content,
                             "warnings": warnings(s.content)})
    await ctx.db.commit()
    return _run_out(run, await _items(ctx, run))


@router.get("/script-runs")
async def list_runs(ctx: Ctx = ReadCtx, limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    q = select(ScriptRun).order_by(ScriptRun.created_at.desc()).limit(limit)
    if ctx.tenant_id:
        q = q.where(ScriptRun.tenant_id == ctx.tenant_id)
    return [_run_out(r) for r in (await ctx.db.execute(q)).scalars()]


@router.get("/script-runs/search")
async def search_outputs(ctx: Ctx = ReadCtx, q: str = Query(min_length=2, max_length=200), limit: int = Query(default=100, le=500)) -> list[dict[str, Any]]:
    rows = await ctx.db.execute(select(ScriptRunItem, ScriptRun, Device).join(ScriptRun, ScriptRun.id == ScriptRunItem.run_id)
                                .join(Device, Device.id == ScriptRunItem.device_id).where(ScriptRunItem.output.ilike(f"%{q}%"))
                                .order_by(ScriptRunItem.finished_at.desc()).limit(limit))
    out = []
    for i, r, d in rows.all():
        lines = [ln for ln in (i.output or "").splitlines() if q.lower() in ln.lower()][:5]
        out.append({"run_id": str(r.id), "script_name": r.script_name, "device_id": str(d.id), "device": d.name, "finished_at": i.finished_at, "lines": lines})
    return out


@router.get("/script-runs/{run_id}")
async def get_run(run_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    r = await get_or_404(ctx.db, ScriptRun, run_id, "Ausführung")
    if ctx.tenant_id and r.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ausführung nicht gefunden")
    return _run_out(r, await _items(ctx, r))


@router.post("/script-runs/{run_id}/{action}")
async def control(run_id: uuid.UUID, action: Literal["pause", "resume", "cancel"], ctx: Ctx = TechCtx) -> dict[str, Any]:
    r = await get_or_404(ctx.db, ScriptRun, run_id, "Ausführung")
    if ctx.tenant_id and r.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ausführung nicht gefunden")
    if r.category == "change" and not _is_admin(ctx) and action == "resume":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ändernde Scripts setzt nur ein Admin fort")
    if action == "pause" and r.status == "running":
        r.status = "paused"
    elif action == "resume" and r.status == "paused":
        r.status, r.max_failures = "running", max(r.max_failures, sum(1 for i, _ in await _items(ctx, r) if i.status == "failed") + 1)
    elif action == "cancel" and r.status in ("running", "paused"):
        r.status, r.finished_at = "cancelled", utcnow()
        for i, _ in await _items(ctx, r):
            if i.status == "queued":
                i.status = "cancelled"
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{action} im Status {r.status} nicht möglich")
    await ctx.audit(f"script.run.{action}", target_type="script_run", target_id=r.id, details={"script": r.script_name})
    await ctx.db.commit()
    return _run_out(r, await _items(ctx, r))
