"""WLAN-Verwaltung (Phase 19): Profile, Zuweisungen, Ausrollen, Gäste-PSK mit QR-Code, Status je Gerät."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceStatus, Tenant, WlanAssignment, WlanDeviceState, WlanProfile
from app.routeros import RouterOSError, connect_device
from app.security import decrypt_secret, encrypt_secret
from app.services import wlan as wl

router = APIRouter(tags=["wlan"])


class ScheduleIn(BaseModel):
    start: str
    end: str


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=1000)
    ssid: str = Field(min_length=1, max_length=32)
    security: str = "wpa2-wpa3-psk"
    passphrase: str | None = Field(default=None, max_length=63)  # None = unverändert (beim Ändern)
    radius_server: str | None = Field(default=None, max_length=100)
    radius_port: int = Field(default=1812, ge=1, le=65535)
    radius_secret: str | None = Field(default=None, max_length=200)
    band: str = "both"
    channel_width: str = "auto"
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    bridge: str = Field(default="bridge", min_length=1, max_length=64, pattern=r"^[^\s\"\\;]+$")
    client_isolation: bool = False
    hidden: bool = False
    schedule: ScheduleIn | None = None
    is_guest: bool = False
    psk_rotate_days: int | None = Field(default=None, ge=1, le=365)
    enabled: bool = True


class AssignmentIn(BaseModel):
    mode: Literal["local", "capsman"] = "local"
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []


def _check(data: ProfileIn, existing: WlanProfile | None) -> None:
    if data.passphrase:
        try:
            wl.check_passphrase(data.passphrase)
        except wl.WlanError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    d = data.model_dump()
    d["schedule"] = data.schedule.model_dump() if data.schedule else None
    has_psk = bool(data.passphrase or (existing and existing.passphrase_enc) or (data.is_guest and data.security.endswith("-psk")))
    has_sec = bool(data.radius_secret or (existing and existing.radius_secret_enc))
    try:
        wl.validate_profile(d, has_psk, has_sec)
    except wl.WlanError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def _apply_fields(p: WlanProfile, data: ProfileIn) -> None:
    for k, v in data.model_dump(exclude={"passphrase", "radius_secret", "schedule"}).items():
        setattr(p, k, v)
    p.schedule = data.schedule.model_dump() if data.schedule else None
    if data.passphrase:
        p.passphrase_enc = encrypt_secret(data.passphrase)
    elif data.security.endswith("-psk") and not p.passphrase_enc:  # Gäste-WLAN ohne PSK: erzeugen
        p.passphrase_enc = encrypt_secret(wl.generate_psk())
        p.psk_rotated_at = None
    if data.radius_secret:
        p.radius_secret_enc = encrypt_secret(data.radius_secret)


async def _out(ctx: Ctx, p: WlanProfile) -> dict[str, Any]:
    assigns = (await ctx.db.execute(select(WlanAssignment).where(WlanAssignment.profile_id == p.id))).scalars().all()
    states = (await ctx.db.execute(select(WlanDeviceState, Device).join(Device, Device.id == WlanDeviceState.device_id)
                                   .where(WlanDeviceState.profile_id == p.id))).all()
    targets = await wl.profile_devices(ctx.db, p)
    by_dev = {st.device_id: (st, d) for st, d in states}
    devices = []
    for d, mode in targets:
        st = by_dev.pop(d.id, (None, d))[0]
        devices.append({"device_id": str(d.id), "device": d.name, "mode": mode, "status": st.status if st else "pending",
                        "error": st.error if st else None, "applied_version": st.applied_version if st else None,
                        "applied_at": st.applied_at if st else None, "detail": st.detail if st else {},
                        "current": bool(st and st.applied_version == p.version and st.status == "ok")})
    removed = [{"device_id": str(d.id), "device": d.name} for st, d in by_dev.values()]  # nicht mehr zugewiesen, noch nicht entfernt
    return {
        "id": str(p.id), "name": p.name, "slug": p.slug, "description": p.description, "ssid": p.ssid, "security": p.security,
        "has_passphrase": bool(p.passphrase_enc), "radius_server": p.radius_server, "radius_port": p.radius_port,
        "has_radius_secret": bool(p.radius_secret_enc), "band": p.band, "channel_width": p.channel_width, "country_code": p.country_code,
        "vlan_id": p.vlan_id, "bridge": p.bridge, "client_isolation": p.client_isolation, "hidden": p.hidden, "schedule": p.schedule,
        "is_guest": p.is_guest, "psk_rotate_days": p.psk_rotate_days, "psk_rotated_at": p.psk_rotated_at, "enabled": p.enabled,
        "version": p.version, "router_name": wl.base(p),
        "assignments": [{"id": str(a.id), "mode": a.mode, **(a.targets or {})} for a in assigns],
        "devices": devices, "pending_removal": removed,
        "undeployed": [x["device"] for x in devices if not x["current"]] + [x["device"] for x in removed],
    }


@router.get("/wlan/countries")
async def countries(ctx: Ctx = ReadCtx) -> dict[str, Any]:
    tenant = await ctx.db.get(Tenant, ctx.require_tenant())
    return {"countries": wl.COUNTRIES, "tenant_default": tenant.country_code if tenant else "AT"}


@router.get("/wlan/profiles")
async def list_profiles(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    ctx.require_tenant()
    rows = (await ctx.db.execute(select(WlanProfile).order_by(WlanProfile.name))).scalars().all()
    return [await _out(ctx, p) for p in rows]


@router.post("/wlan/profiles", status_code=201)
async def create_profile(data: ProfileIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    _check(data, None)
    if (await ctx.db.execute(select(WlanProfile).where(WlanProfile.slug == data.slug))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Kürzel bereits vergeben")
    p = WlanProfile(tenant_id=tenant_id, created_by=ctx.user.email, version=1)
    _apply_fields(p, data)
    ctx.db.add(p)
    await ctx.db.flush()
    await ctx.audit("wlan.profile.create", target_type="wlan_profile", target_id=p.id,
                    details=data.model_dump(mode="json", exclude={"passphrase", "radius_secret"}))
    await ctx.db.commit()
    return await _out(ctx, p)


@router.put("/wlan/profiles/{profile_id}")
async def update_profile(profile_id: uuid.UUID, data: ProfileIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    if data.slug != p.slug:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Das Kürzel ist Teil der Router-Objektnamen und kann nicht geändert werden")
    _check(data, p)
    _apply_fields(p, data)
    p.version += 1
    await ctx.audit("wlan.profile.update", target_type="wlan_profile", target_id=p.id,
                    details={**data.model_dump(mode="json", exclude={"passphrase", "radius_secret"}),
                             "passphrase_changed": bool(data.passphrase), "radius_secret_changed": bool(data.radius_secret)})
    await ctx.db.commit()
    return await _out(ctx, p)


@router.delete("/wlan/profiles/{profile_id}", status_code=204, response_model=None)
async def delete_profile(profile_id: uuid.UUID, bg: BackgroundTasks, ctx: Ctx = AdminCtx) -> None:
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    devs = {st.device_id for st in (await ctx.db.execute(select(WlanDeviceState).where(WlanDeviceState.profile_id == p.id))).scalars()}
    await ctx.audit("wlan.profile.delete", target_type="wlan_profile", target_id=p.id, details={"name": p.name, "devices": len(devs)})
    await ctx.db.delete(p)
    await ctx.db.commit()
    if devs:  # WLAN von den Geräten entfernen, auf denen es ausgerollt war
        bg.add_task(wl.apply_devices_bg, p.tenant_id, list(devs))


@router.put("/wlan/profiles/{profile_id}/assignments")
async def set_assignments(profile_id: uuid.UUID, data: list[AssignmentIn], ctx: Ctx = TechCtx) -> dict[str, Any]:
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    for a in (await ctx.db.execute(select(WlanAssignment).where(WlanAssignment.profile_id == p.id))).scalars():
        await ctx.db.delete(a)
    for a in data:
        ctx.db.add(WlanAssignment(tenant_id=p.tenant_id, profile_id=p.id, mode=a.mode,
                                  targets={"device_ids": [str(x) for x in a.device_ids], "site_ids": [str(x) for x in a.site_ids], "tags": a.tags}))
    await ctx.db.flush()
    await ctx.audit("wlan.profile.assign", target_type="wlan_profile", target_id=p.id, details={"assignments": [a.model_dump(mode="json") for a in data]})
    await ctx.db.commit()
    return await _out(ctx, p)


@router.post("/wlan/profiles/{profile_id}/apply", status_code=202)
async def apply_profile(profile_id: uuid.UUID, bg: BackgroundTasks, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Auf alle Zielgeräte ausrollen; Geräte, von denen das Profil entfernt wurde, werden bereinigt."""
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    devs = {d.id: d.name for d, _ in await wl.profile_devices(ctx.db, p)}
    for st, d in (await ctx.db.execute(select(WlanDeviceState, Device).join(Device, Device.id == WlanDeviceState.device_id)
                                       .where(WlanDeviceState.profile_id == p.id))).all():
        devs.setdefault(d.id, d.name)
    await ctx.audit("wlan.profile.apply", target_type="wlan_profile", target_id=p.id, details={"version": p.version, "devices": sorted(devs.values())})
    await ctx.db.commit()
    bg.add_task(wl.apply_devices_bg, p.tenant_id, list(devs))
    return {"devices": sorted(devs.values())}


@router.post("/wlan/profiles/{profile_id}/rotate-psk")
async def rotate(profile_id: uuid.UUID, bg: BackgroundTasks, ctx: Ctx = TechCtx) -> dict[str, Any]:
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    if not p.security.endswith("-psk"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Nur für PSK-WLANs")
    psk = await wl.rotate_psk(p)
    await ctx.audit("wlan.psk.rotate", target_type="wlan_profile", target_id=p.id, details={"auto": False, "version": p.version})
    await ctx.db.commit()
    devs = [d.id for d, _ in await wl.profile_devices(ctx.db, p)]
    bg.add_task(wl.apply_devices_bg, p.tenant_id, devs)
    return {"passphrase": psk, "version": p.version, "devices": len(devs)}


@router.get("/wlan/profiles/{profile_id}/credentials")
async def credentials(profile_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Zugangsdaten für Aushang/QR (Techniker+, protokolliert)."""
    p = await get_or_404(ctx.db, WlanProfile, profile_id, "WLAN-Profil")
    psk = decrypt_secret(p.passphrase_enc) if p.passphrase_enc and p.security.endswith("-psk") else None
    if psk is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nur für PSK-WLANs")
    await ctx.audit("wlan.credentials.view", target_type="wlan_profile", target_id=p.id, details={"name": p.name})
    await ctx.db.commit()
    payload = wl.wifi_qr_payload(p.ssid, psk, p.hidden, p.security)
    return {"name": p.name, "ssid": p.ssid, "passphrase": psk, "hidden": p.hidden, "is_guest": p.is_guest,
            "rotated_at": p.psk_rotated_at, "qr_svg": wl.qr_svg(payload)}


# ----------------------------------------------------------------------------- Gerät
@router.get("/devices/{device_id}/wlan")
async def device_wlan(device_id: uuid.UUID, ctx: Ctx = ReadCtx, live: bool = True) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    states = (await ctx.db.execute(select(WlanDeviceState, WlanProfile).join(WlanProfile, WlanProfile.id == WlanDeviceState.profile_id)
                                   .where(WlanDeviceState.device_id == dev.id))).all()
    items = await wl.device_items(ctx.db, dev)
    st_by = {p.id: st for st, p in states}
    profiles = [{"id": str(p.id), "name": p.name, "ssid": p.ssid, "mode": m, "version": p.version,
                 "status": st_by[p.id].status if p.id in st_by else "pending", "error": st_by[p.id].error if p.id in st_by else None,
                 "applied_version": st_by[p.id].applied_version if p.id in st_by else None,
                 "detail": st_by[p.id].detail if p.id in st_by else {}} for p, m in items]
    out: dict[str, Any] = {"facts": (dev.facts or {}).get("wlan"), "profiles": profiles, "live": None, "live_error": None}
    if live and dev.status != DeviceStatus.offline and dev.api_password_enc:
        try:
            async with connect_device(dev) as api:
                out["live"] = await wl.live_status(api)
        except RouterOSError as exc:
            out["live_error"] = str(exc)
    return out


@router.post("/devices/{device_id}/wlan/apply")
async def device_wlan_apply(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    res = await wl.apply_device(ctx.db, dev)
    await ctx.audit("wlan.device.apply", target_type="device", target_id=dev.id, details={"status": res.get("status"), "stats": res.get("stats")})
    await ctx.db.commit()
    return {"status": res.get("status"), "stats": res.get("stats"), "error": res.get("error")}
