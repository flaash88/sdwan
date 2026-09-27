"""Nachbarn/Topologie und Top-Verbraucher (Phase 25)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceFlow, DeviceStatus, PairingStatus, Site, Tenant
from app.routeros import RouterOSError
from app.services import flows as flow_svc
from app.services import neighbors as nb_svc

router = APIRouter(tags=["network-insight"])


@router.get("/devices/{device_id}/neighbors")
async def neighbors(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return await nb_svc.device_neighbors(ctx.db, dev)


@router.get("/sites/{site_id}/topology")
async def topology(site_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    site = await get_or_404(ctx.db, Site, site_id, "Site")
    return {"site": {"id": str(site.id), "name": site.name}, **await nb_svc.site_topology(ctx.db, site.id)}


# ----------------------------------------------------------------------------- Top-Verbraucher
def _flow_out(f: DeviceFlow | None) -> dict[str, Any]:
    if f is None:
        return {"enabled": False, "status": "off", "interfaces": [], "error": None, "updated_at": None}
    return {"enabled": f.enabled, "status": f.status, "interfaces": f.interfaces or [], "error": f.error, "updated_at": f.updated_at}


@router.get("/devices/{device_id}/flows")
async def flow_state(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    await get_or_404(ctx.db, Device, device_id, "Device")
    f = (await ctx.db.execute(select(DeviceFlow).where(DeviceFlow.device_id == device_id))).scalar_one_or_none()
    tenant = await ctx.db.get(Tenant, ctx.require_tenant())
    return {**_flow_out(f), "retention_days": int(((tenant.settings or {}) if tenant else {}).get("flow_retention_days") or flow_svc.DEFAULT_RETENTION_DAYS)}


class FlowIn(BaseModel):
    enabled: bool
    interfaces: list[str] | None = None


@router.put("/devices/{device_id}/flows")
async def set_flows(device_id: uuid.UUID, data: FlowIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Opt-in: IPFIX-Export der WAN-Interfaces an den Plattform-Collector ein-/ausschalten."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if dev.pairing_status != PairingStatus.paired or dev.status == DeviceStatus.offline:
        raise HTTPException(409, "Gerät ist nicht erreichbar")
    f = (await ctx.db.execute(select(DeviceFlow).where(DeviceFlow.device_id == device_id))).scalar_one_or_none()
    if f is None:
        f = DeviceFlow(tenant_id=dev.tenant_id, device_id=dev.id)
        ctx.db.add(f)
    try:
        if data.enabled:
            await flow_svc.enable(ctx.db, dev, f, data.interfaces)
        elif f.enabled or f.before is not None:
            await flow_svc.disable(dev, f)
    except flow_svc.FlowError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RouterOSError as exc:
        f.status, f.error = "error", str(exc)
        await ctx.db.commit()
        raise HTTPException(502, str(exc)) from exc
    await ctx.audit("flows.set", target_type="device", target_id=dev.id, details={"enabled": data.enabled, "interfaces": f.interfaces})
    await ctx.db.commit()
    return _flow_out(f)


@router.get("/devices/{device_id}/flows/top")
async def flow_top(device_id: uuid.UUID, period: Literal["1h", "24h", "7d"] = "24h", wan: str | None = None,
                   limit: int = 20, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    await get_or_404(ctx.db, Device, device_id, "Device")
    return await flow_svc.top(ctx.db, device_id, period, wan, max(1, min(limit, 100)))


class RetentionIn(BaseModel):
    flow_retention_days: int = Field(ge=1, le=90)


@router.put("/flows/settings")
async def flow_settings(data: RetentionIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    t = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    t.settings = {**(t.settings or {}), "flow_retention_days": data.flow_retention_days}
    await ctx.audit("flows.settings", target_type="tenant", target_id=t.id, details=data.model_dump())
    await ctx.db.commit()
    return data.model_dump()
