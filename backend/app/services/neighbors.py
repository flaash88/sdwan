"""Nachbarn (Phase 25): ``/ip/neighbor`` alle 10 min je Gerät, Standort-Topologie.

ANNAHME (Labor): Felder ``interface, identity, platform, board, version, mac-address, address`` (RouterOS 7 liefert je
nach Discovery-Protokoll nur einen Teil; fehlende Felder bleiben leer). ``interface`` kann bei Bridges als
``ether2,bridge`` erscheinen – angezeigt wird der erste Teil.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import Device, DeviceNeighbor
from app.routeros import RouterOSError
from app.routeros.client import DeviceAPI

INTERVAL_S = 600
MAX_ROWS = 500
FIELDS = {"interface": "interface", "identity": "identity", "platform": "platform", "board": "board", "version": "version",
          "mac_address": "mac-address", "address": "address"}


def _clean(v: Any, n: int) -> str | None:
    s = str(v or "").strip()
    return s[:n] or None


def parse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows[:MAX_ROWS]:
        item = {k: _clean(r.get(src), 200) for k, src in FIELDS.items()}
        if item["interface"]:
            item["interface"] = item["interface"].split(",")[0]
        if item["address"]:
            item["address"] = item["address"].split(",")[0][:64]
        if item["mac_address"]:
            item["mac_address"] = item["mac_address"].upper()[:32]
        if item["identity"] or item["mac_address"]:
            out.append(item)
    return out


async def neighbor_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    if time.time() - float((device.facts or {}).get("_neighbors_at") or 0) < INTERVAL_S:
        return None
    try:
        rows = await api.call("/ip/neighbor/print")
    except RouterOSError:
        return None
    device._neighbors = parse(rows)  # type: ignore[attr-defined]  # vom Post-Poll-Hook gespeichert
    return {"_neighbors_at": time.time(), "neighbor_count": len(device._neighbors)}  # type: ignore[attr-defined]


async def store_neighbors(db: AsyncSession, devices: list[Device]) -> None:
    for d in devices:
        rows = getattr(d, "_neighbors", None)
        if rows is None:
            continue
        await db.execute(delete(DeviceNeighbor).where(DeviceNeighbor.device_id == d.id))
        now = utcnow()
        for r in rows:
            db.add(DeviceNeighbor(tenant_id=d.tenant_id, device_id=d.id, seen_at=now, **r))
        d._neighbors = None  # type: ignore[attr-defined]


def neighbor_out(n: DeviceNeighbor, match: Device | None = None) -> dict[str, Any]:
    return {"interface": n.interface, "identity": n.identity, "platform": n.platform, "board": n.board, "version": n.version,
            "mac_address": n.mac_address, "address": n.address, "seen_at": n.seen_at,
            "device": {"id": str(match.id), "name": match.name} if match else None}


def matcher(devices: list[Device]) -> Any:
    """Nachbar → Plattform-Gerät: gleiche Identity oder Nachbar-Adresse unter den Adressen eines Geräts."""
    by_identity = {d.identity.lower(): d for d in devices if d.identity}
    by_addr: dict[str, Device] = {}
    for d in devices:
        for a in (d.facts or {}).get("addresses") or []:
            ip = str(a.get("address") or "").split("/")[0]
            if ip:
                by_addr[ip] = d
        if d.tunnel_ip:
            by_addr[d.tunnel_ip] = d

    def find(n: DeviceNeighbor) -> Device | None:
        return (by_identity.get((n.identity or "").lower()) if n.identity else None) or (by_addr.get(n.address or "") if n.address else None)

    return find


async def device_neighbors(db: AsyncSession, device: Device) -> list[dict[str, Any]]:
    rows = (await db.execute(select(DeviceNeighbor).where(DeviceNeighbor.device_id == device.id)
                             .order_by(DeviceNeighbor.interface, DeviceNeighbor.identity))).scalars().all()
    find = matcher(list((await db.execute(select(Device))).scalars()))
    return [neighbor_out(n, m if (m := find(n)) and m.id != device.id else None) for n in rows]


async def site_topology(db: AsyncSession, site_id: Any) -> dict[str, Any]:
    """Router des Standorts mit ihren Nachbarn je Interface; Nachbarn, die Plattform-Geräte sind, werden verknüpft."""
    all_devs = list((await db.execute(select(Device))).scalars())
    find = matcher(all_devs)
    devs = [d for d in all_devs if d.site_id == site_id]
    nodes: list[dict[str, Any]] = []
    for d in sorted(devs, key=lambda x: x.name):
        rows = (await db.execute(select(DeviceNeighbor).where(DeviceNeighbor.device_id == d.id)
                                 .order_by(DeviceNeighbor.interface, DeviceNeighbor.identity))).scalars().all()
        nodes.append({"id": str(d.id), "name": d.name, "status": d.status.value, "model": d.model,
                      "neighbors": [neighbor_out(n, m if (m := find(n)) and m.id != d.id else None) for n in rows]})
    return {"devices": nodes}
