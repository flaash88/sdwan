from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.v1.common import apply_update, get_or_404
from app.deps import Ctx, ReadCtx, SuperCtx
from app.models import Device, Site, Tenant
from app.schemas import TenantCreate, TenantOut, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("", response_model=list[TenantOut])
async def list_tenants(ctx: Ctx = ReadCtx) -> list[Tenant]:
    q = select(Tenant).order_by(Tenant.name)
    if not ctx.is_superuser:
        q = q.where(Tenant.id == ctx.tenant_id)
    return list((await ctx.db.execute(q)).scalars())


@router.post("", response_model=TenantOut, status_code=201)
async def create_tenant(data: TenantCreate, ctx: Ctx = SuperCtx) -> Tenant:
    exists = await ctx.db.execute(select(Tenant).where((Tenant.slug == data.slug) | (Tenant.name == data.name)))
    if exists.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Tenant mit diesem Namen/Slug existiert bereits")
    tenant = Tenant(**data.model_dump())
    ctx.db.add(tenant)
    await ctx.db.flush()
    await ctx.audit("tenant.create", tenant_id=tenant.id, target_type="tenant", target_id=tenant.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return tenant


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    if not ctx.is_superuser and tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant nicht gefunden")
    tenant = await get_or_404(ctx.db, Tenant, tenant_id, "Tenant")
    sites = await ctx.db.scalar(select(func.count()).select_from(Site).where(Site.tenant_id == tenant_id))
    devices = await ctx.db.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == tenant_id))
    return {**TenantOut.model_validate(tenant).model_dump(mode="json"), "site_count": sites, "device_count": devices}


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(tenant_id: uuid.UUID, data: TenantUpdate, ctx: Ctx = SuperCtx) -> Tenant:
    tenant = await get_or_404(ctx.db, Tenant, tenant_id, "Tenant")
    changed = apply_update(tenant, data.model_dump(exclude_unset=True))
    await ctx.audit("tenant.update", tenant_id=tenant.id, target_type="tenant", target_id=tenant.id, details=changed)
    await ctx.db.commit()
    return tenant


@router.delete("/{tenant_id}", status_code=204, response_model=None)
async def delete_tenant(tenant_id: uuid.UUID, ctx: Ctx = SuperCtx) -> None:
    tenant = await get_or_404(ctx.db, Tenant, tenant_id, "Tenant")
    await ctx.audit("tenant.delete", tenant_id=None, target_type="tenant", target_id=tenant.id, details={"name": tenant.name})
    await ctx.db.delete(tenant)
    await ctx.db.commit()
