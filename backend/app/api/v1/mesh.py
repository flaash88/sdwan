from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, Site, Tenant, VpnPeer
from app.services.mesh import MeshError, apply_mesh, build_plan

router = APIRouter(prefix="/mesh", tags=["mesh"])


class MeshSettings(BaseModel):
    topology: str
    auto_apply: bool = True


@router.get("")
async def get_mesh(ctx: Ctx = ReadCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    try:
        plan = await build_plan(ctx.db, tenant)
        plan_error = None
    except MeshError as exc:
        plan, plan_error = None, str(exc)
    devices = {d.id: d for d in (await ctx.db.execute(select(Device))).scalars()}
    sites = {s.id: s for s in (await ctx.db.execute(select(Site))).scalars()}
    participants = {n.id for n in plan.nodes} if plan else set()
    nodes = [
        {
            "device_id": str(d.id),
            "name": d.name,
            "site": sites[d.site_id].name if d.site_id in sites else None,
            "site_id": str(d.site_id) if d.site_id else None,
            "is_hub": bool(d.site_id in sites and sites[d.site_id].is_mesh_hub),
            "lan_subnets": sites[d.site_id].lan_subnets if d.site_id in sites else [],
            "mesh_ip": d.mesh_ip,
            "mesh_endpoint": d.mesh_endpoint,
            "status": d.status.value,
            "participating": d.id in participants,
        }
        for d in devices.values()
        if d.id in participants or d.mesh_ip
    ]
    links = [
        {
            "id": str(p.id),
            "a": str(p.device_a_id),
            "b": str(p.device_b_id),
            "a_name": devices[p.device_a_id].name if p.device_a_id in devices else "?",
            "b_name": devices[p.device_b_id].name if p.device_b_id in devices else "?",
            "kind": p.kind,
            "status": p.status,
            "last_handshake_at": p.last_handshake_at,
            "rx_bytes": p.rx_bytes,
            "tx_bytes": p.tx_bytes,
            "last_error": p.last_error,
        }
        for p in (await ctx.db.execute(select(VpnPeer))).scalars()
    ]
    return {
        "topology": tenant.mesh_topology,
        "auto_apply": (tenant.settings or {}).get("mesh_auto", True),
        "subnet": tenant.mesh_subnet,
        "plan_error": plan_error,
        "planned_links": len(plan.links) if plan else 0,
        "warnings": plan.warnings if plan else [],
        "nodes": nodes,
        "links": links,
        "last_apply": (tenant.settings or {}).get("mesh_last_apply"),
    }


@router.put("/settings")
async def update_settings(data: MeshSettings, ctx: Ctx = AdminCtx) -> dict:
    if data.topology not in ("hub_spoke", "full_mesh", "none"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "topology: hub_spoke | full_mesh | none")
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    tenant.mesh_topology = data.topology
    tenant.settings = {**(tenant.settings or {}), "mesh_auto": data.auto_apply}
    await ctx.audit("mesh.settings", target_type="tenant", target_id=tenant.id, details=data.model_dump())
    await ctx.db.commit()
    return {"topology": tenant.mesh_topology, "auto_apply": data.auto_apply}


@router.post("/apply")
async def apply(ctx: Ctx = TechCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    try:
        report = await apply_mesh(ctx.db, tenant)
    except MeshError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    ok = all(d.get("ok") for d in report["devices"].values())
    await ctx.audit("mesh.apply", target_type="tenant", target_id=tenant.id, success=ok,
                    details={"links": report["links"], "errors": [d for d in report["devices"].values() if not d.get("ok")]})
    await ctx.db.commit()
    return report
