"""Seed-Daten: vordefinierte, globale Einträge (Dienste, Zonen, Bausteine, …) aus JSON-Dateien.

Idempotent: Einträge werden über ``seed_key`` erkannt; neue werden angelegt, bestehende Builtins aktualisiert.
Einträge ohne ``builtin`` (eigene oder kopierte) werden nie verändert. Keine Kundendaten, keine festen Netze.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import system_session

log = logging.getLogger(__name__)
SEED_DIR = Path(__file__).parent


def load(name: str) -> dict[str, Any]:
    return json.loads((SEED_DIR / f"{name}.json").read_text(encoding="utf-8"))


async def _upsert(db: AsyncSession, model: Any, item: dict[str, Any]) -> Any:
    row = (await db.execute(select(model).where(model.seed_key == item["seed_key"], model.tenant_id.is_(None)))).scalar_one_or_none()
    fields = {k: v for k, v in item.items() if not k.endswith("_seed")}
    if row is None:
        row = model(tenant_id=None, builtin=True, **fields)
        db.add(row)
    elif row.builtin:
        for k, v in fields.items():
            setattr(row, k, v)
    return row


async def apply_firewall(db: AsyncSession) -> None:
    from app.models import FwBlock, FwService, FwZone

    data = load("firewall")
    by_key: dict[str, Any] = {}
    for s in data["services"]:
        by_key[s["seed_key"]] = await _upsert(db, FwService, {"entries": [], "members": [], **s})
    await db.flush()
    for s in data["services"]:
        if s.get("members_seed"):
            by_key[s["seed_key"]].members = [str(by_key[k].id) for k in s["members_seed"]]
    for z in data["zones"]:
        await _upsert(db, FwZone, {"source": "manual", "management": False, **z})
    for b in data["blocks"]:
        await _upsert(db, FwBlock, {"nat": [], **b})


async def apply_feeds(db: AsyncSession) -> None:
    from app.models import FwBlock, FwObject, ThreatFeed

    data = load("feeds")
    for f in data["feeds"]:
        feed = await _upsert(db, ThreatFeed, f)
        # passendes Firewall-Objekt (Typ feed) für den Editor
        await _upsert(db, FwObject, {"seed_key": f"obj-{f['seed_key']}", "name": f"Threat-Feed: {f['name']}", "slug": f["slug"],
                                     "kind": "feed", "values": [], "members": [], "description": f.get("description")})
        del feed
    for b in data["blocks"]:
        await _upsert(db, FwBlock, {"nat": [], **b})


async def apply_compliance(db: AsyncSession) -> None:
    from app.models import ComplianceRuleSet

    for rs in load("compliance")["rule_sets"]:
        await _upsert(db, ComplianceRuleSet, rs)


async def apply_scripts(db: AsyncSession) -> None:
    from app.models import Script

    for sc in load("scripts")["scripts"]:
        await _upsert(db, Script, {"version": 1, **sc})


APPLIERS = [apply_firewall, apply_feeds, apply_compliance, apply_scripts]


async def apply_all() -> None:
    async with system_session() as db:
        for fn in APPLIERS:
            await fn(db)
        await db.commit()
    log.info("Seed-Daten angewendet")
