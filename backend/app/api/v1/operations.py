"""Betrieb (Phase 18): Wartungsfenster, Speedtest, Syslog."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import system_session, utcnow
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceSyslog, MaintenanceWindow, Site, SpeedtestResult, SyslogMessage, Tenant, WanLink
from app.routeros import RouterOSError, connect_device
from app.services import speedtest as st
from app.services import syslog as sl
from app.services.maintenance import MaintenanceError, active_windows, is_active, validate

router = APIRouter(tags=["operations"])


# ----------------------------------------------------------------------------- Wartungsfenster
class WindowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    site_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    kind: Literal["once", "weekly"] = "once"
    start_at: dt.datetime | None = None
    weekdays: list[int] = []
    start_time: str | None = None
    duration_min: int = Field(default=60, ge=1, le=10080)
    suppress_alerts: bool = True
    firmware_allowed: bool = True
    enabled: bool = True
    note: str | None = Field(default=None, max_length=1000)


def _w_out(w: MaintenanceWindow, active: bool) -> dict[str, Any]:
    return {"id": str(w.id), "name": w.name, "site_id": str(w.site_id) if w.site_id else None, "device_id": str(w.device_id) if w.device_id else None,
            "kind": w.kind, "start_at": w.start_at, "weekdays": w.weekdays or [], "start_time": w.start_time, "duration_min": w.duration_min,
            "suppress_alerts": w.suppress_alerts, "firmware_allowed": w.firmware_allowed, "enabled": w.enabled, "note": w.note,
            "created_by": w.created_by, "active": active}


async def _check_scope(ctx: Ctx, data: WindowIn) -> None:
    if data.site_id and data.device_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Entweder Standort oder Gerät (oder keines = ganzer Mandant)")
    if data.site_id:
        await get_or_404(ctx.db, Site, data.site_id, "Standort")
    if data.device_id:
        await get_or_404(ctx.db, Device, data.device_id, "Device")
    try:
        validate(data.kind, data.start_at, data.weekdays, data.start_time, data.duration_min)
    except MaintenanceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/maintenance-windows")
async def list_windows(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    tenant = await ctx.db.get(Tenant, ctx.require_tenant())
    rows = (await ctx.db.execute(select(MaintenanceWindow).order_by(MaintenanceWindow.name))).scalars().all()
    now = utcnow()
    return [_w_out(w, is_active(w, now, tenant.timezone if tenant else None)) for w in rows]


@router.post("/maintenance-windows", status_code=201)
async def create_window(data: WindowIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    await _check_scope(ctx, data)
    w = MaintenanceWindow(tenant_id=tenant_id, created_by=ctx.user.email, **data.model_dump())
    ctx.db.add(w)
    await ctx.db.flush()
    await ctx.audit("maintenance.create", target_type="maintenance_window", target_id=w.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    tenant = await ctx.db.get(Tenant, tenant_id)
    return _w_out(w, is_active(w, utcnow(), tenant.timezone if tenant else None))


@router.put("/maintenance-windows/{window_id}")
async def update_window(window_id: uuid.UUID, data: WindowIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    w = await get_or_404(ctx.db, MaintenanceWindow, window_id, "Wartungsfenster")
    await _check_scope(ctx, data)
    for k, v in data.model_dump().items():
        setattr(w, k, v)
    await ctx.audit("maintenance.update", target_type="maintenance_window", target_id=w.id, details=data.model_dump(mode="json"))
    await ctx.db.commit()
    tenant = await ctx.db.get(Tenant, w.tenant_id)
    return _w_out(w, is_active(w, utcnow(), tenant.timezone if tenant else None))


@router.delete("/maintenance-windows/{window_id}", status_code=204, response_model=None)
async def delete_window(window_id: uuid.UUID, ctx: Ctx = TechCtx) -> None:
    w = await get_or_404(ctx.db, MaintenanceWindow, window_id, "Wartungsfenster")
    await ctx.audit("maintenance.delete", target_type="maintenance_window", target_id=w.id, details={"name": w.name})
    await ctx.db.delete(w)
    await ctx.db.commit()


@router.get("/devices/{device_id}/maintenance")
async def device_maintenance(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    from app.services.maintenance import window_for

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    tenant = await ctx.db.get(Tenant, dev.tenant_id)
    w = window_for(await active_windows(ctx.db, tenant, utcnow()), dev) if tenant else None
    return {"active": w is not None, "window": w.name if w else None}


# ----------------------------------------------------------------------------- Speedtest
def _st_out(r: SpeedtestResult) -> dict[str, Any]:
    return {"id": str(r.id), "wan_link_id": str(r.wan_link_id) if r.wan_link_id else None, "slot": r.slot, "status": r.status,
            "down_mbps": r.down_mbps, "up_mbps": r.up_mbps, "duration_s": r.duration_s, "bytes_estimated": r.bytes_estimated,
            "error": r.error, "triggered_by": r.triggered_by, "created_at": r.created_at, "finished_at": r.finished_at}


async def _link(ctx: Ctx, device_id: uuid.UUID, link_id: uuid.UUID) -> tuple[Device, WanLink]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    link = await get_or_404(ctx.db, WanLink, link_id, "WAN")
    if link.device_id != dev.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WAN nicht gefunden")
    return dev, link


@router.get("/devices/{device_id}/speedtests")
async def list_speedtests(device_id: uuid.UUID, ctx: Ctx = ReadCtx, limit: int = Query(default=50, le=500)) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = (await ctx.db.execute(select(SpeedtestResult).where(SpeedtestResult.device_id == dev.id)
                                 .order_by(SpeedtestResult.created_at.desc()).limit(limit))).scalars().all()
    links = (await ctx.db.execute(select(WanLink).where(WanLink.device_id == dev.id))).scalars().all()
    return {"enabled": st.enabled(), "results": [_st_out(r) for r in rows],
            "weekly": {str(lk.id): lk.speedtest_weekly for lk in links}}


@router.get("/devices/{device_id}/wan/{link_id}/speedtest/estimate")
async def speedtest_estimate(device_id: uuid.UUID, link_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    _dev, link = await _link(ctx, device_id, link_id)
    return {"enabled": st.enabled(), **await st.estimate(ctx.db, link)}


class SpeedtestIn(BaseModel):
    confirm_volume: bool = False  # bei WAN mit Volumenlimit Pflicht


async def _run_bg(device_id: uuid.UUID, link_id: uuid.UUID, by: str, result_id: uuid.UUID) -> None:
    async with system_session() as db:
        dev, link = await db.get(Device, device_id), await db.get(WanLink, link_id)
        placeholder = await db.get(SpeedtestResult, result_id)
        if placeholder is not None:
            await db.delete(placeholder)
        if dev and link:
            await st.run(db, dev, link, by)
        await db.commit()


@router.post("/devices/{device_id}/wan/{link_id}/speedtest", status_code=202)
async def speedtest_start(device_id: uuid.UUID, link_id: uuid.UUID, data: SpeedtestIn, bg: BackgroundTasks, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev, link = await _link(ctx, device_id, link_id)
    if not st.enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, "Kein Speedtest-Server konfiguriert (SPEEDTEST_SERVER)")
    est = await st.estimate(ctx.db, link)
    if est["volume_warning"] and not data.confirm_volume:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": est["volume_warning"], "estimate": est})
    # Platzhalter, damit die Oberfläche sofort „läuft“ zeigt; der Hintergrundlauf ersetzt ihn
    ph = SpeedtestResult(tenant_id=dev.tenant_id, device_id=dev.id, wan_link_id=link.id, slot=link.slot, status="running",
                         duration_s=est["duration_s"], bytes_estimated=est["bytes_estimated"], triggered_by=ctx.user.email)
    ctx.db.add(ph)
    await ctx.audit("speedtest.start", target_type="device", target_id=dev.id, details={"wan": link.name, "estimate_bytes": est["bytes_estimated"]})
    await ctx.db.commit()
    bg.add_task(_run_bg, dev.id, link.id, ctx.user.email, ph.id)
    return {"started": True, "estimate": est}


class WeeklyIn(BaseModel):
    weekly: bool


@router.put("/devices/{device_id}/wan/{link_id}/speedtest/schedule")
async def speedtest_schedule(device_id: uuid.UUID, link_id: uuid.UUID, data: WeeklyIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev, link = await _link(ctx, device_id, link_id)
    link.speedtest_weekly = data.weekly
    await ctx.audit("speedtest.schedule", target_type="device", target_id=dev.id, details={"wan": link.name, "weekly": data.weekly})
    await ctx.db.commit()
    return {"weekly": link.speedtest_weekly}


# ----------------------------------------------------------------------------- Syslog
class SyslogIn(BaseModel):
    enabled: bool
    topics: list[str] = []


@router.get("/devices/{device_id}/syslog")
async def get_syslog(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    cfg = (await ctx.db.execute(select(DeviceSyslog).where(DeviceSyslog.device_id == dev.id))).scalar_one_or_none()
    tenant = await ctx.db.get(Tenant, dev.tenant_id)
    return {"enabled": bool(cfg and cfg.enabled), "topics": (cfg.topics if cfg else None) or sl.DEFAULT_TOPICS,
            "allowed_topics": sorted(sl.ALLOWED_TOPICS), "applied_at": cfg.applied_at if cfg else None, "last_error": cfg.last_error if cfg else None,
            "retention_days": int(((tenant.settings or {}) if tenant else {}).get("syslog_retention_days") or sl.DEFAULT_RETENTION_DAYS)}


@router.put("/devices/{device_id}/syslog")
async def put_syslog(device_id: uuid.UUID, data: SyslogIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        topics = sl.validate_topics(data.topics)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    cfg = (await ctx.db.execute(select(DeviceSyslog).where(DeviceSyslog.device_id == dev.id))).scalar_one_or_none()
    if cfg is None:
        cfg = DeviceSyslog(tenant_id=dev.tenant_id, device_id=dev.id)
        ctx.db.add(cfg)
    cfg.enabled, cfg.topics = data.enabled, topics
    stats = None
    try:
        async with connect_device(dev) as api:
            stats = await sl.apply(api, dev, data.enabled, topics)
        cfg.applied_at, cfg.last_error = utcnow(), None
    except RouterOSError as exc:
        cfg.last_error = str(exc)
    await ctx.audit("syslog.config", target_type="device", target_id=dev.id, success=cfg.last_error is None,
                    details={"enabled": data.enabled, "topics": topics, "error": cfg.last_error})
    await ctx.db.commit()
    return {"enabled": cfg.enabled, "topics": cfg.topics, "applied": stats, "last_error": cfg.last_error}


class RetentionIn(BaseModel):
    days: int = Field(ge=1, le=3650)


@router.put("/syslog/retention")
async def set_retention(data: RetentionIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Mandant")
    tenant.settings = {**(tenant.settings or {}), "syslog_retention_days": data.days}
    await ctx.audit("syslog.retention", details={"days": data.days})
    await ctx.db.commit()
    return {"retention_days": data.days}


@router.get("/devices/{device_id}/logs")
async def device_logs(device_id: uuid.UUID, ctx: Ctx = ReadCtx, since: dt.datetime | None = None, until: dt.datetime | None = None,
                      around: dt.datetime | None = None, topic: str | None = Query(default=None, max_length=40),
                      q: str | None = Query(default=None, max_length=200), max_severity: int | None = Query(default=None, ge=0, le=7),
                      limit: int = Query(default=500, le=5000)) -> dict[str, Any]:
    """Syslog eines Geräts; ``around`` = ±15 Minuten um einen Zeitpunkt (Absprung aus Metriken/VRRP)."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    if around is not None:
        since, until = around - dt.timedelta(minutes=15), around + dt.timedelta(minutes=15)
    qy = select(SyslogMessage).where(SyslogMessage.device_id == dev.id)
    if since:
        qy = qy.where(SyslogMessage.received_at >= since)
    if until:
        qy = qy.where(SyslogMessage.received_at <= until)
    if topic:
        qy = qy.where(SyslogMessage.topics.ilike(f"%{topic}%"))
    if q:
        qy = qy.where(SyslogMessage.message.ilike(f"%{q}%"))
    if max_severity is not None:
        qy = qy.where(SyslogMessage.severity <= max_severity)
    rows = (await ctx.db.execute(qy.order_by(SyslogMessage.received_at.desc()).limit(limit))).scalars().all()
    return {"since": since, "until": until, "messages": [
        {"id": str(m.id), "at": m.received_at, "severity": m.severity, "topics": m.topics, "message": m.message} for m in rows]}
