"""Trefferzähler verwalteter Firewall-Regeln (Phase 14).

Der Poll-Hook liest alle 5 Minuten ``packets``/``bytes`` der ``sdwan:fw:``-Regeln (Filter und NAT) und ordnet
sie über den Kommentar (``r:<id>`` bzw. ``base:<name>``) der Editor-Regel zu. Regeln, die je Protokoll aufgeteilt
wurden, werden summiert. Sinkt ein Zähler, wurde er zurückgesetzt (Reset oder Neustart).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import Device, FwRuleHit
from app.routeros.client import DeviceAPI
from app.services.fw_compile import rule_key_from_comment

HIT_INTERVAL_S = 300
PATHS = ("/ip/firewall/filter", "/ip/firewall/nat")


def _int(v: Any) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


async def read_hits(api: DeviceAPI) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for path in PATHS:
        for r in await api.print(path):
            key = rule_key_from_comment(str(r.get("comment", "")))
            if key is None:
                continue
            h = hits.setdefault(key, [0, 0])
            h[0] += _int(r.get("packets"))
            h[1] += _int(r.get("bytes"))
    return hits


async def fw_hits_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    last = float((device.facts or {}).get("_fw_hits_at") or 0)
    if time.time() - last < HIT_INTERVAL_S:
        return None
    hits = await read_hits(api)
    device._fw_hits = hits  # type: ignore[attr-defined]  # für den Post-Poll-Hook (nur dieser Durchlauf)
    return {"_fw_hits_at": time.time()}


async def store_hits(db: AsyncSession, device: Device, hits: dict[str, list[int]]) -> None:
    now = utcnow()
    rows = {h.key: h for h in (await db.execute(select(FwRuleHit).where(FwRuleHit.device_id == device.id))).scalars()}
    for key, (packets, byts) in hits.items():
        row = rows.get(key)
        if row is None:
            row = FwRuleHit(tenant_id=device.tenant_id, device_id=device.id, key=key, packets=0, bytes=0)
            db.add(row)
        grew = packets > (row.packets or 0) or (packets < (row.packets or 0) and packets > 0)
        if grew:
            row.last_hit_at = now
        row.packets, row.bytes, row.updated_at = packets, byts, now


async def update_hits(db: AsyncSession, devices: list[Device]) -> None:
    for d in devices:
        hits = getattr(d, "_fw_hits", None)
        if hits is not None:
            await store_hits(db, d, hits)
