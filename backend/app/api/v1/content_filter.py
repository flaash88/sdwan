from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import ContentFilterProfile, Device, Site, Tenant
from app.security import encrypt_secret
from app.services.content_filter import apply_tenant, sync_profile
from app.services.nextdns import BLOCKLISTS, CATEGORIES, DEFAULT_SECURITY, SECURITY, SERVICES, NextDNSClient, NextDNSError

router = APIRouter(prefix="/content-filter", tags=["content-filter"])
_DOMAIN = re.compile(r"^(\*\.)?([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9 ._\-]+$")
    categories: list[str] = []
    services: list[str] = []
    security: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_SECURITY))
    blocklists: list[str] = ["nextdns-recommended"]
    denylist: list[str] = []
    allowlist: list[str] = []
    safe_search: bool = False
    youtube_restricted: bool = False
    block_bypass: bool = True
    force_dns: bool = True

    @field_validator("categories")
    @classmethod
    def v_cat(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(CATEGORIES)
        if bad:
            raise ValueError(f"unbekannte Kategorien {sorted(bad)}")
        return v

    @field_validator("services")
    @classmethod
    def v_srv(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(SERVICES)
        if bad:
            raise ValueError(f"unbekannte Dienste {sorted(bad)}")
        return v

    @field_validator("security")
    @classmethod
    def v_sec(cls, v: dict[str, bool]) -> dict[str, bool]:
        bad = set(v) - set(SECURITY)
        if bad:
            raise ValueError(f"unbekannte Security-Optionen {sorted(bad)}")
        return v

    @field_validator("blocklists")
    @classmethod
    def v_bl(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(BLOCKLISTS)
        if bad:
            raise ValueError(f"unbekannte Blocklisten {sorted(bad)}")
        return v

    @field_validator("denylist", "allowlist")
    @classmethod
    def v_dom(cls, v: list[str]) -> list[str]:
        out = [d.strip().lower() for d in v if d.strip()]
        for d in out:
            if not _DOMAIN.match(d):
                raise ValueError(f"ungültige Domain {d!r}")
        return out


class AssignmentIn(BaseModel):
    default_profile_id: uuid.UUID | None = None
    sites: dict[uuid.UUID, uuid.UUID | None] = {}
    apply: bool = True


class ApiKeyIn(BaseModel):
    api_key: str = Field(min_length=10, max_length=200)


def _out(p: ContentFilterProfile) -> dict:
    return {
        "id": str(p.id), "name": p.name, "nextdns_profile_id": p.nextdns_profile_id, "categories": p.categories, "services": p.services,
        "security": p.security, "blocklists": p.blocklists, "denylist": p.denylist, "allowlist": p.allowlist,
        "safe_search": p.safe_search, "youtube_restricted": p.youtube_restricted, "block_bypass": p.block_bypass,
        "force_dns": p.force_dns, "sync_status": p.sync_status, "last_error": p.last_error, "synced_at": p.synced_at,
    }


@router.get("/catalog")
async def catalog(_ctx: Ctx = ReadCtx) -> dict:
    return {"categories": CATEGORIES, "services": SERVICES, "security": SECURITY, "blocklists": BLOCKLISTS, "default_security": DEFAULT_SECURITY}


@router.get("/profiles")
async def list_profiles(ctx: Ctx = ReadCtx) -> list[dict]:
    return [_out(p) for p in (await ctx.db.execute(select(ContentFilterProfile).order_by(ContentFilterProfile.name))).scalars()]


async def _sync(ctx: Ctx, p: ContentFilterProfile) -> None:
    try:
        await sync_profile(ctx.db, p)
    except NextDNSError:
        pass  # Status/Fehler stehen im Profil


@router.post("/profiles", status_code=201)
async def create_profile(data: ProfileIn, ctx: Ctx = AdminCtx) -> dict:
    p = ContentFilterProfile(tenant_id=ctx.require_tenant(), **data.model_dump())
    ctx.db.add(p)
    await ctx.db.flush()
    await _sync(ctx, p)
    await ctx.audit("content_filter.create", target_type="filter_profile", target_id=p.id, details={**data.model_dump(), "sync": p.sync_status})
    await ctx.db.commit()
    return _out(p)


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    return _out(await get_or_404(ctx.db, ContentFilterProfile, profile_id, "Profil"))


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: uuid.UUID, data: ProfileIn, ctx: Ctx = AdminCtx) -> dict:
    p = await get_or_404(ctx.db, ContentFilterProfile, profile_id, "Profil")
    force_changed = p.force_dns != data.force_dns
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    await _sync(ctx, p)
    await ctx.audit("content_filter.update", target_type="filter_profile", target_id=p.id, details={**data.model_dump(), "sync": p.sync_status})
    if force_changed:
        await apply_tenant(ctx.db, p.tenant_id)
    await ctx.db.commit()
    return _out(p)


@router.post("/profiles/{profile_id}/sync")
async def resync(profile_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    p = await get_or_404(ctx.db, ContentFilterProfile, profile_id, "Profil")
    await _sync(ctx, p)
    await ctx.audit("content_filter.sync", target_type="filter_profile", target_id=p.id, success=p.sync_status == "synced", details={"error": p.last_error})
    await ctx.db.commit()
    return _out(p)


@router.delete("/profiles/{profile_id}", status_code=204, response_model=None)
async def delete_profile(profile_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    p = await get_or_404(ctx.db, ContentFilterProfile, profile_id, "Profil")
    tenant = await get_or_404(ctx.db, Tenant, p.tenant_id, "Tenant")
    if (tenant.settings or {}).get("default_filter_profile") == str(p.id) or (
        await ctx.db.execute(select(Site).where(Site.content_filter_profile_id == p.id).limit(1))
    ).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Profil ist noch zugewiesen")
    if p.nextdns_profile_id:
        try:
            from app.services.content_filter import _client_for

            await _client_for(tenant).delete_profile(p.nextdns_profile_id)
        except NextDNSError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("content_filter.delete", target_type="filter_profile", target_id=p.id, details={"name": p.name})
    await ctx.db.delete(p)
    await ctx.db.commit()


@router.get("/assignment")
async def get_assignment(ctx: Ctx = ReadCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    sites = (await ctx.db.execute(select(Site).order_by(Site.name))).scalars()
    devices = (await ctx.db.execute(select(Device).order_by(Device.name))).scalars()
    return {
        "default_profile_id": (tenant.settings or {}).get("default_filter_profile"),
        "api_key_configured": bool((tenant.settings or {}).get("nextdns_api_key_enc")),
        "sites": {str(s.id): str(s.content_filter_profile_id) if s.content_filter_profile_id else None for s in sites},
        "devices": [{"id": str(d.id), "name": d.name, "site_id": str(d.site_id) if d.site_id else None, "dns_filter": (d.facts or {}).get("dns_filter")} for d in devices],
    }


@router.put("/assignment")
async def put_assignment(data: AssignmentIn, ctx: Ctx = AdminCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    ids = {data.default_profile_id, *data.sites.values()} - {None}
    for pid in ids:
        await get_or_404(ctx.db, ContentFilterProfile, pid, "Profil")  # nur eigene Profile
    tenant.settings = {**(tenant.settings or {}), "default_filter_profile": str(data.default_profile_id) if data.default_profile_id else None}
    for site_id, pid in data.sites.items():
        site = await get_or_404(ctx.db, Site, site_id, "Site")
        site.content_filter_profile_id = pid
    await ctx.db.flush()
    report = await apply_tenant(ctx.db, tenant.id) if data.apply else None
    await ctx.audit("content_filter.assign", details={"default": str(data.default_profile_id) if data.default_profile_id else None,
                                                       "sites": {str(k): str(v) if v else None for k, v in data.sites.items()},
                                                       "errors": [r for r in (report or {}).values() if not r.get("ok")]})
    await ctx.db.commit()
    return {"applied": report}


@router.post("/apply")
async def apply(ctx: Ctx = TechCtx) -> dict:
    tenant_id = ctx.require_tenant()
    report = await apply_tenant(ctx.db, tenant_id)
    await ctx.audit("content_filter.apply", success=all(r.get("ok") for r in report.values()), details={"devices": len(report)})
    await ctx.db.commit()
    return report


@router.put("/api-key")
async def set_api_key(data: ApiKeyIn, ctx: Ctx = AdminCtx) -> dict:
    """Mandanteneigener NextDNS-API-Key (sonst globaler Key des MSP)."""
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    try:
        await NextDNSClient(data.api_key)._req("GET", "/profiles")
    except NextDNSError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"API-Key ungültig: {exc}") from exc
    tenant.settings = {**(tenant.settings or {}), "nextdns_api_key_enc": encrypt_secret(data.api_key)}
    await ctx.audit("content_filter.api_key", details={"set": True})
    await ctx.db.commit()
    return {"ok": True}
