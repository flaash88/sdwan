"""Firewall-Zonen auf Geräten (Phase 14): Interface → Zone, auf dem Router als Interface-List.

Verwaltet werden ``/interface/list`` (``sdwan-zone-<slug>``, Kommentar ``sdwan:zone:<slug>``) und
``/interface/list/member`` (Kommentar ``sdwan:zone:<slug>:<interface>``). Die Zone mit ``source=wan`` nutzt die
bestehende Liste ``sdwan-wan`` der WAN-Konfiguration und wird hier nicht angelegt. Nicht verwaltete Listen
(z. B. defconf ``LAN``/``WAN``) bleiben unberührt.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceZoneMember, FirewallPolicy, FwZone, PolicyAssignment
from app.routeros.client import DeviceAPI
from app.services.fw_compile import referenced, zone_list


async def device_zone_config(db: AsyncSession, device: Device) -> dict[str, Any]:
    """Sollzustand: alle Zonen mit Interfaces auf dem Gerät und alle Zonen, die zugewiesene einfache Policies
    referenzieren (Listen müssen existieren, sonst lehnt RouterOS die Regel ab)."""
    members = (await db.execute(select(DeviceZoneMember).where(DeviceZoneMember.device_id == device.id))).scalars().all()
    zone_ids = {m.zone_id for m in members}
    pols = (await db.execute(select(FirewallPolicy).join(PolicyAssignment, PolicyAssignment.policy_id == FirewallPolicy.id)
                             .where(PolicyAssignment.device_id == device.id, FirewallPolicy.mode == "simple"))).scalars().all()
    for p in pols:
        zone_ids |= {uuid.UUID(z) for z in referenced(p.spec or {})["zones"]}
    zones = {z.id: z for z in (await db.execute(select(FwZone).where(FwZone.id.in_(zone_ids)).execution_options(skip_tenant_filter=True))).scalars()} if zone_ids else {}
    lists, mems = [], []
    for z in sorted(zones.values(), key=lambda z: z.slug):
        if z.source == "wan":
            continue
        name = zone_list({"slug": z.slug, "source": z.source})
        lists.append({"name": name, "comment": f"sdwan:zone:{z.slug}"})
        for m in sorted((m for m in members if m.zone_id == z.id), key=lambda m: m.interface):
            mems.append({"list": name, "interface": m.interface, "comment": f"sdwan:zone:{z.slug}:{m.interface}"})
    return {"lists": lists, "members": mems}


async def push_zones(api: DeviceAPI, cfg: dict[str, Any]) -> dict[str, Any]:
    # Reihenfolge: Listen anlegen, dann Mitglieder abgleichen; nicht mehr benötigte Listen erst danach entfernen
    existing = {str(r.get("name")) for r in await api.print("/interface/list")}
    early = [x for x in cfg["lists"] if x["name"] not in existing]
    for x in early:
        await api.add("/interface/list", **x)
    stats = {"members": await api.sync_managed("/interface/list/member", "zone:", cfg["members"])}
    stats["lists"] = await api.sync_managed("/interface/list", "zone:", cfg["lists"])
    return stats


async def read_interface_lists(api: DeviceAPI) -> set[str]:
    return {str(r.get("name")) for r in await api.print("/interface/list")}
