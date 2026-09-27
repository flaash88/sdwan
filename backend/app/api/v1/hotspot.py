"""Hotspot / Gäste-Portal (Phase 20): Portale, Hotspots, Voucher, Live-Gäste, Registrierungen, öffentliche Anmeldung."""

from __future__ import annotations

import copy
import csv
import io
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.v1.common import get_or_404
from app.db import system_session
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, GuestRegistration, HotspotInstance, HotspotPortal, Tenant, Voucher, VoucherBatch, VoucherProfile
from app.routeros import RouterOSError, connect_device
from app.services import hotspot as hs
from app.services.wlan import qr_svg

router = APIRouter(tags=["hotspot"])


def _err(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


# ----------------------------------------------------------------------------- Portale
class FormField(BaseModel):
    key: str
    label_de: str = Field(max_length=100)
    label_en: str = Field(max_length=100)
    type: str = "text"
    required: bool = False
    max_len: int = 100


class PortalIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    login_type: Literal["voucher", "click", "form"] = "voucher"
    design: dict[str, Any] = {}
    texts: dict[str, dict[str, str]] = {}
    terms_required: bool = True
    form_fields: list[FormField] = []


def _portal_out(p: HotspotPortal) -> dict[str, Any]:
    return {"id": str(p.id), "name": p.name, "description": p.description, "builtin": p.builtin, "scope": "global" if p.tenant_id is None else "tenant",
            "login_type": p.login_type, "design": p.design or {}, "texts": p.texts or {}, "terms_required": p.terms_required,
            "form_fields": p.form_fields or [], "custom_files": sorted((p.custom_files or {}).keys()), "version": p.version}


def _portal_write(ctx: Ctx, p: HotspotPortal | None = None) -> None:
    if p is not None:
        if p.builtin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Vorlage – bitte kopieren und die Kopie anpassen")
        if p.tenant_id is None and not ctx.is_superuser:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Portale pflegt nur der MSP")


def _clean_texts(texts: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    keys = ("title", "welcome", "button", "code_label", "terms_label", "terms", "success")
    return {lang: {k: str((texts.get(lang) or {}).get(k, ""))[:2000] for k in keys} for lang in ("de", "en")}


@router.get("/hotspot/portals")
async def list_portals(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(HotspotPortal).order_by(HotspotPortal.builtin.desc(), HotspotPortal.name))).scalars().all()
    return [_portal_out(p) for p in rows]


@router.post("/hotspot/portals", status_code=201)
async def create_portal(data: PortalIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    d = data.model_dump()
    try:
        hs.check_portal(d)
    except hs.HotspotError as exc:
        raise _err(exc) from exc
    p = HotspotPortal(tenant_id=ctx.tenant_id, builtin=False, **{**d, "texts": _clean_texts(data.texts)})  # MSP ohne Mandant → global
    ctx.db.add(p)
    await ctx.db.flush()
    await ctx.audit("hotspot.portal.create", target_type="hotspot_portal", target_id=p.id, details={"name": p.name})
    await ctx.db.commit()
    return _portal_out(p)


@router.put("/hotspot/portals/{portal_id}")
async def update_portal(portal_id: uuid.UUID, data: PortalIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    p = await get_or_404(ctx.db, HotspotPortal, portal_id, "Portal")
    _portal_write(ctx, p)
    d = data.model_dump()
    try:
        hs.check_portal(d)
    except hs.HotspotError as exc:
        raise _err(exc) from exc
    for k, v in {**d, "texts": _clean_texts(data.texts)}.items():
        setattr(p, k, v)
    p.version += 1
    await ctx.audit("hotspot.portal.update", target_type="hotspot_portal", target_id=p.id, details={"name": p.name, "version": p.version})
    await ctx.db.commit()
    return _portal_out(p)


@router.put("/hotspot/portals/{portal_id}/files")
async def upload_portal_files(portal_id: uuid.UUID, files: dict[str, str], ctx: Ctx = AdminCtx) -> dict[str, Any]:
    """Eigene Login-Seiten (ersetzen die erzeugten gleichen Namens). Leeres Objekt = wieder erzeugte Seiten."""
    p = await get_or_404(ctx.db, HotspotPortal, portal_id, "Portal")
    _portal_write(ctx, p)
    try:
        hs.check_files(files)
    except hs.HotspotError as exc:
        raise _err(exc) from exc
    p.custom_files = files
    p.version += 1
    await ctx.audit("hotspot.portal.files", target_type="hotspot_portal", target_id=p.id, details={"files": sorted(files)})
    await ctx.db.commit()
    return _portal_out(p)


@router.delete("/hotspot/portals/{portal_id}", status_code=204, response_model=None)
async def delete_portal(portal_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    p = await get_or_404(ctx.db, HotspotPortal, portal_id, "Portal")
    _portal_write(ctx, p)
    if (await ctx.db.execute(select(HotspotInstance.id).where(HotspotInstance.portal_id == p.id).limit(1))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Portal wird von einem Hotspot verwendet")
    await ctx.audit("hotspot.portal.delete", target_type="hotspot_portal", target_id=p.id, details={"name": p.name})
    await ctx.db.delete(p)
    await ctx.db.commit()


@router.post("/hotspot/portals/{portal_id}/copy", status_code=201)
async def copy_portal(portal_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    src = await get_or_404(ctx.db, HotspotPortal, portal_id, "Portal")
    p = HotspotPortal(tenant_id=ctx.tenant_id, builtin=False, name=f"{src.name} (Kopie)", description=src.description, login_type=src.login_type,
                      design=copy.deepcopy(src.design), texts=copy.deepcopy(src.texts), terms_required=src.terms_required,
                      form_fields=copy.deepcopy(src.form_fields), custom_files=copy.deepcopy(src.custom_files))
    ctx.db.add(p)
    await ctx.db.flush()
    await ctx.audit("hotspot.portal.copy", target_type="hotspot_portal", target_id=p.id, details={"from": str(src.id)})
    await ctx.db.commit()
    return _portal_out(p)


@router.get("/hotspot/portals/{portal_id}/preview", response_class=HTMLResponse)
async def preview_portal(portal_id: uuid.UUID, ctx: Ctx = ReadCtx, page: str = Query(default="login.html", pattern=r"^[a-z0-9_-]+\.html$")) -> HTMLResponse:
    p = await get_or_404(ctx.db, HotspotPortal, portal_id, "Portal")
    return HTMLResponse(hs.preview(p, None, page), headers={"Content-Security-Policy": "script-src 'unsafe-inline'; connect-src 'none'"})


class PreviewIn(PortalIn):
    page: str = "login.html"


@router.post("/hotspot/preview", response_class=HTMLResponse)
async def preview_draft(data: PreviewIn, ctx: Ctx = ReadCtx) -> HTMLResponse:
    """Vorschau eines noch nicht gespeicherten Entwurfs (Designer)."""
    p = HotspotPortal(**{**data.model_dump(exclude={"page"}), "texts": _clean_texts(data.texts), "custom_files": {}})
    return HTMLResponse(hs.preview(p, None, data.page), headers={"Content-Security-Policy": "script-src 'unsafe-inline'; connect-src 'none'"})


# ----------------------------------------------------------------------------- Voucher-Profile
class VProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)
    validity_min: int = Field(default=1440, ge=5, le=525600)
    data_limit_mb: int | None = Field(default=None, ge=1, le=10_000_000)
    rate_limit: str | None = Field(default=None, max_length=40)
    shared_users: int = Field(default=1, ge=1, le=20)


def _vp_out(p: VoucherProfile) -> dict[str, Any]:
    return {"id": str(p.id), "name": p.name, "slug": p.slug, "validity_min": p.validity_min, "data_limit_mb": p.data_limit_mb,
            "rate_limit": p.rate_limit, "shared_users": p.shared_users}


@router.get("/hotspot/voucher-profiles")
async def list_vprofiles(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    return [_vp_out(p) for p in (await ctx.db.execute(select(VoucherProfile).order_by(VoucherProfile.name))).scalars()]


@router.post("/hotspot/voucher-profiles", status_code=201)
async def create_vprofile(data: VProfileIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    try:
        hs.check_slug(data.slug)
        hs.check_rate(data.rate_limit)
    except hs.HotspotError as exc:
        raise _err(exc) from exc
    if (await ctx.db.execute(select(VoucherProfile).where(VoucherProfile.slug == data.slug))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Kürzel bereits vergeben")
    p = VoucherProfile(tenant_id=tenant_id, **data.model_dump())
    ctx.db.add(p)
    await ctx.db.flush()
    await ctx.audit("hotspot.voucher_profile.create", target_type="voucher_profile", target_id=p.id, details=data.model_dump())
    await ctx.db.commit()
    return _vp_out(p)


@router.put("/hotspot/voucher-profiles/{profile_id}")
async def update_vprofile(profile_id: uuid.UUID, data: VProfileIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    p = await get_or_404(ctx.db, VoucherProfile, profile_id, "Voucher-Profil")
    if data.slug != p.slug:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Kürzel kann nicht geändert werden")
    try:
        hs.check_rate(data.rate_limit)
    except hs.HotspotError as exc:
        raise _err(exc) from exc
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    await ctx.audit("hotspot.voucher_profile.update", target_type="voucher_profile", target_id=p.id, details=data.model_dump())
    await ctx.db.commit()
    return _vp_out(p)


@router.delete("/hotspot/voucher-profiles/{profile_id}", status_code=204, response_model=None)
async def delete_vprofile(profile_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    p = await get_or_404(ctx.db, VoucherProfile, profile_id, "Voucher-Profil")
    if (await ctx.db.execute(select(Voucher.id).where(Voucher.profile_id == p.id).limit(1))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Es gibt Voucher mit diesem Profil")
    await ctx.audit("hotspot.voucher_profile.delete", target_type="voucher_profile", target_id=p.id, details={"name": p.name})
    await ctx.db.delete(p)
    await ctx.db.commit()


# ----------------------------------------------------------------------------- Hotspots
class InstanceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)
    device_id: uuid.UUID
    portal_id: uuid.UUID
    interface: str = Field(min_length=1, max_length=64, pattern=r"^[^\s\"\\;]+$")
    hotspot_address: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,3}){3}$")
    dns_name: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9.-]+$")
    walled_garden: list[str] = []
    session_timeout_min: int = Field(default=240, ge=5, le=10080)
    idle_timeout_min: int = Field(default=15, ge=1, le=1440)
    rate_limit: str | None = Field(default=None, max_length=40)
    enabled: bool = True


async def _inst_out(ctx: Ctx, i: HotspotInstance) -> dict[str, Any]:
    dev = await ctx.db.get(Device, i.device_id)
    portal = await ctx.db.get(HotspotPortal, i.portal_id)
    counts = dict((await ctx.db.execute(select(Voucher.status, func.count()).where(Voucher.instance_id == i.id).group_by(Voucher.status))).all())
    regs = (await ctx.db.execute(select(func.count()).select_from(GuestRegistration).where(GuestRegistration.instance_id == i.id))).scalar_one()
    undeployed = i.applied_version != i.version or (portal is not None and i.applied_portal_version != portal.version)
    return {"id": str(i.id), "name": i.name, "slug": i.slug, "device_id": str(i.device_id), "device": dev.name if dev else "?",
            "portal_id": str(i.portal_id), "portal": portal.name if portal else "?", "login_type": portal.login_type if portal else None,
            "interface": i.interface, "hotspot_address": i.hotspot_address, "dns_name": i.dns_name, "walled_garden": i.walled_garden or [],
            "session_timeout_min": i.session_timeout_min, "idle_timeout_min": i.idle_timeout_min, "rate_limit": i.rate_limit, "enabled": i.enabled,
            "status": i.status, "last_error": i.last_error, "applied_at": i.applied_at, "undeployed": undeployed, "router_name": hs.base(i),
            "vouchers": counts, "registrations": regs, "register_url": hs.register_url(i)}


def _check_inst(data: InstanceIn) -> None:
    try:
        hs.check_slug(data.slug)
        hs.check_rate(data.rate_limit)
        hs.check_hosts(data.walled_garden)
    except hs.HotspotError as exc:
        raise _err(exc) from exc


@router.get("/hotspot/instances")
async def list_instances(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(HotspotInstance).order_by(HotspotInstance.name))).scalars().all()
    return [await _inst_out(ctx, i) for i in rows]


@router.post("/hotspot/instances", status_code=201)
async def create_instance(data: InstanceIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    _check_inst(data)
    dev = await get_or_404(ctx.db, Device, data.device_id, "Device")
    await get_or_404(ctx.db, HotspotPortal, data.portal_id, "Portal")
    if dev.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device nicht gefunden")
    if (await ctx.db.execute(select(HotspotInstance).where(HotspotInstance.slug == data.slug))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Kürzel bereits vergeben")
    from app.services.advisories import block_message, blocking

    advs = await blocking(ctx.db, dev, "hotspot")
    if advs:  # Phase 23: Aktivierung blockieren
        raise HTTPException(status.HTTP_409_CONFLICT, block_message(dev, advs))
    i = HotspotInstance(tenant_id=tenant_id, version=1, **data.model_dump())
    ctx.db.add(i)
    await ctx.db.flush()
    await ctx.audit("hotspot.instance.create", target_type="hotspot_instance", target_id=i.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return await _inst_out(ctx, i)


@router.put("/hotspot/instances/{instance_id}")
async def update_instance(instance_id: uuid.UUID, data: InstanceIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    if data.slug != i.slug or data.device_id != i.device_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Kürzel und Gerät können nicht geändert werden")
    _check_inst(data)
    await get_or_404(ctx.db, HotspotPortal, data.portal_id, "Portal")
    for k, v in data.model_dump().items():
        setattr(i, k, v)
    i.version += 1
    await ctx.audit("hotspot.instance.update", target_type="hotspot_instance", target_id=i.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return await _inst_out(ctx, i)


@router.delete("/hotspot/instances/{instance_id}", status_code=204, response_model=None)
async def delete_instance(instance_id: uuid.UUID, ctx: Ctx = AdminCtx, force: bool = False) -> None:
    """Entfernt den Hotspot vom Gerät, dann aus der Plattform. Ist das Gerät nicht erreichbar, nur mit ``force``."""
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    try:
        await hs.apply_instance(ctx.db, i, remove=True)
    except (RouterOSError, hs.HotspotError) as exc:
        if not force:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Hotspot konnte nicht vom Gerät entfernt werden: {exc}") from exc
    await ctx.audit("hotspot.instance.delete", target_type="hotspot_instance", target_id=i.id, details={"name": i.name, "force": force})
    await ctx.db.delete(i)
    await ctx.db.commit()


@router.post("/hotspot/instances/{instance_id}/apply")
async def apply_instance(instance_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    try:
        res = await hs.apply_instance(ctx.db, i)
    except (RouterOSError, hs.HotspotError) as exc:
        i.status, i.last_error = "error", str(exc)
        await ctx.audit("hotspot.instance.apply", target_type="hotspot_instance", target_id=i.id, success=False, details={"error": str(exc)})
        await ctx.db.commit()
        code = status.HTTP_409_CONFLICT if isinstance(exc, hs.HotspotBlocked) else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, str(exc)) from exc
    await ctx.audit("hotspot.instance.apply", target_type="hotspot_instance", target_id=i.id, details={"stats": res["stats"], "version": i.version})
    await ctx.db.commit()
    return {**res, "instance": await _inst_out(ctx, i)}


async def _live(ctx: Ctx, i: HotspotInstance):  # noqa: ANN202
    dev = await ctx.db.get(Device, i.device_id)
    return connect_device(dev)


@router.get("/hotspot/instances/{instance_id}/active")
async def active(instance_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    """Aktive Gäste (live vom Router, nicht gespeichert) und gesperrte Geräte."""
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    try:
        async with await _live(ctx, i) as api:
            return {"active": await hs.active_guests(api, i), "blocked": await hs.blocked_macs(api, i)}
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


class GuestAction(BaseModel):
    active_id: str | None = None
    user: str | None = None
    mac: str | None = Field(default=None, pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@router.post("/hotspot/instances/{instance_id}/disconnect")
async def disconnect(instance_id: uuid.UUID, data: GuestAction, ctx: Ctx = TechCtx) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    if not data.active_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "active_id fehlt")
    try:
        async with await _live(ctx, i) as api:
            await api.remove("/ip/hotspot/active", data.active_id)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("hotspot.guest.disconnect", target_type="hotspot_instance", target_id=i.id, details={"user": data.user})
    await ctx.db.commit()
    return {"ok": True}


@router.post("/hotspot/instances/{instance_id}/block")
async def block(instance_id: uuid.UUID, data: GuestAction, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Sperren: Voucher-Gast → Voucher deaktivieren; Klick/Formular-Gast → MAC per ip-binding sperren. Dazu trennen."""
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    v = None
    if data.user and not data.user.startswith("T-"):
        v = (await ctx.db.execute(select(Voucher).where(Voucher.instance_id == i.id, Voucher.code == data.user))).scalar_one_or_none()
    if v is None and not data.mac:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Weder Voucher noch MAC-Adresse")
    try:
        async with await _live(ctx, i) as api:
            if v is not None:
                v.status = "blocked"
                for u in await api.print("/ip/hotspot/user"):
                    if u.get("name") == v.code and str(u.get("comment", "")).startswith(f"{hs.COMMENT}{i.slug}:v"):
                        await api.set("/ip/hotspot/user", u[".id"], disabled="yes")
            else:
                await api.add("/ip/hotspot/ip-binding", **{"mac-address": data.mac, "type": "blocked", "server": hs.base(i),
                                                           "comment": f"{hs.COMMENT}{i.slug}:block"})
            if data.active_id:
                await api.remove("/ip/hotspot/active", data.active_id)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("hotspot.guest.block", target_type="hotspot_instance", target_id=i.id, details={"voucher": v.code if v else None, "mac": data.mac})
    await ctx.db.commit()
    return {"ok": True, "voucher": v.code if v else None}


@router.delete("/hotspot/instances/{instance_id}/blocked/{binding_id}", status_code=204, response_model=None)
async def unblock(instance_id: uuid.UUID, binding_id: str, ctx: Ctx = TechCtx) -> None:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    try:
        async with await _live(ctx, i) as api:
            if not any(b["id"] == binding_id for b in await hs.blocked_macs(api, i)):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Sperre nicht gefunden")
            await api.remove("/ip/hotspot/ip-binding", binding_id)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("hotspot.guest.unblock", target_type="hotspot_instance", target_id=i.id, details={"binding": binding_id})
    await ctx.db.commit()


# ----------------------------------------------------------------------------- Voucher
class BatchIn(BaseModel):
    profile_id: uuid.UUID
    count: int = Field(ge=1, le=500)
    note: str | None = Field(default=None, max_length=200)


def _v_out(v: Voucher) -> dict[str, Any]:
    return {"id": str(v.id), "code": v.code, "status": v.status, "batch_id": str(v.batch_id), "profile_id": str(v.profile_id), "pushed": v.pushed,
            "first_used_at": v.first_used_at, "uptime_s": v.uptime_s, "bytes_total": v.bytes_total, "created_at": v.created_at}


@router.post("/hotspot/instances/{instance_id}/batches", status_code=201)
async def create_batch(instance_id: uuid.UUID, data: BatchIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    await get_or_404(ctx.db, VoucherProfile, data.profile_id, "Voucher-Profil")
    existing = set((await ctx.db.execute(select(Voucher.code).where(Voucher.instance_id == i.id))).scalars())
    b = VoucherBatch(tenant_id=i.tenant_id, instance_id=i.id, profile_id=data.profile_id, count=data.count, note=data.note, created_by=ctx.user.email)
    ctx.db.add(b)
    await ctx.db.flush()
    for _ in range(data.count):
        ctx.db.add(Voucher(tenant_id=i.tenant_id, instance_id=i.id, batch_id=b.id, profile_id=data.profile_id, code=hs.generate_code(existing)))
    await ctx.db.flush()
    pushed, error = False, None
    try:  # sofort übertragen; sonst übernimmt der Worker
        await hs.apply_instance(ctx.db, i)
        pushed = True
    except (RouterOSError, hs.HotspotError) as exc:
        error = str(exc)
    await ctx.audit("hotspot.vouchers.create", target_type="hotspot_instance", target_id=i.id, details={"batch": str(b.id), "count": data.count, "pushed": pushed})
    await ctx.db.commit()
    return {"batch_id": str(b.id), "count": data.count, "pushed": pushed, "error": error}


@router.get("/hotspot/instances/{instance_id}/vouchers")
async def list_vouchers(instance_id: uuid.UUID, ctx: Ctx = ReadCtx, batch_id: uuid.UUID | None = None) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    q = select(Voucher).where(Voucher.instance_id == i.id)
    if batch_id:
        q = q.where(Voucher.batch_id == batch_id)
    vs = (await ctx.db.execute(q.order_by(Voucher.created_at.desc(), Voucher.code).limit(2000))).scalars().all()
    batches = (await ctx.db.execute(select(VoucherBatch).where(VoucherBatch.instance_id == i.id).order_by(VoucherBatch.created_at.desc()))).scalars().all()
    return {"vouchers": [_v_out(v) for v in vs],
            "batches": [{"id": str(b.id), "count": b.count, "note": b.note, "profile_id": str(b.profile_id), "created_by": b.created_by,
                         "created_at": b.created_at} for b in batches]}


@router.post("/hotspot/vouchers/{voucher_id}/block")
async def block_voucher(voucher_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    v = await get_or_404(ctx.db, Voucher, voucher_id, "Voucher")
    i = await ctx.db.get(HotspotInstance, v.instance_id)
    v.status = "blocked"
    await ctx.audit("hotspot.voucher.block", target_type="hotspot_instance", target_id=i.id, details={"code": v.code})
    try:
        await hs.apply_instance(ctx.db, i)
        applied = True
    except (RouterOSError, hs.HotspotError):
        applied = False  # Worker überträgt später
    await ctx.db.commit()
    return {**_v_out(v), "applied": applied}


async def _batch_data(ctx: Ctx, batch_id: uuid.UUID) -> tuple[VoucherBatch, HotspotInstance, VoucherProfile, list[Voucher]]:
    b = await get_or_404(ctx.db, VoucherBatch, batch_id, "Voucher-Stapel")
    i = await ctx.db.get(HotspotInstance, b.instance_id)
    p = await ctx.db.get(VoucherProfile, b.profile_id)
    vs = (await ctx.db.execute(select(Voucher).where(Voucher.batch_id == b.id).order_by(Voucher.code))).scalars().all()
    return b, i, p, list(vs)


@router.get("/hotspot/batches/{batch_id}/print")
async def batch_print(batch_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Daten für die A4-Druckansicht: Code, QR-Code (Login-URL mit Code) je Voucher."""
    b, i, p, vs = await _batch_data(ctx, batch_id)
    portal = await ctx.db.get(HotspotPortal, i.portal_id)
    address = i.hotspot_address
    if not address and not i.dns_name:  # Adresse des Interfaces vom Router (best effort)
        try:
            async with await _live(ctx, i) as api:
                address = await hs._interface_address(api, i.interface)
        except RouterOSError:
            address = None
    url = hs.login_url(i, address)
    await ctx.audit("hotspot.vouchers.print", target_type="hotspot_instance", target_id=i.id, details={"batch": str(b.id)})
    await ctx.db.commit()
    return {"instance": i.name, "profile": _vp_out(p), "note": b.note, "login_url": url,
            "title": ((portal.texts or {}).get("de") or {}).get("title") if portal else None,
            "design": portal.design if portal else {},
            "vouchers": [{"code": v.code, "status": v.status, "qr_svg": qr_svg(f"{url}?username={v.code}&password=")} for v in vs if v.status != "blocked"]}


@router.get("/hotspot/batches/{batch_id}/csv")
async def batch_csv(batch_id: uuid.UUID, ctx: Ctx = TechCtx) -> Response:
    b, i, p, vs = await _batch_data(ctx, batch_id)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["code", "status", "profil", "gueltig_min", "datenlimit_mb", "erstmals_genutzt", "online_s", "bytes"])
    for v in vs:
        w.writerow([v.code, v.status, p.name, p.validity_min, p.data_limit_mb or "", v.first_used_at.isoformat() if v.first_used_at else "", v.uptime_s, v.bytes_total])
    await ctx.audit("hotspot.vouchers.csv", target_type="hotspot_instance", target_id=i.id, details={"batch": str(b.id)})
    await ctx.db.commit()
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="voucher-{i.slug}.csv"'})


# ----------------------------------------------------------------------------- Registrierungen / DSGVO
@router.get("/hotspot/instances/{instance_id}/registrations")
async def registrations(instance_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    i = await get_or_404(ctx.db, HotspotInstance, instance_id, "Hotspot")
    rows = (await ctx.db.execute(select(GuestRegistration).where(GuestRegistration.instance_id == i.id)
                                 .order_by(GuestRegistration.created_at.desc()).limit(1000))).scalars().all()
    tenant = await ctx.db.get(Tenant, i.tenant_id)
    await ctx.audit("hotspot.registrations.view", target_type="hotspot_instance", target_id=i.id, details={"count": len(rows)})
    await ctx.db.commit()
    return {"retention_days": hs.retention_days(tenant),
            "registrations": [{"id": str(r.id), "data": r.data, "terms_accepted": r.terms_accepted, "lang": r.lang, "created_at": r.created_at} for r in rows]}


class RetentionIn(BaseModel):
    days: int = Field(ge=1, le=3650)


@router.get("/hotspot/settings")
async def get_settings_(ctx: Ctx = ReadCtx) -> dict[str, Any]:
    tenant = await ctx.db.get(Tenant, ctx.require_tenant())
    return {"retention_days": hs.retention_days(tenant), "platform_host": hs.platform_host()}


@router.put("/hotspot/retention")
async def set_retention(data: RetentionIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    tenant = await ctx.db.get(Tenant, ctx.require_tenant())
    tenant.settings = {**(tenant.settings or {}), "guest_retention_days": data.days}
    await ctx.audit("hotspot.retention", target_type="tenant", target_id=tenant.id, details={"days": data.days})
    await ctx.db.commit()
    return {"retention_days": data.days}


# ----------------------------------------------------------------------------- öffentlich: Formular-Anmeldung
_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type",
         "Access-Control-Max-Age": "600"}


@router.options("/portal/{instance_id}/register", include_in_schema=False)
async def register_preflight(instance_id: uuid.UUID) -> Response:
    return Response(status_code=204, headers=_CORS)


@router.post("/portal/{instance_id}/register")
async def register(instance_id: uuid.UUID, request: Request) -> Response:
    """Ohne Anmeldung (Login-Seite im Gästenetz). Rate-Limit je IP und Portal, max. 4 KB, nur definierte Felder."""
    def reply(code: int, body: dict[str, Any]) -> Response:
        return Response(json.dumps(body), status_code=code, media_type="application/json", headers=_CORS)

    ip = request.client.host if request.client else "?"
    if int(request.headers.get("content-length") or 0) > hs.REGISTER_MAX_BYTES:
        return reply(413, {"detail": "zu groß"})
    raw = await request.body()
    if len(raw) > hs.REGISTER_MAX_BYTES:
        return reply(413, {"detail": "zu groß"})
    if await hs.rate_limited(ip, instance_id):
        return reply(429, {"detail": "zu viele Anfragen"})
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return reply(400, {"detail": "JSON erwartet"})
    if not isinstance(payload, dict):
        return reply(400, {"detail": "JSON-Objekt erwartet"})
    async with system_session() as db:
        inst = await db.get(HotspotInstance, instance_id)
        portal = await db.get(HotspotPortal, inst.portal_id) if inst else None
        if inst is None or not inst.enabled or portal is None or portal.login_type != "form":
            return reply(404, {"detail": "unbekannt"})
        if portal.terms_required and payload.get("terms_accepted") is not True:
            return reply(422, {"detail": "Nutzungsbedingungen nicht akzeptiert"})
        try:
            data = hs.clean_registration(portal, payload.get("fields"))
        except hs.HotspotError as exc:
            return reply(422, {"detail": str(exc)})
        lang = payload.get("lang") if payload.get("lang") in ("de", "en") else None
        db.add(GuestRegistration(tenant_id=inst.tenant_id, instance_id=inst.id, portal_id=portal.id, data=data,
                                 terms_accepted=payload.get("terms_accepted") is True, lang=lang))
        await db.commit()
    return reply(201, {"ok": True})
