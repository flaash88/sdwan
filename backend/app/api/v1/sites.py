from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.v1.common import apply_update, get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Site, Tenant
from app.schemas import SiteCreate, SiteOut, SiteUpdate

router = APIRouter(prefix="/sites", tags=["sites"])


@router.get("", response_model=list[SiteOut])
async def list_sites(ctx: Ctx = ReadCtx) -> list[Site]:
    return list((await ctx.db.execute(select(Site).order_by(Site.name))).scalars())


@router.post("", response_model=SiteOut, status_code=201)
async def create_site(data: SiteCreate, ctx: Ctx = TechCtx) -> Site:
    tenant_id = ctx.tenant_id or data.tenant_id
    if tenant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant_id erforderlich")
    if ctx.tenant_id and data.tenant_id and data.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Falscher Tenant")
    await get_or_404(ctx.db, Tenant, tenant_id, "Tenant")
    if (await ctx.db.execute(select(Site).where(Site.tenant_id == tenant_id, Site.name == data.name))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Site-Name existiert bereits")
    site = Site(tenant_id=tenant_id, **data.model_dump(exclude={"tenant_id"}))
    ctx.db.add(site)
    await ctx.db.flush()
    await ctx.audit("site.create", tenant_id=tenant_id, target_type="site", target_id=site.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return site


@router.get("/{site_id}", response_model=SiteOut)
async def get_site(site_id: uuid.UUID, ctx: Ctx = ReadCtx) -> Site:
    return await get_or_404(ctx.db, Site, site_id, "Site")


@router.patch("/{site_id}", response_model=SiteOut)
async def update_site(site_id: uuid.UUID, data: SiteUpdate, ctx: Ctx = TechCtx) -> Site:
    site = await get_or_404(ctx.db, Site, site_id, "Site")
    changed = apply_update(site, data.model_dump(exclude_unset=True))
    await ctx.audit("site.update", tenant_id=site.tenant_id, target_type="site", target_id=site.id, details=changed)
    await ctx.db.commit()
    return site


@router.delete("/{site_id}", status_code=204, response_model=None)
async def delete_site(site_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    site = await get_or_404(ctx.db, Site, site_id, "Site")
    await ctx.audit("site.delete", tenant_id=site.tenant_id, target_type="site", target_id=site.id, details={"name": site.name})
    await ctx.db.delete(site)
    await ctx.db.commit()
