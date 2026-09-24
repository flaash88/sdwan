"""Site-to-Site-VPN-Mesh (Phase 2).

Pro Tenant wird ein Transfernetz (/24 aus ``MESH_NETWORK``) vergeben. Jedes teilnehmende
Gerät (ein gepairtes Gerät pro Standort) bekommt ein zweites WireGuard-Interface
``sdwan-mesh`` mit eigener IP. Die Control-Plane berechnet die Peers je nach Topologie,
erzeugt pro Verbindung einen Preshared-Key und pusht Interface, Adresse, Peers, Routen
und Firewall-Freigaben per RouterOS-API (über den Management-Tunnel).

* hub_spoke: alle Standorte verbinden sich mit dem Hub-Standort (``Site.is_mesh_hub``);
  Spoke-zu-Spoke-Verkehr läuft über den Hub.
* full_mesh: jeder Standort mit jedem. Eine Verbindung braucht mindestens eine Seite mit
  erreichbarem Endpoint, sonst Status ``no_endpoint``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.db import utcnow
from app.models import Device, HubPeerStat, PairingStatus, Site, Tenant, VpnPeer
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.util import parse_duration
from app.security import decrypt_secret, encrypt_secret
from app.services.wireguard import generate_psk, mesh_subnet_for_index

log = logging.getLogger(__name__)
MESH_UP_SECONDS = 180


class MeshError(Exception):
    pass


@dataclass
class Node:
    device: Device
    site: Site

    @property
    def id(self) -> uuid.UUID:
        return self.device.id


@dataclass
class Link:
    a: Node
    b: Node
    kind: str
    # Welche Seite initiiert die Verbindung (setzt endpoint + keepalive)?
    a_initiates: bool = True
    b_initiates: bool = False
    peer: VpnPeer | None = None


@dataclass
class MeshPlan:
    tenant: Tenant
    nodes: list[Node]
    links: list[Link]
    hub: Node | None = None
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- Planung
async def _endpoint_of(db: AsyncSession, dev: Device) -> str | None:
    if dev.mesh_endpoint:
        return dev.mesh_endpoint
    if dev.wg_public_key:
        st = await db.get(HubPeerStat, dev.wg_public_key)
        if st and st.endpoint:
            host = st.endpoint.rsplit(":", 1)[0].strip("[]")
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_global:
                    return host
            except ValueError:
                return host
    return None


async def ensure_tenant_subnet(db: AsyncSession, tenant: Tenant) -> ipaddress.IPv4Network:
    if not tenant.mesh_subnet:
        used = {
            r[0] for r in await db.execute(select(Tenant.mesh_subnet).where(Tenant.mesh_subnet.is_not(None)))
        }
        idx = 0
        while mesh_subnet_for_index(idx) in used:
            idx += 1
        tenant.mesh_subnet = mesh_subnet_for_index(idx)
        await db.flush()
    return ipaddress.ip_network(tenant.mesh_subnet)


async def build_plan(db: AsyncSession, tenant: Tenant) -> MeshPlan:
    rows = await db.execute(
        select(Device, Site)
        .join(Site, Device.site_id == Site.id)
        .where(Device.tenant_id == tenant.id, Device.pairing_status == PairingStatus.paired)
        .order_by(Site.name, Device.name)
    )
    nodes: list[Node] = []
    seen_sites: set[uuid.UUID] = set()
    warnings: list[str] = []
    for dev, site in rows.all():
        if site.id in seen_sites:
            warnings.append(f"Standort {site.name}: mehrere Geräte – nur das erste nimmt am Mesh teil ({dev.name} übersprungen)")
            continue
        seen_sites.add(site.id)
        nodes.append(Node(dev, site))

    plan = MeshPlan(tenant=tenant, nodes=nodes, links=[], warnings=warnings)
    if tenant.mesh_topology == "none" or len(nodes) < 2:
        return plan
    if tenant.mesh_topology == "hub_spoke":
        hubs = [n for n in nodes if n.site.is_mesh_hub]
        if not hubs:
            raise MeshError("Hub-and-Spoke: kein Standort als Hub markiert")
        if len(hubs) > 1:
            warnings.append(f"Mehrere Hub-Standorte – verwende {hubs[0].site.name}")
        plan.hub = hubs[0]
        for n in nodes:
            if n is not plan.hub:
                plan.links.append(Link(plan.hub, n, "hub_spoke", a_initiates=False, b_initiates=True))
    else:
        for a, b in itertools.combinations(nodes, 2):
            plan.links.append(Link(a, b, "full_mesh", a_initiates=True, b_initiates=True))
    return plan


def _ordered(a: Node, b: Node) -> tuple[Node, Node, bool]:
    """VpnPeer speichert Paare sortiert nach Device-ID."""
    return (a, b, False) if str(a.id) < str(b.id) else (b, a, True)


async def sync_peer_rows(db: AsyncSession, plan: MeshPlan) -> None:
    existing = {
        (p.device_a_id, p.device_b_id): p
        for p in (await db.execute(select(VpnPeer).where(VpnPeer.tenant_id == plan.tenant.id))).scalars()
    }
    wanted: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for link in plan.links:
        x, y, _ = _ordered(link.a, link.b)
        key = (x.id, y.id)
        wanted.add(key)
        peer = existing.get(key)
        if peer is None:
            peer = VpnPeer(tenant_id=plan.tenant.id, device_a_id=x.id, device_b_id=y.id, kind=link.kind, psk_enc=encrypt_secret(generate_psk()))
            db.add(peer)
        peer.kind = link.kind
        link.peer = peer
    for key, peer in existing.items():
        if key not in wanted:
            await db.delete(peer)
    await db.flush()


def _allowed_for(plan: MeshPlan, local: Node, remote: Node) -> list[str]:
    """Allowed-Addresses für den Peer ``remote`` auf Gerät ``local``."""
    if plan.tenant.mesh_topology == "hub_spoke" and plan.hub is not None and remote is plan.hub:
        # Spoke -> Hub: gesamtes Transfernetz + alle LANs aller anderen Standorte
        nets = [plan.tenant.mesh_subnet]
        for n in plan.nodes:
            if n is not local:
                nets += n.site.lan_subnets or []
        return _dedupe(nets)
    return _dedupe([f"{remote.device.mesh_ip}/32", *(remote.site.lan_subnets or [])])


def _dedupe(nets: list[str | None]) -> list[str]:
    out: list[str] = []
    for n in nets:
        if n and n not in out:
            out.append(n)
    return out


def device_config(plan: MeshPlan, node: Node, endpoints: dict[uuid.UUID, str | None]) -> dict[str, Any]:
    """Soll-Konfiguration (Peers, Routen, Firewall) für ein Gerät."""
    s = get_settings()
    iface = s.mesh_interface
    peers: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    local_lans = set(node.site.lan_subnets or [])
    for link in plan.links:
        if node not in (link.a, link.b):
            continue
        remote = link.b if node is link.a else link.a
        initiates = link.a_initiates if node is link.a else link.b_initiates
        allowed = _allowed_for(plan, node, remote)
        entry: dict[str, Any] = {
            "interface": iface,
            "public-key": remote.device.mesh_public_key,
            "preshared-key": decrypt_secret(link.peer.psk_enc) if link.peer else None,
            "allowed-address": ",".join(allowed),
            "comment": f"sdwan:mesh:{remote.id}",
        }
        ep = endpoints.get(remote.id)
        if initiates and ep:
            entry.update({"endpoint-address": ep, "endpoint-port": s.mesh_listen_port, "persistent-keepalive": "25s"})
        peers.append({k: v for k, v in entry.items() if v is not None})
        for net in allowed:
            if net == plan.tenant.mesh_subnet or net.endswith("/32") or net in local_lans:
                continue
            routes.append({"dst-address": net, "gateway": iface, "comment": f"sdwan:mesh:route:{net}"})
    firewall = [
        {"chain": "input", "protocol": "udp", "dst-port": str(s.mesh_listen_port), "action": "accept", "comment": "sdwan:mesh:wg"},
        {"chain": "forward", "in-interface": iface, "action": "accept", "comment": "sdwan:mesh:fwd-in"},
        {"chain": "forward", "out-interface": iface, "action": "accept", "comment": "sdwan:mesh:fwd-out"},
    ] if peers else []
    return {"peers": peers, "routes": _uniq_routes(routes), "firewall": firewall}


def _uniq_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in routes:
        seen.setdefault(r["comment"], r)
    return list(seen.values())


# ----------------------------------------------------------------------------- Push
async def ensure_mesh_interface(api: DeviceAPI, device: Device, mesh_ip: str, prefix: int) -> str:
    s = get_settings()
    iface = s.mesh_interface
    wg = await api.print("/interface/wireguard", name=iface)
    if not wg:
        await api.add("/interface/wireguard", name=iface, **{"listen-port": s.mesh_listen_port, "mtu": 1420, "comment": "sdwan:mesh"})
        wg = await api.print("/interface/wireguard", name=iface)
    await api.sync_managed("/ip/address", "mesh:addr", [
        {"address": f"{mesh_ip}/{prefix}", "interface": iface, "comment": "sdwan:mesh:addr"},
    ])
    return str(wg[0]["public-key"])


async def remove_mesh(api: DeviceAPI) -> None:
    iface = get_settings().mesh_interface
    for path in ("/interface/wireguard/peers", "/ip/route", "/ip/firewall/filter", "/ip/address"):
        await api.sync_managed(path, "mesh", [])
    for wg in await api.print("/interface/wireguard", name=iface):
        await api.remove("/interface/wireguard", wg[".id"])


async def _allocate_mesh_ips(db: AsyncSession, plan: MeshPlan, subnet: ipaddress.IPv4Network) -> None:
    all_devs = (await db.execute(select(Device).where(Device.tenant_id == plan.tenant.id))).scalars().all()
    used = {d.mesh_ip for d in all_devs if d.mesh_ip}
    hosts = (str(h) for h in subnet.hosts())
    ordered = sorted(plan.nodes, key=lambda n: (n is not plan.hub, n.site.name))
    for n in ordered:
        if n.device.mesh_ip and ipaddress.ip_address(n.device.mesh_ip) in subnet:
            continue
        for ip in hosts:
            if ip not in used:
                n.device.mesh_ip = ip
                used.add(ip)
                break


async def apply_mesh(db: AsyncSession, tenant: Tenant) -> dict[str, Any]:
    """Berechnet und pusht das Mesh eines Tenants. Liefert einen Report pro Gerät."""
    subnet = await ensure_tenant_subnet(db, tenant)
    plan = await build_plan(db, tenant)
    report: dict[str, Any] = {"topology": tenant.mesh_topology, "subnet": str(subnet), "devices": {}, "warnings": plan.warnings, "links": 0}
    await _allocate_mesh_ips(db, plan, subnet)

    participating = {n.id for n in plan.nodes} if plan.links else set()
    # Geräte, die nicht (mehr) teilnehmen: Mesh-Konfiguration entfernen
    leavers = (
        await db.execute(select(Device).where(Device.tenant_id == tenant.id, Device.mesh_public_key.is_not(None)))
    ).scalars().all()
    for dev in leavers:
        if dev.id not in participating:
            try:
                async with connect_device(dev) as api:
                    await remove_mesh(api)
                dev.mesh_public_key = None
                report["devices"][str(dev.id)] = {"name": dev.name, "ok": True, "action": "removed"}
            except RouterOSError as exc:
                report["devices"][str(dev.id)] = {"name": dev.name, "ok": False, "error": str(exc)}

    if not plan.links:
        await sync_peer_rows(db, plan)
        tenant.settings = {**(tenant.settings or {}), "mesh_last_apply": {"at": utcnow().isoformat(), **report}}
        await db.commit()
        return report

    # Schritt 1: Interfaces sicherstellen und Mesh-Public-Keys einsammeln
    reachable: list[Node] = []

    async def step1(n: Node) -> None:
        try:
            async with connect_device(n.device) as api:
                n.device.mesh_public_key = await ensure_mesh_interface(api, n.device, n.device.mesh_ip, subnet.prefixlen)
            reachable.append(n)
        except RouterOSError as exc:
            report["devices"][str(n.id)] = {"name": n.device.name, "ok": False, "error": str(exc)}

    await asyncio.gather(*(step1(n) for n in plan.nodes))
    for n in plan.nodes:
        if n.device.mesh_public_key is None and n not in reachable:
            plan.warnings.append(f"{n.device.name}: nicht erreichbar, Mesh-Key unbekannt")
    plan.links = [lk for lk in plan.links if lk.a.device.mesh_public_key and lk.b.device.mesh_public_key]
    await sync_peer_rows(db, plan)
    report["links"] = len(plan.links)

    endpoints = {n.id: await _endpoint_of(db, n.device) for n in plan.nodes}
    for link in plan.links:
        a_ok = link.a_initiates and endpoints.get(link.b.id)
        b_ok = link.b_initiates and endpoints.get(link.a.id)
        if not (a_ok or b_ok) and link.peer is not None:
            link.peer.status = "no_endpoint"
            link.peer.last_error = "Keine Seite hat einen erreichbaren Endpoint (beide hinter NAT?)"
            plan.warnings.append(f"{link.a.device.name} ↔ {link.b.device.name}: kein erreichbarer Endpoint")

    # Schritt 2: Peers, Routen, Firewall pushen
    async def step2(n: Node) -> None:
        cfg = device_config(plan, n, endpoints)
        try:
            async with connect_device(n.device) as api:
                stats = {
                    "peers": await api.sync_managed("/interface/wireguard/peers", "mesh:", cfg["peers"]),
                    "routes": await api.sync_managed("/ip/route", "mesh:route:", cfg["routes"]),
                    "firewall": await api.sync_managed("/ip/firewall/filter", "mesh:", cfg["firewall"], ordered=True, place_first=True),
                }
            report["devices"][str(n.id)] = {"name": n.device.name, "ok": True, "mesh_ip": n.device.mesh_ip, "peers": len(cfg["peers"]), "stats": stats}
        except RouterOSError as exc:
            report["devices"][str(n.id)] = {"name": n.device.name, "ok": False, "error": str(exc)}

    await asyncio.gather(*(step2(n) for n in reachable))
    tenant.settings = {**(tenant.settings or {}), "mesh_last_apply": {"at": utcnow().isoformat(), **report}}
    await db.commit()
    await events.publish(tenant.id, "mesh.applied", {"links": report["links"], "errors": sum(1 for d in report["devices"].values() if not d["ok"])})
    return report


# ----------------------------------------------------------------------------- Status
async def mesh_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    """Poll-Hook: liest Handshake/Traffic der Mesh-Peers des Geräts."""
    if not device.mesh_public_key:
        return None
    iface = get_settings().mesh_interface
    out: dict[str, Any] = {}
    for p in await api.print("/interface/wireguard/peers"):
        comment = str(p.get("comment", ""))
        if p.get("interface") != iface or not comment.startswith("sdwan:mesh:"):
            continue
        out[comment.removeprefix("sdwan:mesh:")] = {
            "handshake_s": parse_duration(p.get("last-handshake")),
            "rx": int(p.get("rx", 0) or 0),
            "tx": int(p.get("tx", 0) or 0),
        }
    return {"mesh_peers": out}


async def update_peer_status(db: AsyncSession, devices: list[Device]) -> None:
    """Post-Poll: VpnPeer-Status aus den Fakten beider Enden ableiten."""
    by_id = {d.id: d for d in devices}
    peers = (await db.execute(select(VpnPeer))).scalars().all()
    now = utcnow()
    for p in peers:
        a, b = by_id.get(p.device_a_id), by_id.get(p.device_b_id)
        best: float | None = None
        rx = tx = 0
        for local, remote in ((a, p.device_b_id), (b, p.device_a_id)):
            if local is None:
                continue
            info = ((local.facts or {}).get("mesh_peers") or {}).get(str(remote))
            if not info:
                continue
            hs = info.get("handshake_s")
            if hs is not None and (best is None or hs < best):
                best = hs
            rx, tx = max(rx, info.get("rx", 0)), max(tx, info.get("tx", 0))
        old = p.status
        if best is not None and best <= MESH_UP_SECONDS:
            p.status = "up"
            p.last_error = None
        elif p.status != "no_endpoint":
            p.status = "down"
        if best is not None:
            import datetime as dt

            p.last_handshake_at = now - dt.timedelta(seconds=best)
        p.rx_bytes, p.tx_bytes, p.updated_at = rx, tx, now
        if old != p.status:
            await events.publish(p.tenant_id, "mesh.link", {"id": str(p.id), "status": p.status, "previous": old})


# ----------------------------------------------------------------------------- Automatik
def fingerprint(plan: MeshPlan) -> str:
    import hashlib
    import json

    data = {
        "topo": plan.tenant.mesh_topology,
        "hub": str(plan.hub.id) if plan.hub else None,
        "nodes": sorted(
            [str(n.id), sorted(n.site.lan_subnets or []), n.device.mesh_endpoint or "", n.device.wg_public_key or ""]
            for n in plan.nodes
        ),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def auto_apply_all() -> None:
    """Worker-Job: wendet das Mesh neu an, sobald sich Topologie/Standorte/Geräte ändern."""
    from app.db import system_session

    async with system_session() as db:
        tenants = (await db.execute(select(Tenant).where(Tenant.is_active.is_(True)))).scalars().all()
        for t in tenants:
            if not (t.settings or {}).get("mesh_auto", True):
                continue
            try:
                plan = await build_plan(db, t)
            except MeshError as exc:
                log.info("Mesh %s: %s", t.slug, exc)
                continue
            fp = fingerprint(plan)
            if (t.settings or {}).get("mesh_fingerprint") == fp:
                continue
            log.info("Mesh für %s geändert – wende an", t.slug)
            report = await apply_mesh(db, t)
            if all(d.get("ok") for d in report["devices"].values()):
                t.settings = {**(t.settings or {}), "mesh_fingerprint": fp}
                await db.commit()
