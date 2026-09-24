from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.config import get_settings
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, PairingStatus, WanLink
from app.routeros import RouterOSError
from app.services.wan import MODES, WanError, apply_wan, test_link, validate_links

router = APIRouter(prefix="/devices/{device_id}/wan", tags=["wan"])
_NAME = re.compile(r"^[A-Za-z0-9._\-/]{1,100}$")


class WanLinkIn(BaseModel):
    slot: int | None = Field(default=None, ge=1, le=4)
    name: str = Field(min_length=1, max_length=100)
    interface: str
    gateway: str
    priority: int = Field(default=1, ge=1, le=4)
    weight: int = Field(default=1, ge=1, le=10)
    check_type: Literal["ping", "http"] = "ping"
    check_target: str
    check_interval_s: int = Field(default=10, ge=1, le=300)
    check_timeout_ms: int = Field(default=1000, ge=100, le=10000)
    loss_threshold_pct: int = Field(default=50, ge=1, le=100)
    latency_threshold_ms: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool = True

    @field_validator("interface")
    @classmethod
    def check_iface(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("ungültiger Interface-Name")
        return v

    @field_validator("gateway")
    @classmethod
    def check_gw(cls, v: str) -> str:
        if v.lower() == "dhcp":
            return "dhcp"
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            if _NAME.match(v):
                return v  # Interface-Gateway (PPPoE, LTE)
            raise ValueError("Gateway: IP-Adresse, Interface-Name oder 'dhcp'") from None

    @field_validator("check_target")
    @classmethod
    def check_tgt(cls, v: str) -> str:
        ip = ipaddress.ip_address(v)
        if ip in get_settings().wg_net:
            raise ValueError("Check-Ziel darf nicht im Management-Netz liegen")
        return str(ip)


class WanConfigIn(BaseModel):
    mode: str = "failover"
    recovery_delay_s: int = Field(default=30, ge=0, le=3600)
    flush_connections: bool = True
    links: list[WanLinkIn] = []
    push: bool = True


class WanLinkOut(WanLinkIn):
    id: uuid.UUID
    slot: int
    resolved_gateway: str | None
    status: str
    active: bool
    last_latency_ms: float | None
    last_loss_pct: float | None
    last_check_at: object | None
    last_change_at: object | None

    model_config = {"from_attributes": True}


async def _device(ctx: Ctx, device_id: uuid.UUID) -> Device:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return dev


def _out(dev: Device, links: list[WanLink]) -> dict:
    opts = dev.wan_options or {}
    return {
        "mode": dev.wan_mode,
        "recovery_delay_s": opts.get("recovery_delay_s", 30),
        "flush_connections": opts.get("flush_connections", True),
        "last_apply": opts.get("last_apply"),
        "last_error": opts.get("last_error"),
        "links": [WanLinkOut.model_validate(lk).model_dump(mode="json") for lk in links],
    }


async def _links(ctx: Ctx, dev: Device) -> list[WanLink]:
    return list((await ctx.db.execute(select(WanLink).where(WanLink.device_id == dev.id).order_by(WanLink.slot))).scalars())


@router.get("")
async def get_wan(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    dev = await _device(ctx, device_id)
    return _out(dev, await _links(ctx, dev))


@router.put("")
async def put_wan(device_id: uuid.UUID, data: WanConfigIn, ctx: Ctx = TechCtx) -> dict:
    dev = await _device(ctx, device_id)
    if data.mode not in MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"mode: {' | '.join(MODES)}")
    try:
        validate_links([lk.model_dump() for lk in data.links])
    except (WanError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    existing = {lk.slot: lk for lk in await _links(ctx, dev)}
    used = {lk.slot for lk in data.links if lk.slot}
    free = [s for s in (1, 2, 3, 4) if s not in used]
    keep: set[int] = set()
    for item in data.links:
        slot = item.slot or free.pop(0)
        keep.add(slot)
        lk = existing.get(slot)
        fields = item.model_dump(exclude={"slot"})
        if lk is None:
            lk = WanLink(tenant_id=dev.tenant_id, device_id=dev.id, slot=slot, **fields)
            ctx.db.add(lk)
        else:
            for k, v in fields.items():
                setattr(lk, k, v)
    for slot, lk in existing.items():
        if slot not in keep:
            await ctx.db.delete(lk)
    dev.wan_mode = data.mode
    dev.wan_options = {**(dev.wan_options or {}), "recovery_delay_s": data.recovery_delay_s, "flush_connections": data.flush_connections}
    await ctx.db.flush()
    result = None
    if data.push and dev.pairing_status == PairingStatus.paired:
        result = await _apply(ctx, dev)
    await ctx.audit("wan.update", target_type="device", target_id=dev.id,
                    details={"mode": data.mode, "links": [lk.model_dump() for lk in data.links], "pushed": bool(result and result.get("ok"))})
    await ctx.db.commit()
    return {**_out(dev, await _links(ctx, dev)), "push": result}


async def _apply(ctx: Ctx, dev: Device) -> dict:
    try:
        res = await apply_wan(ctx.db, dev)
        dev.wan_options = {**(dev.wan_options or {}), "last_error": None}
        return res
    except (RouterOSError, WanError) as exc:
        dev.wan_options = {**(dev.wan_options or {}), "last_error": str(exc)}
        return {"ok": False, "error": str(exc)}


@router.post("/apply")
async def apply(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    dev = await _device(ctx, device_id)
    res = await _apply(ctx, dev)
    await ctx.audit("wan.apply", target_type="device", target_id=dev.id, success=res.get("ok", False), details={"error": res.get("error")})
    await ctx.db.commit()
    return res


@router.post("/{slot}/test")
async def test(device_id: uuid.UUID, slot: int, ctx: Ctx = TechCtx) -> dict:
    dev = await _device(ctx, device_id)
    lk = next((x for x in await _links(ctx, dev) if x.slot == slot), None)
    if lk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WAN-Link nicht gefunden")
    try:
        return await test_link(dev, lk)
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{slot}/simulate-outage")
async def simulate_outage(device_id: uuid.UUID, slot: int, down: bool = True, ctx: Ctx = TechCtx) -> dict:
    """Nur Simulator: WAN-Ausfall simulieren (Netwatch-Ziel gilt als nicht erreichbar)."""
    if get_settings().routeros_backend != "simulator":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nur im Simulator-Modus verfügbar")
    from app.routeros.client import open_connection

    dev = await _device(ctx, device_id)
    lk = next((x for x in await _links(ctx, dev) if x.slot == slot), None)
    if lk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WAN-Link nicht gefunden")
    conn = await open_connection(dev.tunnel_ip, "", "")
    router_ = conn.router  # type: ignore[attr-defined]
    (router_.down_hosts.add if down else router_.down_hosts.discard)(lk.check_target)
    await conn.close()
    await ctx.audit("wan.simulate_outage", target_type="device", target_id=dev.id, details={"slot": slot, "down": down})
    await ctx.db.commit()
    return {"slot": slot, "down": down}
