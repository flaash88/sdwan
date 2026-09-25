"""VRRP (Phase 11): Router als Backup-Gateway neben einem zentralen Master (z. B. FortiGate).

Pro Instanz werden verwaltet (Kommentar ``sdwan:vrrp:<kurz-id>…``):

* optionale lokale Adresse auf dem Parent-Interface (``/ip address``),
* das VRRP-Interface (``/interface vrrp``: interface, vrid, priority, interval, preemption-mode,
  version, on-master, on-backup),
* die virtuelle Adresse als ``/32`` auf dem VRRP-Interface.

Ist ``linked_wan_slot`` gesetzt, deaktiviert ``on-master`` sofort die Default-Routen dieses WAN-Slots
und leert dessen Verbindungen – VRRP schaltet in ~3 s, die Netwatch erst nach 10–20 s; ohne das
entstünde ein Blackhole Richtung des ausgefallenen zentralen Gateways. Das Wiedereinschalten bleibt
bei der Netwatch-Hysterese; ``on-backup`` schaltet nur ein, wenn die Netwatch nach der
Recovery-Verzögerung „up“ meldet (Schutz gegen dauerhaft deaktivierte Routen nach VRRP-Flattern).

Manuell angelegte VRRP-Instanzen (ohne ``sdwan:``-Kommentar) werden nicht angefasst.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.db import utcnow
from app.models import Device, VrrpInstance, WanLink
from app.routeros import connect_device
from app.routeros.client import DeviceAPI
from app.services.wan import DEFAULT_OPTIONS, _default_comment, flush_snippet

log = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,40}$")


class VrrpError(ValueError):
    pass


def _tag(inst: VrrpInstance) -> str:
    return inst.id.hex[:8]


def validate_instance(d: dict[str, Any]) -> dict[str, Any]:
    """Prüft eine Instanz (Dict aus API/Template) und normalisiert VIP/Adressen."""
    if not NAME_RE.match(str(d.get("name", ""))):
        raise VrrpError("Name: 1–40 Zeichen, Buchstaben/Ziffern/._-")
    if not NAME_RE.match(str(d.get("interface", ""))):
        raise VrrpError("Ungültiges Parent-Interface")
    vrid = int(d.get("vrid", 0))
    if not 1 <= vrid <= 255:
        raise VrrpError("VRID muss 1–255 sein")
    prio = int(d.get("priority", 100))
    if not 1 <= prio <= 254:
        raise VrrpError("Priorität muss 1–254 sein (255 ist für den Adress-Besitzer reserviert)")
    if int(d.get("version", 3)) not in (2, 3):
        raise VrrpError("Version: 2 oder 3")
    ms = int(d.get("interval_ms", 1000))
    if not 10 <= ms <= 255000:
        raise VrrpError("Intervall: 10 ms – 255 s")
    vip_raw = str(d.get("vip", ""))
    try:
        vip = ipaddress.ip_interface(vip_raw if "/" in vip_raw else f"{vip_raw}/32")
    except ValueError as exc:
        raise VrrpError(f"Ungültige VIP {vip_raw!r}") from exc
    if vip.network.prefixlen != 32:
        raise VrrpError("Die VIP muss /32 sein (Adresse auf dem VRRP-Interface)")
    local = d.get("local_address") or None
    if local:
        try:
            li = ipaddress.ip_interface(str(local))
        except ValueError as exc:
            raise VrrpError(f"Ungültige lokale Adresse {local!r}") from exc
        if li.network.prefixlen == 32:
            raise VrrpError("Lokale Adresse braucht ein Präfix, z. B. /24")
        if vip.ip not in li.network:
            raise VrrpError(f"VIP {vip.ip} liegt nicht im Netz der lokalen Adresse {li.network}")
        if vip.ip == li.ip:
            raise VrrpError("Lokale Adresse und VIP müssen verschieden sein")
        local = li.with_prefixlen
    slot = d.get("linked_wan_slot")
    if slot is not None and not 1 <= int(slot) <= 4:
        raise VrrpError("WAN-Slot 1–4")
    return {**d, "vrid": vrid, "priority": prio, "interval_ms": ms, "vip": vip.with_prefixlen, "local_address": local,
            "linked_wan_slot": int(slot) if slot is not None else None}


def validate_set(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [validate_instance(i) for i in items]
    names = [i["name"] for i in out]
    if len(set(names)) != len(names):
        raise VrrpError("Name mehrfach verwendet")
    keys = [(i["interface"], i["vrid"]) for i in out]
    if len(set(keys)) != len(keys):
        raise VrrpError("VRID auf demselben Interface mehrfach verwendet")
    return out


def _scripts(inst: VrrpInstance, link: WanLink | None, device: Device) -> tuple[str, str]:
    log_m = f':log warning "SD-WAN: VRRP {inst.name} ist MASTER"'
    log_b = f':log info "SD-WAN: VRRP {inst.name} ist BACKUP"'
    if link is None:
        return log_m, log_b
    find = f'[find where comment~"^{_default_comment(link.slot)}"]'
    opts = {**DEFAULT_OPTIONS, **(device.wan_options or {})}
    on_master = f"/ip route disable {find}; {flush_snippet(link, device.wan_mode)}; {log_m}"
    on_backup = (
        f":delay {int(opts['recovery_delay_s'])}s; "
        f':if ([/tool netwatch get [find where comment="sdwan:wan:check:{link.slot}"] status] = "up") do={{ /ip route enable {find} }}; '
        f"{log_b}"
    )
    return on_master, on_backup


def _interval(ms: int) -> str:
    # RouterOS zeigt ganze Sekunden als "1s" an – gleiche Schreibweise hält den Abgleich idempotent
    return f"{ms // 1000}s" if ms % 1000 == 0 else f"{ms}ms"


def build_config(device: Device, instances: list[VrrpInstance], links: list[WanLink]) -> dict[str, list[dict[str, Any]]]:
    by_slot = {lk.slot: lk for lk in links if lk.enabled}
    vrrp: list[dict[str, Any]] = []
    local_addrs: list[dict[str, Any]] = []
    vips: list[dict[str, Any]] = []
    for inst in sorted(instances, key=lambda i: i.name):
        t = _tag(inst)
        on_master, on_backup = _scripts(inst, by_slot.get(inst.linked_wan_slot) if inst.linked_wan_slot else None, device)
        vrrp.append({
            "name": inst.name, "interface": inst.interface, "vrid": inst.vrid, "priority": inst.priority,
            "interval": _interval(inst.interval_ms), "preemption-mode": "yes" if inst.preemption else "no",
            "version": inst.version, "on-master": on_master, "on-backup": on_backup,
            "disabled": "no" if inst.enabled else "yes", "comment": f"sdwan:vrrp:{t}",
        })
        if inst.local_address:
            local_addrs.append({"address": inst.local_address, "interface": inst.interface, "comment": f"sdwan:vrrp:{t}:local"})
        vips.append({"address": inst.vip, "interface": inst.name, "comment": f"sdwan:vrrp:{t}:vip"})
    return {"vrrp": vrrp, "local": local_addrs, "vip": vips}


async def push_config(api: DeviceAPI, cfg: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Reihenfolge: veraltete VIPs weg → lokale Adressen → VRRP-Interfaces → VIPs."""
    names = {v["name"] for v in cfg["vrrp"]}
    existing_vrrp = {str(r.get("name")) for r in await api.print("/interface/vrrp")}
    # 1) Adressen synchronisieren, VIPs nur für Interfaces, die schon existieren (stale VIPs verschwinden)
    early = cfg["local"] + [v for v in cfg["vip"] if v["interface"] in existing_vrrp and v["interface"] in names]
    stats = {"addresses_pre": await api.sync_managed("/ip/address", "vrrp:", early)}
    stats["vrrp"] = await api.sync_managed("/interface/vrrp", "vrrp:", cfg["vrrp"])
    stats["addresses"] = await api.sync_managed("/ip/address", "vrrp:", cfg["local"] + cfg["vip"])
    return stats


async def apply_vrrp(db: AsyncSession, device: Device) -> dict[str, Any]:
    instances = list((await db.execute(select(VrrpInstance).where(VrrpInstance.device_id == device.id))).scalars())
    links = list((await db.execute(select(WanLink).where(WanLink.device_id == device.id))).scalars())
    cfg = build_config(device, instances, links)
    async with connect_device(device) as api:
        stats = await push_config(api, cfg)
    return {"ok": True, "stats": stats}


# ----------------------------------------------------------------------------- Monitoring
def _flag(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


async def vrrp_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    rows = [r for r in await api.print("/interface/vrrp") if str(r.get("comment", "")).startswith("sdwan:vrrp:")]
    if not rows:
        return None
    info: dict[str, str] = {}
    for r in rows:
        tag = str(r["comment"]).split(":")[2]
        if _flag(r.get("disabled")):
            st = "disabled"
        elif _flag(r.get("master")):
            st = "master"
        elif _flag(r.get("backup")):
            st = "backup"
        elif "master" not in r and "backup" not in r:
            # Fallback, falls die RouterOS-Version die Flags nicht als Felder liefert: running = Master aktiv
            st = "master" if _flag(r.get("running")) else "backup"
        else:
            st = "unknown"
        info[tag] = st
    return {"vrrp": info}


async def update_vrrp_status(db: AsyncSession, devices: list[Device]) -> None:
    from app.services.state_log import record_subject_change

    by_id = {d.id: d for d in devices if getattr(d, "_poll_ok", False)}
    if not by_id:
        return
    now = utcnow()
    for inst in (await db.execute(select(VrrpInstance).where(VrrpInstance.device_id.in_(list(by_id))))).scalars():
        dev = by_id[inst.device_id]
        new = ((dev.facts or {}).get("vrrp") or {}).get(_tag(inst))
        if new is None:
            new = "disabled" if not inst.enabled else "unknown"
        if new != inst.state:
            old, inst.state, inst.last_change_at = inst.state, new, now
            if new in ("master", "backup"):
                await record_subject_change(db, inst.tenant_id, dev.id, f"vrrp:{inst.id}", new)
            await events.publish(inst.tenant_id, "vrrp.state", {
                "id": str(inst.id), "device_id": str(dev.id), "device": dev.name, "name": inst.name, "state": new, "previous": old,
            })
