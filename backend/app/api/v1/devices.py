from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app import events
from app.api.v1.common import apply_update, get_or_404
from app.config import get_settings
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceStatus, PairingStatus, Site
from app.schemas import DeviceCreate, DeviceCreated, DeviceOut, DeviceUpdate, PairIn, PairingInfo
from app.services.pairing import PairingError, complete_pairing, issue_pairing_token
from app.services.wireguard import allocate_tunnel_ip

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    ctx: Ctx = ReadCtx,
    site_id: uuid.UUID | None = None,
    status_: DeviceStatus | None = Query(default=None, alias="status"),
) -> list[Device]:
    q = select(Device).order_by(Device.name)
    if site_id:
        q = q.where(Device.site_id == site_id)
    if status_:
        q = q.where(Device.status == status_)
    return list((await ctx.db.execute(q)).scalars())


@router.post("", response_model=DeviceCreated, status_code=201)
async def create_device(data: DeviceCreate, ctx: Ctx = TechCtx) -> DeviceCreated:
    tenant_id = ctx.require_tenant()
    if data.site_id:
        await get_or_404(ctx.db, Site, data.site_id, "Site")
    device = Device(tenant_id=tenant_id, tunnel_ip=await allocate_tunnel_ip(ctx.db), **data.model_dump())
    pairing = issue_pairing_token(device)
    ctx.db.add(device)
    await ctx.db.flush()
    await ctx.audit("device.create", target_type="device", target_id=device.id,
                    details={"name": device.name, "tunnel_ip": device.tunnel_ip, "serial": device.serial})
    await ctx.db.commit()
    return DeviceCreated(device=DeviceOut.model_validate(device), pairing=pairing)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> Device:
    return await get_or_404(ctx.db, Device, device_id, "Device")


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(device_id: uuid.UUID, data: DeviceUpdate, ctx: Ctx = TechCtx) -> Device:
    device = await get_or_404(ctx.db, Device, device_id, "Device")
    payload = data.model_dump(exclude_unset=True)
    if payload.get("site_id"):
        await get_or_404(ctx.db, Site, payload["site_id"], "Site")
    changed = apply_update(device, payload)
    await ctx.audit("device.update", target_type="device", target_id=device.id, details=changed)
    await ctx.db.commit()
    return device


@router.post("/{device_id}/pairing-token", response_model=PairingInfo)
async def regenerate_pairing_token(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> PairingInfo:
    """Neuen Onboarding-Befehl erzeugen (z. B. nach Reset oder Hardwaretausch)."""
    device = await get_or_404(ctx.db, Device, device_id, "Device")
    info = issue_pairing_token(device)
    if device.pairing_status == PairingStatus.revoked:
        device.pairing_status = PairingStatus.pending
    await ctx.audit("device.pairing_token", target_type="device", target_id=device.id)
    await ctx.db.commit()
    return info


@router.post("/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(device_id: uuid.UUID, ctx: Ctx = AdminCtx) -> Device:
    """Sperrt das Gerät: Peer wird beim nächsten Hub-Sync entfernt, API-Zugang verworfen."""
    device = await get_or_404(ctx.db, Device, device_id, "Device")
    device.pairing_status = PairingStatus.revoked
    device.wg_public_key = None
    device.api_password_enc = None
    device.pairing_token_hash = None
    device.status = DeviceStatus.unknown
    await ctx.audit("device.revoke", target_type="device", target_id=device.id)
    await ctx.db.commit()
    await events.publish(device.tenant_id, "device.status", {"id": str(device.id), "status": device.status.value})
    return device


@router.delete("/{device_id}", status_code=204, response_model=None)
async def delete_device(device_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    device = await get_or_404(ctx.db, Device, device_id, "Device")
    await ctx.audit("device.delete", target_type="device", target_id=device.id, details={"name": device.name})
    await ctx.db.delete(device)
    await ctx.db.commit()


@router.post("/{device_id}/simulate-pair", response_model=DeviceOut)
async def simulate_pair(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> Device:
    """Nur im Simulator-Modus: führt das Onboarding ohne echten Router durch."""
    if get_settings().routeros_backend != "simulator":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nur im Simulator-Modus verfügbar")
    from app.routeros.simulator import get_router

    device = await get_or_404(ctx.db, Device, device_id, "Device")
    if device.pairing_status != PairingStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gerät ist nicht im Status 'pending'")
    token = issue_pairing_token(device).token
    await ctx.db.flush()
    router_ = get_router(device.tunnel_ip)
    wg = next(r for r in router_.tables["/interface/wireguard"] if r["name"] == get_settings().wg_device_interface)
    res = await router_call_resource(router_)
    try:
        dev, _ = await complete_pairing(
            ctx.db,
            PairIn(token=token, public_key=wg["public-key"], serial=device.serial or res["serial"],
                   routeros_version=res["version"], model=res["board"], architecture="arm64", identity=router_.identity),
            ip=ctx.ip,
        )
    except PairingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return dev


async def router_call_resource(router_) -> dict:
    res = router_.call("/system/resource/print", {})[0]
    rb = router_.call("/system/routerboard/print", {})[0]
    return {"version": res["version"], "board": res["board-name"], "serial": rb["serial-number"]}


@router.get("/{device_id}/interfaces")
async def device_interfaces(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict]:
    """Interfaces des Geräts inkl. Aliasnamen (Kommentar / umbenannter Port).

    Live vom Router, bei Nichterreichbarkeit aus dem letzten Poll.
    """
    from app.routeros import RouterOSError, connect_device

    device = await get_or_404(ctx.db, Device, device_id, "Device")
    rows: list[dict] = []
    try:
        async with connect_device(device) as api:
            for r in await api.print("/interface"):
                rows.append({
                    "name": str(r.get("name")), "type": r.get("type"), "comment": str(r.get("comment") or "") or None,
                    "default_name": str(r.get("default-name") or "") or None,
                    "running": str(r.get("running", "")).lower() in ("true", "yes"),
                    "disabled": str(r.get("disabled", "")).lower() in ("true", "yes"),
                })
    except RouterOSError:
        for n, i in ((device.facts or {}).get("interfaces") or {}).items():
            rows.append({"name": n, "type": i.get("type"), "comment": i.get("comment"), "default_name": i.get("default_name"),
                         "running": bool(i.get("running")), "disabled": False})
    return sorted((r for r in rows if not r["name"].startswith("sdwan-")), key=lambda r: r["name"])
