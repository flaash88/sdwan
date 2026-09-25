from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, FirewallPolicy, PolicyAssignment, PolicyDeployment, PolicyVersion
from app.services.policy import PolicyError, device_policies, run_deployment, validate_content

router = APIRouter(tags=["policies"])


class PolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    content: dict[str, Any] = {}
    note: str | None = None


class PolicyPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    content: dict[str, Any] | None = None
    note: str | None = None


class Targets(BaseModel):
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []
    position: int = 100


class DeployIn(BaseModel):
    device_ids: list[uuid.UUID] | None = None  # None = alle zugewiesenen Geräte (im aktuellen Scope)
    atomic: bool = False


class RollbackIn(BaseModel):
    version: int
    deploy: bool = True
    atomic: bool = False


def _policy_out(p: FirewallPolicy, assigned: int | None = None) -> dict:
    return {
        "id": str(p.id), "name": p.name, "description": p.description, "version": p.version,
        "scope": "global" if p.tenant_id is None else "tenant", "tenant_id": str(p.tenant_id) if p.tenant_id else None,
        "content": p.content, "updated_at": p.updated_at, "created_at": p.created_at, "assigned_devices": assigned,
    }


def _can_edit(ctx: Ctx, p: FirewallPolicy) -> None:
    if p.tenant_id is None and not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Policies kann nur der MSP bearbeiten")


def _norm(content: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_content(content)
    except PolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/policies")
async def list_policies(ctx: Ctx = ReadCtx) -> list[dict]:
    pols = (await ctx.db.execute(select(FirewallPolicy).order_by(FirewallPolicy.name))).scalars().all()
    counts: dict[uuid.UUID, int] = {}
    for a in (await ctx.db.execute(select(PolicyAssignment))).scalars():
        counts[a.policy_id] = counts.get(a.policy_id, 0) + 1
    return [_policy_out(p, counts.get(p.id, 0)) for p in pols]


@router.post("/policies", status_code=201)
async def create_policy(data: PolicyIn, ctx: Ctx = TechCtx) -> dict:
    content = _norm(data.content)
    # MSP ohne gewählten Tenant -> globale Policy
    p = FirewallPolicy(tenant_id=ctx.tenant_id, name=data.name, description=data.description, content=content, version=1, updated_at=utcnow())
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.db.add(PolicyVersion(policy_id=p.id, version=1, content=content, note=data.note or "initial", created_by=ctx.user.email))
    await ctx.audit("policy.create", target_type="policy", target_id=p.id, details={"name": p.name, "scope": "global" if p.tenant_id is None else "tenant"})
    await ctx.db.commit()
    return _policy_out(p, 0)


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    assigns = (await ctx.db.execute(select(PolicyAssignment, Device).join(Device, Device.id == PolicyAssignment.device_id)
                                    .where(PolicyAssignment.policy_id == p.id).order_by(Device.name))).all()
    out = _policy_out(p, len(assigns))
    out["assignments"] = [
        {"device_id": str(d.id), "device": d.name, "position": a.position, "status": a.status,
         "deployed_version": a.deployed_version, "deployed_at": a.deployed_at, "last_error": a.last_error}
        for a, d in assigns
    ]
    return out


@router.patch("/policies/{policy_id}")
async def update_policy(policy_id: uuid.UUID, data: PolicyPatch, ctx: Ctx = TechCtx) -> dict:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    _can_edit(ctx, p)
    if data.name is not None:
        p.name = data.name
    if data.description is not None:
        p.description = data.description
    if data.content is not None:
        content = _norm(data.content)
        if content != p.content:
            p.version += 1
            p.content = content
            ctx.db.add(PolicyVersion(policy_id=p.id, version=p.version, content=content, note=data.note, created_by=ctx.user.email))
    p.updated_at = utcnow()
    await ctx.audit("policy.update", target_type="policy", target_id=p.id, details={"version": p.version, "note": data.note})
    await ctx.db.commit()
    return _policy_out(p)


@router.delete("/policies/{policy_id}", status_code=204, response_model=None)
async def delete_policy(policy_id: uuid.UUID, ctx: Ctx = TechCtx) -> None:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    _can_edit(ctx, p)
    if (await ctx.db.execute(select(PolicyAssignment).where(PolicyAssignment.policy_id == p.id).limit(1))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Policy ist noch Geräten zugewiesen")
    await ctx.audit("policy.delete", target_type="policy", target_id=p.id, details={"name": p.name})
    await ctx.db.delete(p)
    await ctx.db.commit()


@router.get("/policies/{policy_id}/versions")
async def versions(policy_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict]:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    rows = (await ctx.db.execute(select(PolicyVersion).where(PolicyVersion.policy_id == p.id).order_by(PolicyVersion.version.desc()))).scalars()
    return [{"version": v.version, "content": v.content, "note": v.note, "created_by": v.created_by, "created_at": v.created_at} for v in rows]


@router.post("/policies/{policy_id}/assign")
async def assign(policy_id: uuid.UUID, data: Targets, ctx: Ctx = TechCtx) -> dict:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    q = select(Device)
    conds = []
    if data.device_ids:
        conds.append(Device.id.in_(data.device_ids))
    if data.site_ids:
        conds.append(Device.site_id.in_(data.site_ids))
    devices = list((await ctx.db.execute(q.where(or_(*conds)))).scalars()) if conds else []
    if data.tags:
        devices += [d for d in (await ctx.db.execute(select(Device))).scalars() if set(d.tags or []) & set(data.tags) and d not in devices]
    if p.tenant_id is not None:
        devices = [d for d in devices if d.tenant_id == p.tenant_id]
    existing = {a.device_id for a in (await ctx.db.execute(select(PolicyAssignment).where(PolicyAssignment.policy_id == p.id))).scalars()}
    added = []
    for d in devices:
        if d.id not in existing:
            ctx.db.add(PolicyAssignment(tenant_id=d.tenant_id, policy_id=p.id, device_id=d.id, position=data.position))
            added.append(str(d.id))
    await ctx.audit("policy.assign", target_type="policy", target_id=p.id, details={"devices": added})
    await ctx.db.commit()
    return {"assigned": added}


@router.delete("/policies/{policy_id}/assign/{device_id}")
async def unassign(policy_id: uuid.UUID, device_id: uuid.UUID, bg: BackgroundTasks, push: bool = True, ctx: Ctx = TechCtx) -> dict:
    a = (await ctx.db.execute(select(PolicyAssignment).where(PolicyAssignment.policy_id == policy_id, PolicyAssignment.device_id == device_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Zuweisung nicht gefunden")
    tenant_id = a.tenant_id
    await ctx.db.delete(a)
    await ctx.audit("policy.unassign", tenant_id=tenant_id, target_type="policy", target_id=policy_id, details={"device_id": str(device_id)})
    dep = None
    if push:
        # Gerät neu pushen -> Regeln dieser Policy werden entfernt
        dep = PolicyDeployment(tenant_id=tenant_id, policy_id=None, started_by=ctx.user.email)
        ctx.db.add(dep)
    await ctx.db.commit()
    if dep:
        bg.add_task(run_deployment, dep.id, [device_id])
    return {"deployment_id": str(dep.id) if dep else None}


async def _start(ctx: Ctx, bg: BackgroundTasks, p: FirewallPolicy, device_ids: list[uuid.UUID] | None, atomic: bool) -> PolicyDeployment:
    assigns = (await ctx.db.execute(select(PolicyAssignment).where(PolicyAssignment.policy_id == p.id))).scalars().all()
    targets = [a.device_id for a in assigns if device_ids is None or a.device_id in device_ids]
    if not targets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keine zugewiesenen Geräte im Ziel")
    tenants = {a.tenant_id for a in assigns if a.device_id in targets}
    dep = PolicyDeployment(tenant_id=tenants.pop() if len(tenants) == 1 else None, policy_id=p.id, policy_version=p.version,
                           atomic=atomic, started_by=ctx.user.email)
    ctx.db.add(dep)
    await ctx.db.flush()
    await ctx.audit("policy.deploy", target_type="policy", target_id=p.id,
                    details={"deployment": str(dep.id), "version": p.version, "devices": [str(t) for t in targets], "atomic": atomic})
    await ctx.db.commit()
    bg.add_task(run_deployment, dep.id, targets)
    return dep


@router.post("/policies/{policy_id}/deploy", status_code=202)
async def deploy(policy_id: uuid.UUID, data: DeployIn, bg: BackgroundTasks, ctx: Ctx = TechCtx) -> dict:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    dep = await _start(ctx, bg, p, data.device_ids, data.atomic)
    return {"deployment_id": str(dep.id), "status": dep.status}


@router.post("/policies/{policy_id}/rollback", status_code=202)
async def rollback(policy_id: uuid.UUID, data: RollbackIn, bg: BackgroundTasks, ctx: Ctx = TechCtx) -> dict:
    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    _can_edit(ctx, p)
    old = (await ctx.db.execute(select(PolicyVersion).where(PolicyVersion.policy_id == p.id, PolicyVersion.version == data.version))).scalar_one_or_none()
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version nicht gefunden")
    p.version += 1
    p.content = old.content
    p.updated_at = utcnow()
    ctx.db.add(PolicyVersion(policy_id=p.id, version=p.version, content=old.content, note=f"Rollback auf v{data.version}", created_by=ctx.user.email))
    await ctx.audit("policy.rollback", target_type="policy", target_id=p.id, details={"to_version": data.version, "new_version": p.version})
    await ctx.db.flush()
    if data.deploy:
        dep = await _start(ctx, bg, p, None, data.atomic)
        return {"version": p.version, "deployment_id": str(dep.id)}
    await ctx.db.commit()
    return {"version": p.version, "deployment_id": None}


@router.get("/deployments")
async def list_deployments(ctx: Ctx = ReadCtx, policy_id: uuid.UUID | None = None, limit: int = Query(default=50, le=500)) -> list[dict]:
    q = select(PolicyDeployment).order_by(PolicyDeployment.created_at.desc()).limit(limit)
    if ctx.tenant_id:
        q = q.where(PolicyDeployment.tenant_id == ctx.tenant_id)
    if policy_id:
        q = q.where(PolicyDeployment.policy_id == policy_id)
    return [_dep_out(d) for d in (await ctx.db.execute(q)).scalars()]


@router.get("/deployments/{deployment_id}")
async def get_deployment(deployment_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    d = await get_or_404(ctx.db, PolicyDeployment, deployment_id, "Deployment")
    if ctx.tenant_id and d.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deployment nicht gefunden")
    return _dep_out(d)


def _dep_out(d: PolicyDeployment) -> dict:
    return {"id": str(d.id), "policy_id": str(d.policy_id) if d.policy_id else None, "policy_version": d.policy_version,
            "atomic": d.atomic, "status": d.status, "started_by": d.started_by, "results": d.results,
            "created_at": d.created_at, "finished_at": d.finished_at}


@router.get("/devices/{device_id}/policies")
async def device_policy_list(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return [
        {"policy_id": str(p.id), "name": p.name, "scope": "global" if p.tenant_id is None else "tenant", "version": p.version,
         "deployed_version": a.deployed_version, "status": a.status, "last_error": a.last_error, "deployed_at": a.deployed_at, "position": a.position}
        for a, p in await device_policies(ctx.db, dev.id)
    ]


# ----------------------------------------------------------------------------- Bestehende Router-Regeln
class ImportIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sections: list[str] = ["filter", "nat", "address_lists"]
    # True: Policy dem Gerät zuweisen, pushen und die Original-Regeln danach entfernen
    replace: bool = False


@router.get("/devices/{device_id}/firewall")
async def device_firewall(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    """Aktuelle Firewall des Routers (live): Filter, NAT, Address-Lists – verwaltet oder manuell."""
    from app.routeros import RouterOSError, connect_device
    from app.services.policy import read_router_firewall

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        async with connect_device(dev) as api:
            fw = await read_router_firewall(api)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Router nicht erreichbar: {exc}") from exc
    return fw


@router.post("/devices/{device_id}/firewall/import", status_code=201)
async def import_firewall(device_id: uuid.UUID, data: ImportIn, ctx: Ctx = TechCtx) -> dict:
    """Übernimmt die manuellen Regeln des Routers als zentrale Policy (optional inkl. Ersetzen)."""
    from app.routeros import RouterOSError, connect_device
    from app.services.policy import PATHS, import_rules, read_router_firewall

    bad = set(data.sections) - {"filter", "nat", "address_lists"}
    if bad or not data.sections:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sections: filter | nat | address_lists")
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        async with connect_device(dev) as api:
            fw = await read_router_firewall(api)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Router nicht erreichbar: {exc}") from exc
    content, warnings, ids = import_rules(fw, data.sections)
    if not any(content.values()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keine übertragbaren manuellen Regeln gefunden")

    p = FirewallPolicy(tenant_id=dev.tenant_id, name=data.name, description=f"Übernommen von {dev.name}",
                       content=content, version=1, updated_at=utcnow())
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.db.add(PolicyVersion(policy_id=p.id, version=1, content=content, note=f"Import von {dev.name}", created_by=ctx.user.email))
    result: dict[str, Any] = {"policy_id": str(p.id), "counts": {k: len(v) for k, v in content.items()}, "warnings": warnings, "replaced": False}

    if data.replace:
        from app.services.backup import BackupError, take_backup
        from app.services.policy import run_deployment

        try:
            await take_backup(ctx.db, dev, "manual", note=f"vor Firewall-Übernahme '{data.name}'", created_by=ctx.user.email)
        except (BackupError, RouterOSError):
            pass
        ctx.db.add(PolicyAssignment(tenant_id=dev.tenant_id, policy_id=p.id, device_id=dev.id, position=1000))
        removed_al: list[dict[str, Any]] = []
        try:
            async with connect_device(dev) as api:
                # Address-Lists vorher entfernen (sonst Duplikate), bei Fehler wiederherstellen
                for r in fw["address_lists"]:
                    if str(r[".id"]) in ids["address_lists"]:
                        await api.remove(PATHS["address_lists"], r[".id"])
                        removed_al.append({k: r[k] for k in ("list", "address", "comment", "disabled") if r.get(k) not in (None, "")})
        except RouterOSError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        dep = PolicyDeployment(tenant_id=dev.tenant_id, policy_id=p.id, policy_version=1, started_by=ctx.user.email)
        ctx.db.add(dep)
        await ctx.db.commit()
        await run_deployment(dep.id, [dev.id])
        await ctx.db.refresh(dep)
        async with connect_device(dev) as api:
            if dep.status == "success":
                for key in ("filter", "nat"):
                    current = {str(r[".id"]) for r in await api.print(PATHS[key])}
                    for rid in ids[key]:
                        if rid in current:
                            await api.remove(PATHS[key], rid)
                result["replaced"] = True
            else:
                for e in removed_al:
                    try:
                        await api.add(PATHS["address_lists"], **e)
                    except RouterOSError:
                        pass
        result["deployment_id"] = str(dep.id)
        result["deployment_status"] = dep.status
    await ctx.audit("policy.import", target_type="device", target_id=dev.id,
                    details={"policy": str(p.id), "counts": result["counts"], "replace": data.replace, "replaced": result["replaced"],
                             "warnings": warnings[:20]})
    await ctx.db.commit()
    return result
