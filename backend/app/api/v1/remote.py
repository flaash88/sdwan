from __future__ import annotations

import ipaddress
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.config import get_settings
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, PairingStatus, RemoteSession, Role
from app.services.remote import RemoteError, close_session, open_session

router = APIRouter(tags=["remote-access"])


class SessionIn(BaseModel):
    protocol: Literal["ssh", "winbox", "webfig"] = "winbox"
    duration_minutes: int = Field(default=60, ge=5)
    reason: str | None = Field(default=None, max_length=500)
    allowed_cidr: str | None = None  # Standard: aktuelle IP des Anfragenden

    @field_validator("allowed_cidr")
    @classmethod
    def v_cidr(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_network(v, strict=False)) if v else None


def _out(s: RemoteSession, device_name: str | None = None) -> dict:
    host = get_settings().remote_proxy_host
    connect = {
        "ssh": f"ssh {s.ros_username or 'admin'}@{host} -p {s.listen_port}",
        "winbox": f"{host}:{s.listen_port}",
        "webfig": f"http://{host}:{s.listen_port}/",
    }[s.protocol]
    return {
        "id": str(s.id), "device_id": str(s.device_id), "device": device_name, "user_email": s.user_email, "protocol": s.protocol,
        "proxy_host": host, "listen_port": s.listen_port, "target_port": s.target_port, "allowed_cidr": s.allowed_cidr,
        "reason": s.reason, "username": s.ros_username, "status": s.status, "expires_at": s.expires_at, "created_at": s.created_at,
        "closed_at": s.closed_at, "closed_by": s.closed_by, "connections": s.connections, "bytes_in": s.bytes_in,
        "bytes_out": s.bytes_out, "last_error": s.last_error, "connect": connect,
    }


@router.post("/devices/{device_id}/remote-sessions", status_code=201)
async def create(device_id: uuid.UUID, data: SessionIn, ctx: Ctx = TechCtx) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if dev.pairing_status != PairingStatus.paired:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gerät ist nicht gepairt")
    maxm = get_settings().remote_session_max_minutes
    if data.duration_minutes > maxm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Maximal {maxm} Minuten")
    cidr = data.allowed_cidr or (f"{ctx.ip}/32" if ctx.ip and ":" not in ctx.ip else (f"{ctx.ip}/128" if ctx.ip else None))
    if not cidr:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quell-IP unbekannt – allowed_cidr angeben")
    try:
        sess, password = await open_session(ctx.db, dev, ctx.user, data.protocol, data.duration_minutes, cidr, data.reason)
    except RemoteError as exc:
        await ctx.audit("remote.open_failed", target_type="device", target_id=dev.id, success=False, details={"error": str(exc), "protocol": data.protocol})
        await ctx.db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await ctx.audit("remote.open", target_type="device", target_id=dev.id, details={
        "session": str(sess.id), "protocol": sess.protocol, "port": sess.listen_port, "allowed": cidr,
        "minutes": data.duration_minutes, "reason": data.reason, "ros_user": sess.ros_username})
    await ctx.db.commit()
    return {**_out(sess, dev.name), "password": password}


@router.get("/devices/{device_id}/remote-sessions")
async def device_sessions(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = (await ctx.db.execute(select(RemoteSession).where(RemoteSession.device_id == dev.id).order_by(RemoteSession.created_at.desc()).limit(100))).scalars()
    return [_out(s, dev.name) for s in rows]


@router.get("/remote-sessions")
async def all_sessions(ctx: Ctx = ReadCtx, active_only: bool = False) -> list[dict]:
    q = select(RemoteSession, Device.name).join(Device, Device.id == RemoteSession.device_id).order_by(RemoteSession.created_at.desc()).limit(200)
    if active_only:
        q = q.where(RemoteSession.status == "active")
    return [_out(s, n) for s, n in (await ctx.db.execute(q)).all()]


@router.post("/remote-sessions/{session_id}/close")
async def close(session_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    sess = await get_or_404(ctx.db, RemoteSession, session_id, "Session")
    if sess.user_id != ctx.user.id and ctx.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur eigene Sessions (oder als Admin)")
    await close_session(ctx.db, sess, "closed", ctx.user.email)
    await ctx.audit("remote.close", target_type="device", target_id=sess.device_id, details={"session": str(sess.id)})
    await ctx.db.commit()
    return _out(sess)
