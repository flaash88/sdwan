from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, PairingStatus, ProvisioningTemplate, Site
from app.routeros.naming import routeros_safe_name
from app.schemas import DeviceOut
from app.services.pairing import issue_pairing_token
from app.services.ztp_import import stage_device
from app.config import get_settings
from app.services.ztp import TemplateError, bootstrap_script, validate_template

router = APIRouter(tags=["zero-touch"])


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    content: dict[str, Any] = {}


class StageDevice(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    serial: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9\-]+$")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        from app.routeros.naming import validate_label

        return validate_label(v, "Gerätename", 200)
    site_id: uuid.UUID | None = None
    tags: list[str] = []
    # Gerätespezifische lokale Adresse für VRRP-Instanzen aus dem Template (z. B. 192.168.110.21/24)
    vrrp_local_address: str | None = None


class StageIn(BaseModel):
    template_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    ttl_days: int = Field(default_factory=lambda: get_settings().ztp_token_ttl_days, ge=1, le=730)
    devices: list[StageDevice] = Field(min_length=1, max_length=500)


def _tpl_out(t: ProvisioningTemplate) -> dict:
    return {"id": str(t.id), "name": t.name, "description": t.description, "content": t.content, "updated_at": t.updated_at, "created_at": t.created_at}


def _validate(content: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_template(content)
    except TemplateError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/ztp/templates")
async def list_templates(ctx: Ctx = ReadCtx) -> list[dict]:
    return [_tpl_out(t) for t in (await ctx.db.execute(select(ProvisioningTemplate).order_by(ProvisioningTemplate.name))).scalars()]


@router.post("/ztp/templates", status_code=201)
async def create_template(data: TemplateIn, ctx: Ctx = AdminCtx) -> dict:
    t = ProvisioningTemplate(tenant_id=ctx.require_tenant(), name=data.name, description=data.description,
                             content=_validate(data.content), updated_at=utcnow())
    ctx.db.add(t)
    await ctx.db.flush()
    await ctx.audit("ztp.template.create", target_type="ztp_template", target_id=t.id, details={"name": t.name})
    await ctx.db.commit()
    return _tpl_out(t)


@router.get("/ztp/templates/{template_id}")
async def get_template(template_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    return _tpl_out(await get_or_404(ctx.db, ProvisioningTemplate, template_id, "Template"))


@router.put("/ztp/templates/{template_id}")
async def update_template(template_id: uuid.UUID, data: TemplateIn, ctx: Ctx = AdminCtx) -> dict:
    t = await get_or_404(ctx.db, ProvisioningTemplate, template_id, "Template")
    t.name, t.description, t.content, t.updated_at = data.name, data.description, _validate(data.content), utcnow()
    await ctx.audit("ztp.template.update", target_type="ztp_template", target_id=t.id, details={"name": t.name})
    await ctx.db.commit()
    return _tpl_out(t)


@router.delete("/ztp/templates/{template_id}", status_code=204, response_model=None)
async def delete_template(template_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    t = await get_or_404(ctx.db, ProvisioningTemplate, template_id, "Template")
    await ctx.audit("ztp.template.delete", target_type="ztp_template", target_id=t.id, details={"name": t.name})
    await ctx.db.delete(t)
    await ctx.db.commit()


@router.post("/ztp/stage", status_code=201)
async def stage(data: StageIn, ctx: Ctx = TechCtx) -> list[dict]:
    """Legt Geräte für den Versand an: seriengebundener Langzeit-Token + Bootstrap-Script je Gerät."""
    tenant_id = ctx.require_tenant()
    template = await get_or_404(ctx.db, ProvisioningTemplate, data.template_id, "Template") if data.template_id else None
    serials = [d.serial.upper() for d in data.devices]
    if len(set(serials)) != len(serials):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Seriennummern doppelt")
    dupes = (await ctx.db.execute(select(Device.serial).where(Device.serial.in_(serials)).execution_options(skip_tenant_filter=True))).scalars().all()
    if dupes:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Seriennummer bereits registriert: {', '.join(dupes)}")
    out = []
    for item in data.devices:
        site_id = item.site_id or data.site_id
        if site_id:
            await get_or_404(ctx.db, Site, site_id, "Site")
        if item.vrrp_local_address and template and (template.content or {}).get("vrrp"):
            from app.services.vrrp import VrrpError, validate_set

            try:
                validate_set([{**i, "local_address": i.get("local_address") or item.vrrp_local_address} for i in template.content["vrrp"]])
            except VrrpError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{item.name}: {exc}") from exc
        out.append(await stage_device(ctx.db, tenant_id, name=item.name, serial=item.serial, site_id=site_id, tags=item.tags,
                                      template=template, ttl_days=data.ttl_days, by=ctx.user.email, vrrp_local_address=item.vrrp_local_address))
    await ctx.audit("ztp.stage", details={"count": len(out), "serials": serials, "template": str(template.id) if template else None})
    await ctx.db.commit()
    return out


@router.post("/devices/{device_id}/ztp/bootstrap", response_class=PlainTextResponse)
async def regenerate_bootstrap(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> PlainTextResponse:
    """Neues Bootstrap-Script (neuer Token – der alte wird ungültig)."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if dev.pairing_status == PairingStatus.paired:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gerät ist bereits gepairt")
    if not dev.serial:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Zero-Touch erfordert eine Seriennummer")
    template = await ctx.db.get(ProvisioningTemplate, dev.ztp_template_id) if dev.ztp_template_id else None
    info = issue_pairing_token(dev, ttl_hours=24 * get_settings().ztp_token_ttl_days)
    dev.ztp_state = "staged"
    await ctx.audit("ztp.bootstrap", target_type="device", target_id=dev.id)
    await ctx.db.commit()
    return PlainTextResponse(bootstrap_script(info.token, dev, template), headers={"Content-Disposition": f'attachment; filename="sdwan-ztp-{routeros_safe_name(dev.serial or "geraet")}.rsc"'})


@router.get("/ztp/devices")
async def ztp_devices(ctx: Ctx = ReadCtx) -> list[dict]:
    rows = (await ctx.db.execute(select(Device).where(Device.ztp_state != "none").order_by(Device.created_at.desc()))).scalars()
    return [DeviceOut.model_validate(d).model_dump(mode="json") for d in rows]




# ----------------------------------------------------------------------------- CSV-Massenimport (Phase 25)
class ImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=500_000)
    ttl_days: int = Field(default_factory=lambda: get_settings().ztp_token_ttl_days, ge=1, le=730)


@router.post("/ztp/import/preview")
async def import_preview(data: ImportIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Nur prüfen, nichts anlegen: je Zeile Ergebnis und Fehler."""
    from app.services import ztp_import

    try:
        rows = await ztp_import.validate(ctx.db, ctx.require_tenant(), data.csv)
    except ztp_import.ImportError_ as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"rows": rows, "valid": sum(1 for r in rows if r["ok"]), "invalid": sum(1 for r in rows if not r["ok"])}


class ImportCommitIn(ImportIn):
    confirm: bool = False


@router.post("/ztp/import/commit", status_code=201)
async def import_commit(data: ImportCommitIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Nach Bestätigung: gültige Zeilen anlegen (erneut geprüft), fehlerhafte bleiben unberücksichtigt."""
    from app.services import ztp_import

    if not data.confirm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Bestätigung fehlt")
    tenant_id = ctx.require_tenant()
    try:
        rows = await ztp_import.validate(ctx.db, tenant_id, data.csv)
    except ztp_import.ImportError_ as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    created = []
    for r in rows:
        if not r["ok"]:
            continue
        tpl = await ctx.db.get(ProvisioningTemplate, uuid.UUID(r["template_id"])) if r["template_id"] else None
        created.append(await stage_device(ctx.db, tenant_id, name=r["name"], serial=r["serial"],
                                          site_id=uuid.UUID(r["site_id"]) if r["site_id"] else None, tags=r["tags"], template=tpl,
                                          ttl_days=data.ttl_days, by=ctx.user.email, vrrp_local_address=r["vrrp_local_address"], model=r["model"]))
    skipped = [{"line": r["line"], "serial": r["serial"], "errors": r["errors"]} for r in rows if not r["ok"]]
    await ctx.audit("ztp.import", details={"created": len(created), "skipped": skipped, "serials": [c["device"]["serial"] for c in created]})
    await ctx.db.commit()
    return {"created": created, "skipped": skipped}
