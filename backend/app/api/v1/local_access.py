"""Vor-Ort-Zugang (Break-Glass, Phase 24): Status, Anlegen, Anzeige mit Begründung, Rotation, Export, Einstellungen."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import AdminCtx, Ctx, ReadCtx
from app.models import Device, DeviceStatus, LocalAccess, PairingStatus, Tenant
from app.routeros import RouterOSError, connect_device
from app.security import decrypt_secret, encrypt_secret
from app.services import local_access as la_svc
from app.services import platform_events, webhook
from app.services.targets import resolve_targets

router = APIRouter(tags=["local-access"])


# ----------------------------------------------------------------------------- Einstellungen je Mandant
class SettingsIn(BaseModel):
    local_admin_name: str = Field(default=la_svc.DEFAULT_NAME)
    local_admin_address_restrict: bool = True
    local_access_auto: bool = True
    local_access_rotate_days: int | None = Field(default=None, ge=1, le=3650)
    local_access_rotate_after_view: bool = False
    local_access_age_recipient: str = ""
    local_access_webhook_url: str | None = None  # None = unverändert, "" = entfernen


@router.get("/local-access/settings")
async def get_settings_(ctx: Ctx = ReadCtx) -> dict[str, Any]:
    t = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    st = t.settings or {}
    out = la_svc.tenant_settings(t)
    out["local_access_webhook_url"] = webhook.mask(decrypt_secret(st["local_access_webhook_url_enc"])) if st.get("local_access_webhook_url_enc") else None
    return out


@router.put("/local-access/settings")
async def put_settings(data: SettingsIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    # Export-Ziel und Schlüssel bestimmen, wohin Passwörter gehen – daher nie per API-Token
    ctx.forbid_token("Einstellungen des Vor-Ort-Zugangs zu ändern")
    t = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    if not re.match(la_svc._NAME_RE, data.local_admin_name):
        raise HTTPException(422, "Benutzername: 3–32 Zeichen, Kleinbuchstaben, Ziffern, - und _, beginnt mit einem Buchstaben")
    if data.local_admin_name in ("admin", "sdwan-api", "sdwan"):
        raise HTTPException(422, "Dieser Benutzername ist reserviert")
    if data.local_access_age_recipient:
        try:
            la_svc.encrypt_age(b"x", data.local_access_age_recipient)
        except la_svc.LocalAccessError as exc:
            raise HTTPException(422, str(exc)) from exc
    st = dict(t.settings or {})
    st.update({k: v for k, v in data.model_dump().items() if k != "local_access_webhook_url"})
    if data.local_access_webhook_url is not None:
        if data.local_access_webhook_url:
            try:
                webhook.validate_url(data.local_access_webhook_url)
            except webhook.WebhookError as exc:
                raise HTTPException(422, str(exc)) from exc
            if not data.local_access_age_recipient:
                raise HTTPException(422, "Automatischer Export nur mit age-Empfänger (nie Klartext)")
            st["local_access_webhook_url_enc"] = encrypt_secret(data.local_access_webhook_url)
        else:
            st.pop("local_access_webhook_url_enc", None)
    t.settings = st
    await ctx.audit("local_access.settings", target_type="tenant", target_id=t.id,
                    details={**data.model_dump(exclude={"local_access_webhook_url"}),
                             "webhook": webhook.mask(data.local_access_webhook_url) if data.local_access_webhook_url else data.local_access_webhook_url})
    await ctx.db.commit()
    return await get_settings_(ctx)


# ----------------------------------------------------------------------------- Übersicht / Gerät
@router.get("/local-access")
async def list_access(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    """Alle Geräte des Mandanten mit Status des Vor-Ort-Zugangs (ohne Passwörter)."""
    ctx.require_tenant()
    rows = {la.device_id: la for la in (await ctx.db.execute(select(LocalAccess))).scalars()}
    devs = (await ctx.db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired).order_by(Device.name))).scalars().all()
    return [{"device_id": str(d.id), "device": d.name, "device_status": d.status.value, "access": la_svc.out(rows.get(d.id))} for d in devs]


@router.get("/devices/{device_id}/local-access")
async def get_access(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any] | None:
    await get_or_404(ctx.db, Device, device_id, "Device")
    la = (await ctx.db.execute(select(LocalAccess).where(LocalAccess.device_id == device_id))).scalar_one_or_none()
    return la_svc.out(la)


class ServicePortIn(BaseModel):
    enabled: bool = False
    interface: str | None = None
    network: str = la_svc.DEFAULT_SP_NETWORK


class AccessIn(BaseModel):
    manual_networks: list[str] | None = None
    service_port: ServicePortIn | None = None
    # Ausdrücklich bestätigte Netze, die sich mit einem privaten (RFC1918) WAN-Netz überschneiden
    # (Doppel-NAT, VRRP-Backup mit WAN = lokales Netz). Öffentliche Netze bleiben immer verboten.
    allow_wan_networks: list[str] = []


async def _online(dev: Device) -> None:
    if dev.pairing_status != PairingStatus.paired or dev.status == DeviceStatus.offline:
        raise HTTPException(409, "Gerät ist nicht erreichbar")


async def _configure(ctx: Ctx, dev: Device, data: AccessIn) -> tuple[LocalAccess, list[dict[str, str]]]:
    """Rückgabe: (Datensatz, neu bestätigte WAN-Ausnahmen)."""
    la = await la_svc.ensure_record(ctx.db, dev)
    new_exc: list[dict[str, str]] = []
    if data.manual_networks is not None:
        before = {e["network"]: e for e in la.wan_exceptions or []}
        try:
            async with connect_device(dev) as api:
                wan = await la_svc.wan_interfaces(ctx.db, dev, api)
                wan_nets = la_svc.wan_networks(await api.print("/ip/address"), wan)
            manual, exc = la_svc.validate_manual(data.manual_networks, wan_nets, set(before) | set(data.allow_wan_networks))
        except la_svc.WanConfirmRequired as e:
            raise HTTPException(409, {"message": "Netz überschneidet sich mit einem privaten WAN-Netz – nur mit ausdrücklicher Bestätigung",
                                      "confirm_wan": e.items}) from e
        except la_svc.LocalAccessError as e:
            raise HTTPException(422, str(e)) from e
        now = utcnow().isoformat()
        la.manual_networks = manual
        la.wan_exceptions = [before.get(x["network"]) or {**x, "confirmed_by": ctx.user.email, "confirmed_at": now} for x in exc] or None
        new_exc = [x for x in la.wan_exceptions or [] if x["network"] not in before]
    if data.service_port is not None:
        sp = data.service_port
        if sp.enabled:
            if not sp.interface or not re.match(r"^[A-Za-z0-9_.-]{1,64}\Z", sp.interface):
                raise HTTPException(422, "Service-Port: Ethernet-Interface angeben")
            try:
                la_svc.sp_plan(sp.network)
            except ValueError as exc:
                raise HTTPException(422, f"Service-Port-Netz: {exc}") from exc
        old = la.service_port or {}
        if old.get("enabled") and (not sp.enabled or old.get("interface") != sp.interface):
            async with connect_device(dev) as api:
                await la_svc.remove_service_port(api, la)
        la.service_port = {**(la.service_port or {}), **sp.model_dump()}
    la.enabled = True
    return la, new_exc


@router.post("/devices/{device_id}/local-access")
async def create_access(device_id: uuid.UUID, data: AccessIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    """Vor-Ort-Zugang anlegen bzw. mit geänderten Netzen/Service-Port neu abgleichen (Benutzeraktion)."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    await _online(dev)
    try:
        la, new_exc = await _configure(ctx, dev, data)
        res = await la_svc.apply_safe(ctx.db, la, dev)
    except RouterOSError as exc:
        raise HTTPException(502, str(exc)) from exc
    for e in new_exc:
        await ctx.audit("local_access.wan_exception", target_type="device", target_id=dev.id,
                        details={"network": e["network"], "interface": e["interface"], "wan_network": e["wan_network"]})
    await ctx.audit("local_access.apply", target_type="device", target_id=dev.id, success=res["status"] == "active",
                    details={"status": res["status"], "reason": res.get("reason"), "networks": la.networks,
                             "manual_networks": la.manual_networks, "service_port": la_svc.out(la)["service_port"]})
    if res["status"] == "active":
        await la_svc.after_change(ctx.db, dev.tenant_id, "created")
    await ctx.db.commit()
    return la_svc.out(la) or {}


@router.delete("/devices/{device_id}/local-access")
async def delete_access(device_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    la = (await ctx.db.execute(select(LocalAccess).where(LocalAccess.device_id == device_id))).scalar_one_or_none()
    if la is None:
        raise HTTPException(404, "Kein Vor-Ort-Zugang")
    await _online(dev)
    try:
        await la_svc.disable(ctx.db, la, dev)
    except RouterOSError as exc:
        raise HTTPException(502, str(exc)) from exc
    await ctx.audit("local_access.disable", target_type="device", target_id=dev.id)
    await ctx.db.commit()
    return la_svc.out(la) or {}


class RevealIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


@router.post("/devices/{device_id}/local-access/reveal")
async def reveal(device_id: uuid.UUID, data: RevealIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    """Passwort anzeigen: nur Admin/MSP-Admin, nie per API-Token; Begründung Pflicht, Audit + Webhook."""
    ctx.forbid_token("Das Anzeigen von Vor-Ort-Passwörtern")
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    la = (await ctx.db.execute(select(LocalAccess).where(LocalAccess.device_id == device_id))).scalar_one_or_none()
    if la is None or not la.password_enc:
        raise HTTPException(404, "Kein Vor-Ort-Passwort gespeichert")
    tenant = await ctx.db.get(Tenant, dev.tenant_id)
    ts = la_svc.tenant_settings(tenant)
    la.viewed_at = utcnow()
    if ts["local_access_rotate_after_view"] and la.rotate_due_at is None:
        la.rotate_due_at = utcnow() + la_svc.ROTATE_AFTER_VIEW
    await ctx.audit("local_access.reveal", target_type="device", target_id=dev.id, details={"reason": data.reason, "username": la.username})
    facts = {"Gerät": dev.name, "Mandant": tenant.name if tenant else "", "Benutzer": ctx.user.email, "Begründung": data.reason}
    await platform_events.notify(ctx.db, "Vor-Ort-Passwort angezeigt", f"{ctx.user.email} hat das Vor-Ort-Passwort von {dev.name} angezeigt.",
                                 "warning", facts)
    st = (tenant.settings or {}) if tenant else {}
    if st.get("local_access_webhook_url_enc"):
        await platform_events.notify(ctx.db, "Vor-Ort-Passwort angezeigt", f"{ctx.user.email}: {dev.name}", "warning", facts,
                                     webhook_url=decrypt_secret(st["local_access_webhook_url_enc"]))
    await ctx.db.commit()
    return {"username": la.username, "password": decrypt_secret(la.password_enc), "networks": la.networks or [],
            "rotate_due_at": la.rotate_due_at}


@router.post("/devices/{device_id}/local-access/rotate")
async def rotate(device_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    la = (await ctx.db.execute(select(LocalAccess).where(LocalAccess.device_id == device_id))).scalar_one_or_none()
    if la is None:
        raise HTTPException(404, "Kein Vor-Ort-Zugang")
    await _online(dev)
    try:
        res = await la_svc.rotate(ctx.db, la, dev)
    except la_svc.LocalAccessError as exc:
        raise HTTPException(409, str(exc)) from exc
    await ctx.audit("local_access.rotate", target_type="device", target_id=dev.id, success=res["ok"], details={"error": res.get("error")})
    await ctx.db.commit()
    if not res["ok"]:
        raise HTTPException(502, f"Rotation fehlgeschlagen – das bisherige Passwort bleibt gültig: {res['error']}")
    return la_svc.out(la) or {}


class BulkIn(BaseModel):
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []


@router.post("/local-access/bulk")
async def bulk(data: BulkIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    """Massenaktion: Vor-Ort-Zugang auf den gewählten erreichbaren Geräten anlegen."""
    devs = await resolve_targets(ctx.db, data.model_dump(mode="json"), ctx.require_tenant())
    results = []
    for dev in devs:
        if dev.pairing_status != PairingStatus.paired or dev.status == DeviceStatus.offline:
            results.append({"device": dev.name, "status": "skipped", "reason": "nicht erreichbar"})
            continue
        la = await la_svc.ensure_record(ctx.db, dev)
        la.enabled = True
        res = await la_svc.apply_safe(ctx.db, la, dev)
        results.append({"device": dev.name, "device_id": str(dev.id), **res})
    await ctx.audit("local_access.bulk", details={"results": results})
    if any(r["status"] == "active" for r in results):
        await la_svc.after_change(ctx.db, ctx.require_tenant(), "created")
    await ctx.db.commit()
    return {"results": results}


class ExportIn(BaseModel):
    format: Literal["age", "zip"]
    password: str | None = None  # für ZIP (AES)
    recipient: str | None = None  # für age; leer = Mandanten-Einstellung


@router.post("/local-access/export")
async def export(data: ExportIn, ctx: Ctx = AdminCtx) -> Response:
    """KeePass-kompatible CSV, verschlüsselt (age oder AES-ZIP) – nie Klartext, nie per API-Token."""
    ctx.forbid_token("Der Export von Vor-Ort-Passwörtern")
    tid = ctx.require_tenant()
    tenant = await get_or_404(ctx.db, Tenant, tid, "Tenant")
    rows = await la_svc.export_rows(ctx.db, tid)
    csv_data = la_svc.keepass_csv(rows)
    try:
        if data.format == "age":
            rcpt = data.recipient or la_svc.tenant_settings(tenant)["local_access_age_recipient"]
            if not rcpt:
                raise la_svc.LocalAccessError("age-Empfänger (Public Key) angeben oder in den Einstellungen hinterlegen")
            body, name, mime = la_svc.encrypt_age(csv_data, rcpt), f"vor-ort-zugang-{tenant.slug}.csv.age", "application/octet-stream"
        else:
            body, name, mime = la_svc.encrypt_zip(csv_data, data.password or ""), f"vor-ort-zugang-{tenant.slug}.zip", "application/zip"
    except la_svc.LocalAccessError as exc:
        raise HTTPException(422, str(exc)) from exc
    await ctx.audit("local_access.export", target_type="tenant", target_id=tid, details={"format": data.format, "devices": len(rows)})
    await ctx.db.commit()
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})
