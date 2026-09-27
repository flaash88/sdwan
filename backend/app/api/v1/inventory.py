"""Inventar und EOL-Liste (Phase 25)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, SuperCtx, TechCtx
from app.models import Device, DeviceInventory, EolModel, Tenant
from app.services import inventory as inv_svc

router = APIRouter(tags=["inventory"])


@router.get("/inventory")
async def list_inventory(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    ctx.require_tenant()
    return await inv_svc.rows(ctx.db)


@router.get("/inventory.csv")
async def export_inventory(ctx: Ctx = ReadCtx) -> Response:
    tid = ctx.require_tenant()
    tenant = await ctx.db.get(Tenant, tid)
    body = inv_svc.to_csv(await inv_svc.rows(ctx.db))
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="inventar-{tenant.slug if tenant else "mandant"}.csv"'})


class InventoryIn(BaseModel):
    purchase_date: dt.date | None = None
    warranty_until: dt.date | None = None
    supplier: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


@router.put("/devices/{device_id}/inventory")
async def put_inventory(device_id: uuid.UUID, data: InventoryIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if data.purchase_date and data.warranty_until and data.warranty_until < data.purchase_date:
        raise HTTPException(422, "Garantie-Ende liegt vor dem Kaufdatum")
    row = (await ctx.db.execute(select(DeviceInventory).where(DeviceInventory.device_id == dev.id))).scalar_one_or_none()
    if row is None:
        row = DeviceInventory(tenant_id=dev.tenant_id, device_id=dev.id)
        ctx.db.add(row)
    for k, v in data.model_dump().items():
        setattr(row, k, v.strip() if isinstance(v, str) else v)
    row.updated_at = utcnow()
    await ctx.audit("inventory.update", target_type="device", target_id=dev.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return next(r for r in await inv_svc.rows(ctx.db) if r["device_id"] == str(dev.id))


# ----------------------------------------------------------------------------- EOL-Liste (global, MSP)
@router.get("/eol-models")
async def list_eol(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    return [inv_svc.eol_out(e) for e in (await ctx.db.execute(select(EolModel).order_by(EolModel.model))).scalars()]


class EolIn(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    status: Literal["end_of_sale", "eol"] = "eol"
    since: dt.date | None = None
    successor: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    link: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    enabled: bool = True


@router.post("/eol-models", status_code=201)
async def create_eol(data: EolIn, ctx: Ctx = SuperCtx) -> dict[str, Any]:
    e = EolModel(**data.model_dump())
    ctx.db.add(e)
    await ctx.db.flush()
    await ctx.audit("eol.create", tenant_id=None, target_type="eol_model", target_id=e.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return inv_svc.eol_out(e)


@router.put("/eol-models/{eol_id}")
async def update_eol(eol_id: uuid.UUID, data: EolIn, ctx: Ctx = SuperCtx) -> dict[str, Any]:
    e = await get_or_404(ctx.db, EolModel, eol_id, "Eintrag")
    if e.builtin:
        raise HTTPException(403, "Vordefinierter Eintrag – bitte einen eigenen anlegen")
    for k, v in data.model_dump().items():
        setattr(e, k, v)
    await ctx.audit("eol.update", tenant_id=None, target_type="eol_model", target_id=e.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return inv_svc.eol_out(e)


@router.delete("/eol-models/{eol_id}", status_code=204, response_model=None)
async def delete_eol(eol_id: uuid.UUID, ctx: Ctx = SuperCtx) -> None:
    e = await get_or_404(ctx.db, EolModel, eol_id, "Eintrag")
    if e.builtin:
        raise HTTPException(403, "Vordefinierter Eintrag kann nicht gelöscht werden")
    await ctx.audit("eol.delete", tenant_id=None, target_type="eol_model", target_id=e.id, details={"model": e.model})
    await ctx.db.delete(e)
    await ctx.db.commit()
