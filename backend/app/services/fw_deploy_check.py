"""Vorprüfung vor dem Deploy einfacher Policies (Phase 14, Entscheidungen 17/18).

Verwaltete Regeln werden per ``place_first`` VOR der ersten nicht verwalteten Regel eingefügt, stehen also
oben im Regelwerk. Enthält eine einfache Policy den Default-Drop, würden alle nicht verwalteten Regeln der
Chains input/forward dahinter liegen und nie mehr greifen. Solche Geräte werden standardmäßig übersprungen;
nur mit ausdrücklicher Bestätigung je Gerät wird ausgerollt.

Werks-Firewallregeln (Kommentar ``defconf…``) werden gesondert gemeldet (``defconf``): Sie sind durch die Grundregeln
abgedeckt und können beim Deploy deaktiviert werden (siehe ``services/fw_defconf.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceZoneMember, FirewallPolicy, PolicyAssignment, WanLink
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.services.fw_lint import DeviceCtx

DROP_CHAINS = ("input", "forward")


async def unmanaged_rules(api: DeviceAPI) -> list[dict[str, Any]]:
    """Nicht verwaltete, aktive, statische Filterregeln der Chains input/forward – ohne defconf-Regeln."""
    from app.services.fw_defconf import is_defconf

    out = []
    for r in await api.print("/ip/firewall/filter"):
        if str(r.get("comment", "")).startswith("sdwan:") or is_defconf(r):
            continue
        if str(r.get("dynamic", "false")).lower() in ("true", "yes") or str(r.get("disabled", "false")).lower() in ("true", "yes"):
            continue
        if r.get("chain") in DROP_CHAINS:
            out.append({"chain": r.get("chain"), "action": r.get("action"), "comment": r.get("comment") or "",
                        "summary": " ".join(f"{k}={v}" for k, v in r.items() if k in ("protocol", "src-address", "dst-address", "dst-port", "in-interface", "in-interface-list"))})
    return out


def has_default_drop(policy: FirewallPolicy) -> bool:
    return policy.mode == "simple" and bool(((policy.spec or {}).get("options") or {}).get("default_drop", True))


async def device_contexts(db: AsyncSession, policy: FirewallPolicy, devices: list[Device]) -> list[DeviceCtx]:
    out = []
    for d in devices:
        zones = {str(m.zone_id) for m in (await db.execute(select(DeviceZoneMember).where(DeviceZoneMember.device_id == d.id))).scalars()}
        has_wan = (await db.execute(select(WanLink.id).where(WanLink.device_id == d.id).limit(1))).first() is not None
        assigns = (await db.execute(select(PolicyAssignment).where(PolicyAssignment.device_id == d.id)
                                    .order_by(PolicyAssignment.position))).scalars().all()
        own = next((a for a in assigns if a.policy_id == policy.id), None)
        later = sum(1 for a in assigns if own is not None and a.policy_id != policy.id and a.position > own.position)
        from app.models import HotspotInstance

        has_hs = (await db.execute(select(HotspotInstance.id).where(HotspotInstance.device_id == d.id).limit(1))).first() is not None
        out.append(DeviceCtx(name=d.name, zone_ids=zones, has_wan=has_wan, later_policies=later, has_hotspot=has_hs))
    return out


async def check_devices(policy: FirewallPolicy, devices: list[Device]) -> dict[str, dict[str, Any]]:
    """Je Gerät: erreichbar?, nicht verwaltete Regeln und defconf-Regeln hinter dem Default-Drop."""
    from app.services.fw_defconf import active_defconf

    result: dict[str, dict[str, Any]] = {}
    if not has_default_drop(policy):
        return {str(d.id): {"name": d.name, "reachable": None, "unmanaged": [], "defconf": []} for d in devices}

    async def one(d: Device) -> None:
        try:
            async with connect_device(d) as api:
                result[str(d.id)] = {"name": d.name, "reachable": True, "unmanaged": await unmanaged_rules(api),
                                     "defconf": await active_defconf(api)}
        except RouterOSError as exc:
            result[str(d.id)] = {"name": d.name, "reachable": False, "error": str(exc), "unmanaged": [], "defconf": []}

    await asyncio.gather(*(one(d) for d in devices))
    return result
