"""Threat-Feeds (Phase 15): Verwaltung, Zuweisung, sofortige Aktualisierung."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Device, FirewallPolicy, FwObject, ThreatFeed, ThreatFeedAssignment
from app.routeros import RouterOSError
from app.services.feeds import refresh_feed, remove_from_device, sync_assignment
from app.services.fw_compile import referenced, slugify
from app.services.targets import resolve_targets

router = APIRouter(prefix="/feeds", tags=["feeds"])


class FeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(pattern=r"^https?://", max_length=500)
    fmt: str = Field(default="lines", pattern=r"^(lines|jsonl)$")
    json_field: str = Field(default="cidr", pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    comment_chars: str = Field(default="#;", max_length=10)
    interval_min: int = Field(default=360, ge=15, le=10080)
    max_entries: int = Field(default=20000, ge=1, le=200000)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=1000)


class TargetsIn(BaseModel):
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []


def _stale(f: ThreatFeed) -> bool:
    from app.db import utcnow

    return f.last_ok_at is not None and (utcnow() - f.last_ok_at).total_seconds() > 3 * f.interval_min * 60


def _out(f: ThreatFeed, assigns: list[tuple[ThreatFeedAssignment, Device]] | None = None) -> dict[str, Any]:
    o = {
        "id": str(f.id), "name": f.name, "slug": f.slug, "url": f.url, "fmt": f.fmt, "json_field": f.json_field,
        "comment_chars": f.comment_chars, "interval_min": f.interval_min, "max_entries": f.max_entries, "enabled": f.enabled,
        "description": f.description, "builtin": f.builtin, "scope": "global" if f.tenant_id is None else "tenant",
        "list_name": f"sdwan-feed-{f.slug}", "count": len(f.entries or []), "rejected": f.rejected,
        "last_fetch_at": f.last_fetch_at, "last_ok_at": f.last_ok_at, "last_error": f.last_error, "stale": _stale(f),
    }
    if assigns is not None:
        o["assignments"] = [{"device_id": str(d.id), "device": d.name, "status": a.status, "synced_count": a.synced_count,
                             "last_sync_at": a.last_sync_at, "last_error": a.last_error,
                             "current": a.synced_hash == f.entries_hash} for a, d in assigns]
    return o


async def _assigns(ctx: Ctx, f: ThreatFeed) -> list[tuple[ThreatFeedAssignment, Device]]:
    rows = await ctx.db.execute(select(ThreatFeedAssignment, Device).join(Device, Device.id == ThreatFeedAssignment.device_id)
                                .where(ThreatFeedAssignment.feed_id == f.id).order_by(Device.name))
    return [(a, d) for a, d in rows.all()]


def _can_edit(ctx: Ctx, f: ThreatFeed) -> None:
    if f.tenant_id is None and not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Feeds pflegt nur der MSP")


@router.get("")
async def list_feeds(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    feeds = (await ctx.db.execute(select(ThreatFeed).order_by(ThreatFeed.name))).scalars().all()
    return [_out(f, await _assigns(ctx, f)) for f in feeds]


@router.get("/{feed_id}")
async def get_feed(feed_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    return {**_out(f, await _assigns(ctx, f)), "sample": (f.entries or [])[:50]}


@router.post("", status_code=201)
async def create_feed(data: FeedIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    slug = slugify(data.name)[:30]
    if (await ctx.db.execute(select(ThreatFeed).where(ThreatFeed.slug == slug))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Kürzel {slug} ist bereits vergeben – anderen Namen wählen")
    f = ThreatFeed(tenant_id=ctx.tenant_id, slug=slug, entries=[], **data.model_dump())
    ctx.db.add(f)
    # Objekt für den Firewall-Editor im selben Geltungsbereich
    ctx.db.add(FwObject(tenant_id=ctx.tenant_id, name=f"Threat-Feed: {data.name}", slug=slug, kind="feed", values=[], members=[],
                        description=data.description, builtin=False))
    await ctx.db.flush()
    await ctx.audit("feed.create", target_type="feed", target_id=f.id, details={"name": f.name, "url": f.url})
    await ctx.db.commit()
    return _out(f, [])


@router.patch("/{feed_id}")
async def update_feed(feed_id: uuid.UUID, data: FeedIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    _can_edit(ctx, f)
    fields = data.model_dump(exclude={"name"}) if f.builtin else data.model_dump()
    if f.builtin:  # vordefiniert: nur Betriebsparameter änderbar
        fields = {k: v for k, v in fields.items() if k in ("interval_min", "max_entries", "enabled")}
    for k, v in fields.items():
        setattr(f, k, v)
    await ctx.audit("feed.update", target_type="feed", target_id=f.id, details=fields)
    await ctx.db.commit()
    return _out(f, await _assigns(ctx, f))


@router.delete("/{feed_id}", status_code=204, response_model=None)
async def delete_feed(feed_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    _can_edit(ctx, f)
    if f.builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vordefinierte Feeds können nur deaktiviert werden")
    if await _assigns(ctx, f):
        raise HTTPException(status.HTTP_409_CONFLICT, "Feed ist noch Geräten zugewiesen")
    obj = (await ctx.db.execute(select(FwObject).where(FwObject.kind == "feed", FwObject.slug == f.slug,
                                                       FwObject.tenant_id == f.tenant_id if f.tenant_id else FwObject.tenant_id.is_(None)))).scalar_one_or_none()
    if obj is not None:
        pols = [p for p in (await ctx.db.execute(select(FirewallPolicy).where(FirewallPolicy.mode == "simple"))).scalars()
                if str(obj.id) in referenced(p.spec or {})["objects"]]
        if pols:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Wird im Firewall-Editor verwendet: {', '.join(p.name for p in pols)}")
        await ctx.db.delete(obj)
    await ctx.audit("feed.delete", target_type="feed", target_id=f.id, details={"name": f.name})
    await ctx.db.delete(f)
    await ctx.db.commit()


@router.post("/{feed_id}/assign")
async def assign(feed_id: uuid.UUID, data: TargetsIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    devices = await resolve_targets(ctx.db, data.model_dump(), f.tenant_id)
    existing = {a.device_id for a, _d in await _assigns(ctx, f)}
    added = []
    for d in devices:
        if d.id not in existing:
            ctx.db.add(ThreatFeedAssignment(tenant_id=d.tenant_id, feed_id=f.id, device_id=d.id))
            added.append(str(d.id))
    await ctx.audit("feed.assign", target_type="feed", target_id=f.id, details={"devices": added})
    await ctx.db.commit()
    return {"assigned": added}


@router.delete("/{feed_id}/assign/{device_id}")
async def unassign(feed_id: uuid.UUID, device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    a = (await ctx.db.execute(select(ThreatFeedAssignment).where(ThreatFeedAssignment.feed_id == f.id,
                                                                 ThreatFeedAssignment.device_id == device_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Zuweisung nicht gefunden")
    dev = await ctx.db.get(Device, device_id)
    removed, err = False, None
    if dev is not None:
        try:
            await remove_from_device(dev, f.slug)
            removed = True
        except RouterOSError as exc:
            err = str(exc)
    tenant_id = a.tenant_id
    await ctx.db.delete(a)
    await ctx.audit("feed.unassign", tenant_id=tenant_id, target_type="feed", target_id=f.id,
                    details={"device_id": str(device_id), "removed_from_router": removed, "error": err})
    await ctx.db.commit()
    return {"removed_from_router": removed, "error": err}


@router.post("/{feed_id}/refresh")
async def refresh(feed_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Jetzt laden und an alle erreichbaren zugewiesenen Geräte verteilen."""
    f = await get_or_404(ctx.db, ThreatFeed, feed_id, "Feed")
    changed = await refresh_feed(f)
    for a, d in await _assigns(ctx, f):
        if d.status.value == "online":
            await sync_assignment(ctx.db, f, a, d, force=True)
    await ctx.audit("feed.refresh", target_type="feed", target_id=f.id, success=f.last_error is None,
                    details={"changed": changed, "count": len(f.entries or []), "error": f.last_error})
    await ctx.db.commit()
    return {**_out(f, await _assigns(ctx, f)), "changed": changed}
