"""Ziele „Geräte / Standorte / Tags“ → Geräteliste (gemeinsam für Feeds, Compliance, Scripts, WLAN …)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device


async def resolve_targets(db: AsyncSession, targets: dict[str, Any], tenant_id: uuid.UUID | None = None) -> list[Device]:
    """``{device_ids, site_ids, tags}`` → Geräte (vereinigt). ``tenant_id`` begrenzt zusätzlich auf einen Mandanten."""
    dev_ids = [uuid.UUID(str(x)) for x in targets.get("device_ids") or []]
    site_ids = [uuid.UUID(str(x)) for x in targets.get("site_ids") or []]
    tags = {str(t) for t in targets.get("tags") or [] if str(t).strip()}
    conds = []
    if dev_ids:
        conds.append(Device.id.in_(dev_ids))
    if site_ids:
        conds.append(Device.site_id.in_(site_ids))
    found: dict[uuid.UUID, Device] = {}
    if conds:
        for d in (await db.execute(select(Device).where(or_(*conds)))).scalars():
            found[d.id] = d
    if tags:
        for d in (await db.execute(select(Device))).scalars():
            if set(d.tags or []) & tags:
                found[d.id] = d
    out = [d for d in found.values() if tenant_id is None or d.tenant_id == tenant_id]
    return sorted(out, key=lambda d: d.name)
