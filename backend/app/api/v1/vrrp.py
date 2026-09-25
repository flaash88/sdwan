"""VRRP-Instanzen pro Gerät (Phase 11) – analog zur WAN-API."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.config import get_settings
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, PairingStatus, VrrpInstance
from app.routeros import RouterOSError
from app.services.vrrp import VrrpError, apply_vrrp, validate_set

router = APIRouter(prefix="/devices/{device_id}/vrrp", tags=["vrrp"])


class VrrpIn(BaseModel):
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=40)
    interface: str
    vrid: int = Field(ge=1, le=255)
    priority: int = Field(default=100, ge=1, le=254)
    interval_ms: int = Field(default=1000, ge=10, le=255000)
    preemption: bool = True
    version: int = Field(default=3, ge=2, le=3)
    vip: str
    local_address: str | None = None
    linked_wan_slot: int | None = Field(default=None, ge=1, le=4)
    enabled: bool = True


class VrrpConfigIn(BaseModel):
    instances: list[VrrpIn] = []
    push: bool = True


FIELDS = ("name", "interface", "vrid", "priority", "interval_ms", "preemption", "version", "vip", "local_address", "linked_wan_slot", "enabled")


def _out(i: VrrpInstance) -> dict:
    return {"id": str(i.id), **{k: getattr(i, k) for k in FIELDS}, "state": i.state,
            "last_change_at": i.last_change_at.isoformat() if isinstance(i.last_change_at, dt.datetime) else None}


async def _instances(ctx: Ctx, dev: Device) -> list[VrrpInstance]:
    return list((await ctx.db.execute(select(VrrpInstance).where(VrrpInstance.device_id == dev.id).order_by(VrrpInstance.name))).scalars())


async def _apply(ctx: Ctx, dev: Device) -> dict:
    try:
        res = await apply_vrrp(ctx.db, dev)
        dev.facts = {**(dev.facts or {}), "vrrp_last_error": None}
        return res
    except RouterOSError as exc:
        dev.facts = {**(dev.facts or {}), "vrrp_last_error": str(exc)}
        return {"ok": False, "error": str(exc)}


@router.get("")
async def get_vrrp(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return {"instances": [_out(i) for i in await _instances(ctx, dev)], "last_error": (dev.facts or {}).get("vrrp_last_error")}


@router.put("")
async def put_vrrp(device_id: uuid.UUID, data: VrrpConfigIn, ctx: Ctx = TechCtx) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        items = validate_set([i.model_dump() for i in data.instances])
    except VrrpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    existing = {i.id: i for i in await _instances(ctx, dev)}
    keep: set[uuid.UUID] = set()
    for item in items:
        inst = existing.get(item["id"]) if item.get("id") else None
        if inst is None:  # ohne ID: bestehende Instanz gleichen Namens übernehmen (statt Unique-Verletzung)
            inst = next((i for i in existing.values() if i.name == item["name"] and i.id not in keep), None)
        fields = {k: item[k] for k in FIELDS}
        if inst is None:
            inst = VrrpInstance(tenant_id=dev.tenant_id, device_id=dev.id, **fields)
            ctx.db.add(inst)
        else:
            for k, v in fields.items():
                setattr(inst, k, v)
            keep.add(inst.id)
    for iid, inst in existing.items():
        if iid not in keep:
            await ctx.db.delete(inst)
    await ctx.db.flush()
    result = await _apply(ctx, dev) if data.push and dev.pairing_status == PairingStatus.paired else None
    await ctx.audit("vrrp.update", target_type="device", target_id=dev.id,
                    details={"instances": [{k: v for k, v in i.items() if k != "id"} for i in items], "pushed": bool(result and result.get("ok"))})
    await ctx.db.commit()
    return {"instances": [_out(i) for i in await _instances(ctx, dev)], "push": result}


@router.post("/apply")
async def apply(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    res = await _apply(ctx, dev)
    await ctx.audit("vrrp.apply", target_type="device", target_id=dev.id, success=res.get("ok", False), details={"error": res.get("error")})
    await ctx.db.commit()
    return res


@router.post("/{instance_id}/simulate")
async def simulate(device_id: uuid.UUID, instance_id: uuid.UUID, master: bool = True, ctx: Ctx = TechCtx) -> dict:
    """Nur Simulator: „Master werden“ (bzw. zurück auf Backup) inkl. on-master/on-backup-Script."""
    if get_settings().routeros_backend != "simulator":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nur im Simulator-Modus verfügbar")
    from app.routeros.client import open_connection

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    inst = await get_or_404(ctx.db, VrrpInstance, instance_id, "VRRP-Instanz")
    if inst.device_id != dev.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "VRRP-Instanz nicht gefunden")
    conn = await open_connection(dev.tunnel_ip, "", "")
    try:
        conn.router.set_vrrp_master(inst.name, master)  # type: ignore[attr-defined]
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{exc} – erst anwenden") from exc
    finally:
        await conn.close()
    await ctx.audit("vrrp.simulate", target_type="device", target_id=dev.id, details={"instance": inst.name, "master": master})
    await ctx.db.commit()
    return {"name": inst.name, "master": master}
