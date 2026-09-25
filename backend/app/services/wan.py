"""WAN-Failover & Load-Balancing (Phase 3).

Die Health-Checks laufen **auf dem Router** (``/tool netwatch``), damit das Umschalten auch
funktioniert, wenn die Cloud über den ausgefallenen WAN gerade nicht erreichbar ist. Die
Control-Plane erzeugt die Konfiguration, pusht sie idempotent und überwacht den Zustand.

Aufbau je WAN-Link ``<slot>`` (1..4):

* Check-Route ``<target>/32 via <gw>`` – zwingt die Health-Check-Pakete über genau diesen WAN.
* Netwatch (ICMP oder HTTP-GET) auf ``<target>``; down-/up-Script deaktivieren/aktivieren alle
  Default-Routen dieses WANs (Kommentar ``sdwan:wan:default:<slot>``). Das up-Script wartet die
  Recovery-Zeit ab und prüft erneut (Hysterese gegen Flapping).
* Default-Route im main-Table, ``distance`` = Priorität (Failover) bzw. gleich (ECMP).
* Load-Balancing PCC: Routing-Tabellen ``sdwan-wan<slot>`` mit eigener Default-Route und
  Fallback über die anderen WANs, Mangle-Regeln mit ``per-connection-classifier`` gewichtet
  nach ``weight``. Eingehende Verbindungen antworten über denselben WAN.
"""

from __future__ import annotations

import datetime as dt

import ipaddress
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.db import utcnow
from app.models import Device, WanLink
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.util import parse_ms

log = logging.getLogger(__name__)

MODES = ("failover", "loadbalance_pcc", "loadbalance_ecmp")
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
DEFAULT_OPTIONS = {"recovery_delay_s": 30, "flush_connections": True}


class WanError(Exception):
    pass


def _default_comment(slot: int) -> str:
    return f"sdwan:wan:default:{slot}"


def flush_snippet(link: WanLink, mode: str) -> str:
    """RouterOS-Script-Baustein: Verbindungen entfernen, die über diesen WAN laufen.

    * PCC: über die Connection-Mark ``sdwan-wan<slot>``.
    * Failover/ECMP: alle Verbindungen, die auf eine Adresse des WAN-Interfaces genattet wurden
      (``reply-dst-address`` = WAN-IP:Port bei Masquerade). Die IPs werden zur Laufzeit gelesen,
      damit das auch bei DHCP funktioniert. Das Interface kann dabei physisch up bleiben.
    * WireGuard-UDP von Management-Tunnel und Mesh wird ausgenommen (Tunnel nicht abreißen).
    """
    if mode == "loadbalance_pcc":
        return f'/ip firewall connection remove [find where connection-mark="sdwan-wan{link.slot}"]'
    s = get_settings()
    keep = " and ".join(f'!(protocol="udp" and dst-address~":{p}")' for p in (s.wg_hub_port, s.mesh_listen_port))
    return (
        f':foreach a in=[/ip address find where interface="{link.interface}"] do={{ '
        f':local ip [/ip address get $a address]; :set ip [:pick $ip 0 [:find $ip "/"]]; '
        f'/ip firewall connection remove [find where reply-dst-address~("^" . $ip . ":") and {keep}] }}'
    )


def _scripts(link: WanLink, mode: str, recovery_s: int, flush: bool) -> tuple[str, str]:
    find = f'[find where comment~"^{_default_comment(link.slot)}"]'
    down = f"/ip route disable {find}"
    if flush:
        down += "; " + flush_snippet(link, mode)
    down += f'; :log warning "SD-WAN: WAN{link.slot} ({link.name}) DOWN"'
    up = (
        f":delay {recovery_s}s; "
        f':if ([/tool netwatch get [find where comment="sdwan:wan:check:{link.slot}"] status] = "up") do={{ '
        f"/ip route enable {find}; "
        f':log info "SD-WAN: WAN{link.slot} ({link.name}) wieder UP" }}'
    )
    return down, up


def build_config(device: Device, links: list[WanLink]) -> dict[str, list[dict[str, Any]]]:
    """Soll-Zustand aller verwalteten RouterOS-Objekte für die WAN-Konfiguration."""
    mode = device.wan_mode
    opts = {**DEFAULT_OPTIONS, **(device.wan_options or {})}
    active = sorted([lk for lk in links if lk.enabled], key=lambda lk: (lk.priority, lk.slot))
    cfg: dict[str, list[dict[str, Any]]] = {
        "/interface/list": [],
        "/interface/list/member": [],
        "/routing/table": [],
        "/ip/route": [],
        "/tool/netwatch": [],
        "/ip/firewall/address-list": [],
        "/ip/firewall/mangle": [],
        "/ip/firewall/nat": [],
    }
    if not active:
        return cfg
    gw = {lk.slot: lk.resolved_gateway or lk.gateway for lk in active}

    cfg["/interface/list"].append({"name": "sdwan-wan", "comment": "sdwan:wan:list"})
    for lk in active:
        cfg["/interface/list/member"].append({"list": "sdwan-wan", "interface": lk.interface, "comment": f"sdwan:wan:member:{lk.slot}"})
        cfg["/ip/route"].append({"dst-address": f"{lk.check_target}/32", "gateway": gw[lk.slot], "scope": 10, "comment": f"sdwan:wan:check:{lk.slot}"})
        distance = 1 if mode == "loadbalance_ecmp" else lk.priority
        cfg["/ip/route"].append({"dst-address": "0.0.0.0/0", "gateway": gw[lk.slot], "distance": distance, "comment": _default_comment(lk.slot)})
        down, up = _scripts(lk, mode, int(opts["recovery_delay_s"]), bool(opts["flush_connections"]))
        nw: dict[str, Any] = {
            "host": lk.check_target,
            "type": "icmp" if lk.check_type == "ping" else "http-get",
            "interval": f"{lk.check_interval_s}s",
            "timeout": f"{lk.check_timeout_ms}ms",
            "down-script": down,
            "up-script": up,
            "comment": f"sdwan:wan:check:{lk.slot}",
        }
        if lk.check_type == "ping":
            nw["thr-loss-percent"] = f"{lk.loss_threshold_pct}%"
            nw["packet-count"] = 5
            if lk.latency_threshold_ms:
                nw["thr-avg"] = f"{lk.latency_threshold_ms}ms"
        cfg["/tool/netwatch"].append(nw)
    cfg["/ip/firewall/nat"].append({"chain": "srcnat", "out-interface-list": "sdwan-wan", "action": "masquerade", "comment": "sdwan:wan:nat"})

    if mode == "loadbalance_pcc" and len(active) > 1:
        for i, net in enumerate(PRIVATE_NETS):
            cfg["/ip/firewall/address-list"].append({"list": "sdwan-private", "address": net, "comment": f"sdwan:wan:private:{i}"})
        total = sum(max(lk.weight, 1) for lk in active)
        mangle: list[dict[str, Any]] = []
        for lk in active:
            mark = f"sdwan-wan{lk.slot}"
            cfg["/routing/table"].append({"name": mark, "fib": "yes", "comment": f"sdwan:wan:table:{lk.slot}"})
            # Tabelle <mark>: eigener WAN zuerst, danach die anderen als Fallback
            others = [o for o in active if o is not lk]
            for rank, o in enumerate([lk, *others]):
                cfg["/ip/route"].append({
                    "dst-address": "0.0.0.0/0", "gateway": gw[o.slot], "routing-table": mark, "distance": rank + 1,
                    "comment": f"{_default_comment(o.slot)}:t{lk.slot}",
                })
            mangle.append({"chain": "prerouting", "in-interface": lk.interface, "connection-state": "new", "connection-mark": "no-mark",
                           "action": "mark-connection", "new-connection-mark": mark, "passthrough": "yes", "comment": f"sdwan:wan:in:{lk.slot}"})
        slot_idx = 0
        for lk in active:
            mark = f"sdwan-wan{lk.slot}"
            for _ in range(max(lk.weight, 1)):
                mangle.append({
                    "chain": "prerouting", "in-interface-list": "!sdwan-wan", "connection-mark": "no-mark", "connection-state": "new",
                    "dst-address-type": "!local", "dst-address-list": "!sdwan-private",
                    "per-connection-classifier": f"both-addresses-and-ports:{total}/{slot_idx}",
                    "action": "mark-connection", "new-connection-mark": mark, "passthrough": "yes", "comment": f"sdwan:wan:pcc:{slot_idx}",
                })
                slot_idx += 1
        for lk in active:
            mark = f"sdwan-wan{lk.slot}"
            mangle.append({"chain": "prerouting", "in-interface-list": "!sdwan-wan", "connection-mark": mark, "dst-address-list": "!sdwan-private",
                           "action": "mark-routing", "new-routing-mark": mark, "passthrough": "no", "comment": f"sdwan:wan:route:{lk.slot}"})
            mangle.append({"chain": "output", "connection-mark": mark, "dst-address-list": "!sdwan-private",
                           "action": "mark-routing", "new-routing-mark": mark, "passthrough": "no", "comment": f"sdwan:wan:out:{lk.slot}"})
        cfg["/ip/firewall/mangle"] = mangle
    return cfg


async def resolve_gateways(api: DeviceAPI, links: list[WanLink]) -> None:
    """Für ``gateway=dhcp`` das aktuell vom DHCP-Client gelernte Gateway auslesen."""
    dhcp = [lk for lk in links if lk.gateway.lower() == "dhcp"]
    if not dhcp:
        for lk in links:
            lk.resolved_gateway = None
        return
    clients = await api.print("/ip/dhcp-client")
    for lk in dhcp:
        cl = next((c for c in clients if c.get("interface") == lk.interface), None)
        if not cl or not cl.get("gateway"):
            raise WanError(f"WAN {lk.name}: kein DHCP-Gateway auf {lk.interface} gefunden")
        lk.resolved_gateway = str(cl["gateway"])


_ORDERED = {"/ip/firewall/mangle", "/ip/firewall/nat"}
# Reihenfolge beim Anlegen (Abhängigkeiten zuerst) und beim Entfernen umgekehrt
_PUSH_ORDER = ["/interface/list", "/interface/list/member", "/routing/table", "/ip/firewall/address-list",
               "/ip/route", "/tool/netwatch", "/ip/firewall/mangle", "/ip/firewall/nat"]


async def push_config(api: DeviceAPI, cfg: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    empty = {p for p in _PUSH_ORDER if not cfg.get(p)}
    # Erst Nutzer entfernen, dann Abhängigkeiten (z. B. Mangle vor Routing-Tabelle)
    for path in reversed(_PUSH_ORDER):
        if path in empty:
            stats[path] = await api.sync_managed(path, "wan:", [], ordered=path in _ORDERED)
    for path in _PUSH_ORDER:
        if path not in empty:
            stats[path] = await api.sync_managed(path, "wan:", cfg[path], ordered=path in _ORDERED, place_first=path in _ORDERED)
    return stats


async def apply_wan(db: AsyncSession, device: Device) -> dict[str, Any]:
    links = list((await db.execute(select(WanLink).where(WanLink.device_id == device.id).order_by(WanLink.slot))).scalars())
    async with connect_device(device) as api:
        await resolve_gateways(api, links)
        cfg = build_config(device, links)
        stats = await push_config(api, cfg)
    device.wan_options = {**DEFAULT_OPTIONS, **(device.wan_options or {}), "last_apply": utcnow().isoformat()}
    await db.flush()
    return {"ok": True, "stats": stats}


async def test_link(device: Device, link: WanLink) -> dict[str, Any]:
    """Sofort-Test: Ping über die Check-Route dieses WANs."""
    async with connect_device(device) as api:
        rows = await api.call("/ping", address=link.check_target, count=5)
    summary = rows[-1] if rows else {}
    times = [parse_ms(r.get("time")) for r in rows if r.get("time")]
    times = [t for t in times if t is not None]
    return {
        "sent": summary.get("sent"),
        "received": summary.get("received"),
        "loss_pct": summary.get("packet-loss"),
        "avg_ms": round(sum(times) / len(times), 1) if times else None,
    }


# ----------------------------------------------------------------------------- Monitoring
async def wan_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    netwatch = [n for n in await api.print("/tool/netwatch") if str(n.get("comment", "")).startswith("sdwan:wan:check:")]
    if not netwatch:
        return None
    routes = [r for r in await api.print("/ip/route") if str(r.get("comment", "")).startswith("sdwan:wan:default:")]
    info: dict[str, Any] = {}
    for n in netwatch:
        slot = str(n["comment"]).rsplit(":", 1)[1]
        info[slot] = {
            "status": str(n.get("status", "unknown")),
            "rtt_ms": parse_ms(n.get("rtt-avg")),
            "loss_pct": float(str(n.get("loss-percent", "0")).rstrip("%") or 0),
        }
    for r in routes:
        parts = str(r["comment"]).split(":")
        if len(parts) != 4:  # nur main-Table-Routen
            continue
        slot = parts[3]
        if slot in info:
            info[slot]["active"] = str(r.get("active", "false")).lower() in ("true", "yes")
    extra: dict[str, Any] = {"wan": info}
    dhcp = await api.print("/ip/dhcp-client")
    if dhcp:
        extra["dhcp_gateways"] = {str(c.get("interface")): str(c.get("gateway", "")) for c in dhcp}
    return extra


def account_volume(lk: WanLink, iface: dict[str, Any] | None, now: dt.datetime) -> None:
    """Addiert den Traffic seit dem letzten Poll auf das Monatsvolumen des WAN-Links."""
    month = now.strftime("%Y-%m")
    if lk.vol_month != month:
        lk.vol_month, lk.vol_bytes = month, 0
    if not iface or iface.get("rx_bytes") is None or iface.get("tx_bytes") is None:
        return
    rx, tx = int(iface["rx_bytes"]), int(iface["tx_bytes"])
    if lk.vol_last_rx is not None and lk.vol_last_tx is not None:
        # Zähler kleiner als zuvor -> Reboot/Reset: der aktuelle Stand ist der Traffic seit dem Reset
        d_rx = rx - lk.vol_last_rx if rx >= lk.vol_last_rx else rx
        d_tx = tx - lk.vol_last_tx if tx >= lk.vol_last_tx else tx
        lk.vol_bytes = (lk.vol_bytes or 0) + d_rx + d_tx
    lk.vol_last_rx, lk.vol_last_tx = rx, tx


async def update_wan_status(db: AsyncSession, devices: list[Device]) -> None:
    by_id = {d.id: d for d in devices}
    links = (await db.execute(select(WanLink).where(WanLink.device_id.in_(list(by_id))))).scalars().all()
    now = utcnow()
    resync: set = set()
    for lk in links:
        dev = by_id[lk.device_id]
        facts = dev.facts or {}
        if lk.gateway.lower() == "dhcp":
            cur = (facts.get("dhcp_gateways") or {}).get(lk.interface)
            if cur and cur != lk.resolved_gateway:
                resync.add(dev.id)
        info = (facts.get("wan") or {}).get(str(lk.slot))
        if getattr(dev, "_poll_ok", False):  # nur frische Zählerstände zählen
            account_volume(lk, (facts.get("interfaces") or {}).get(lk.interface), now)
        old = lk.status
        if not lk.enabled:
            lk.status = "disabled"
        elif info is None:
            lk.status = "unknown"
        else:
            st = info.get("status")
            rtt = info.get("rtt_ms")
            if st == "down":
                lk.status = "down"
            elif st == "up" and lk.latency_threshold_ms and rtt and rtt > lk.latency_threshold_ms:
                lk.status = "degraded"
            elif st == "up":
                lk.status = "up"
            else:
                lk.status = "unknown"
            lk.last_latency_ms = rtt
            lk.last_loss_pct = info.get("loss_pct")
            was_active, lk.active = lk.active, bool(info.get("active"))
            if was_active != lk.active or lk.active_since is None:
                lk.active_since = now
                if was_active != lk.active:
                    from app.services.state_log import record_subject_change

                    # Basis für "Zeit auf Backup-WAN" (SLA) und den Alarm wan_backup_active
                    await record_subject_change(db, lk.tenant_id, dev.id, f"wanactive:{lk.id}", "active" if lk.active else "inactive")
            lk.last_check_at = now
        if old != lk.status:
            lk.last_change_at = now
            if lk.status in ("up", "down", "degraded"):
                from app.services.state_log import record_subject_change

                await record_subject_change(db, lk.tenant_id, dev.id, f"wan:{lk.id}", lk.status)
            await events.publish(lk.tenant_id, "wan.link", {
                "id": str(lk.id), "device_id": str(dev.id), "device": dev.name, "name": lk.name,
                "status": lk.status, "previous": old,
            })
    for dev_id in resync:
        dev = by_id[dev_id]
        try:
            await apply_wan(db, dev)
            log.info("WAN von %s nach DHCP-Gateway-Wechsel neu angewendet", dev.name)
        except (RouterOSError, WanError) as exc:
            log.warning("WAN-Resync %s fehlgeschlagen: %s", dev.name, exc)


def validate_links(links: list[dict[str, Any]]) -> None:
    if len(links) > 4:
        raise WanError("Maximal 4 WAN-Links pro Gerät")
    targets = [lk["check_target"] for lk in links]
    if len(set(targets)) != len(targets):
        raise WanError("Jeder WAN-Link braucht ein eigenes Check-Ziel (Check-Routen pinnen das Ziel an den WAN)")
    ifaces = [lk["interface"] for lk in links]
    if len(set(ifaces)) != len(ifaces):
        raise WanError("Interface mehrfach verwendet")
    for lk in links:
        ipaddress.ip_address(lk["check_target"])
