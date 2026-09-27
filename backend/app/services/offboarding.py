"""Offboarding: Gerät sauber aus der Verwaltung nehmen.

Zwei Wege:
* ``clean`` (Standard, nur online): Router bereinigen, dann aus der Plattform entfernen.
* ``platform_only``: nur aus der Plattform entfernen; der Router bleibt unverändert (verwaltete Konfiguration und
  deaktivierte defconf-Regeln bleiben bestehen).

Bereinigen – feste Reihenfolge, jeder Schritt wird protokolliert; schlägt ein Schritt vor 6 fehl, wird abgebrochen und
das Gerät bleibt in der Plattform:

1. Backup (Auslöser ``offboarding``).
2. Von der Plattform deaktivierte defconf-Regeln wieder aktivieren – ZUERST, damit der Router nie ohne Firewall dasteht.
3. Verwaltete Objekte (Kommentar ``sdwan:`` bzw. Name ``sdwan-``) entfernen: Firewall (Drop-Regeln zuerst), NAT/Mangle
   außer WAN, Address-Lists (auch Feeds), Zonen-Listen, WLAN, Hotspot, Syslog, Scheduler, Scripts, VRRP, Mesh, DNS.
4. Von der Plattform geänderte Dienste (www/winbox/ssh) auf den Ursprungszustand (aus den Fernzugriffs-Sitzungen).
5. Temporäre Fernzugriffs-Benutzer und Gruppe ``sdwan-remote`` entfernen.
6. ZULETZT per einmaligem Scheduler auf dem Router (die Plattform verliert dabei den Zugang): alles, wovon der
   Management-Tunnel abhängt – WAN (Netwatch, Mangle, NAT, Routen, Routing-Tabellen, Liste ``sdwan-wan``), ZTP-LAN –,
   danach API-Benutzer, Gruppe ``sdwan-api`` und Management-Tunnel. Der Scheduler wiederholt sich jede Minute, bis er
   sich zum Schluss selbst entfernt (alle Befehle sind wiederholbar).

ANNAHME (Labor): RouterOS-Script-Syntax ``remove [find where comment~"^sdwan:"]`` und das Weiterlaufen des Schedulers,
nachdem der API-Benutzer und der Tunnel entfernt wurden.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import ConfigBackup, Device, DeviceStatus, OffboardingArchive, PairingStatus, RemoteSession
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI, _norm
from app.routeros.schema import API_GROUP, REMOTE_GROUP

log = logging.getLogger(__name__)

ARCHIVE_DAYS = 90
FINAL_SCHEDULER = "sdwan-offboard"
MGMT_IFACE_COMMENT = "sdwan:mgmt"
# Kommentare, die erst in Schritt 5/6 entfernt werden (Tunnel-Abhängigkeiten, Zugang)
_LATER = ("sdwan:mgmt", "sdwan:hub", "sdwan:remote", "sdwan:wan", "sdwan:ztp")
# Pfade aus optionalen Paketen: fehlen sie, gibt es dort nichts zu bereinigen
_PACKAGE_PATHS = ("/interface/wifi", "/interface/wireless")


class OffboardError(RuntimeError):
    pass


def _c(r: dict[str, Any]) -> str:
    return str(r.get("comment", "") or "")


def _n(r: dict[str, Any]) -> str:
    return str(r.get("name", "") or "")


def _managed_comment(r: dict[str, Any]) -> bool:
    c = _c(r)
    return c.startswith("sdwan:") and not c.startswith(_LATER)


def _managed_name(prefix: str, keep: tuple[str, ...] = ()) -> Any:
    return lambda r: _n(r).startswith(prefix) and _n(r) not in keep and not _c(r).startswith(_LATER)


# Schritt 3: (Pfad, Bezeichnung, Auswahl). Reihenfolge = Abhängigkeiten (Nutzer vor Genutztem).
STEP3: list[tuple[str, str, Any]] = [
    ("/ip/firewall/filter", "Firewall-Filter", _managed_comment),
    ("/ip/firewall/nat", "NAT (ohne WAN)", _managed_comment),
    ("/ip/firewall/mangle", "Mangle (ohne WAN)", _managed_comment),
    ("/ip/firewall/address-list", "Address-Lists (inkl. Feeds, Objekte)", _managed_comment),
    ("/ipv6/firewall/address-list", "IPv6-Address-Lists (Feeds)", _managed_comment),
    ("/ip/hotspot/user", "Hotspot-Voucher", _managed_comment),
    ("/ip/hotspot/ip-binding", "Hotspot-Sperren", _managed_comment),
    ("/ip/hotspot/walled-garden", "Walled Garden", _managed_comment),
    ("/ip/hotspot/walled-garden/ip", "Walled Garden (IP)", _managed_comment),
    ("/ip/hotspot", "Hotspot-Server", _managed_name("sdwan-hs-")),
    ("/ip/hotspot/user/profile", "Hotspot-Benutzerprofile", _managed_name("sdwan-hs-")),
    ("/ip/hotspot/profile", "Hotspot-Profile", _managed_name("sdwan-hs-")),
    ("/file", "Hotspot-Login-Seiten", _managed_name("sdwan-hs-")),
    ("/interface/wifi", "WLAN (virtuelle APs)", lambda r: bool(r.get("master-interface")) and _managed_comment(r)),
    ("/interface/wifi/provisioning", "CAPsMAN-Provisioning", _managed_comment),
    ("/interface/wifi/configuration", "WLAN-Konfigurationen", _managed_name("sdwan-wifi-")),
    ("/interface/wifi/channel", "WLAN-Kanäle", _managed_name("sdwan-wifi-")),
    ("/interface/wifi/datapath", "WLAN-Datapath", _managed_name("sdwan-wifi-")),
    ("/interface/wifi/security", "WLAN-Sicherheit", _managed_name("sdwan-wifi-")),
    ("/radius", "RADIUS (WLAN)", _managed_comment),
    ("/system/logging", "Syslog-Regeln", lambda r: r.get("action") == "sdwan-syslog"),
    ("/system/logging/action", "Syslog-Aktion", lambda r: _n(r) == "sdwan-syslog"),
    ("/system/scheduler", "Scheduler", lambda r: (_managed_comment(r) or _managed_name("sdwan-")(r)) and _n(r) != FINAL_SCHEDULER),
    ("/system/script", "Scripts", lambda r: _managed_comment(r) or _managed_name("sdwan-")(r)),
    ("/ip/address", "Adressen (VRRP, Mesh)", _managed_comment),
    ("/interface/vrrp", "VRRP", _managed_comment),
    ("/interface/wireguard/peers", "Mesh-Peers", _managed_comment),
    ("/interface/wireguard", "Mesh-Interface", lambda r: _n(r).startswith("sdwan-") and _n(r) != get_settings().wg_device_interface
     and not _c(r).startswith(_LATER)),
    ("/ip/dns/static", "DNS (Content-Filter)", _managed_comment),
    ("/interface/list/member", "Zonen-Listen (Mitglieder)", _managed_comment),
    ("/interface/list", "Zonen-Listen", lambda r: (_n(r).startswith("sdwan-") and _n(r) != "sdwan-wan") and not _c(r).startswith(_LATER)),
]

# Schritt 6 (Script auf dem Router): (RouterOS-Pfad, Filter) – Reihenfolge wie bei der Einrichtung rückwärts
STEP6: list[tuple[str, str]] = [
    ("/tool netwatch", 'comment~"^sdwan:"'),
    ("/ip firewall mangle", 'comment~"^sdwan:"'),
    ("/ip firewall nat", 'comment~"^sdwan:"'),
    ("/ip route", 'comment~"^sdwan:"'),
    ("/routing table", 'comment~"^sdwan:"'),
    ("/ip firewall address-list", 'comment~"^sdwan:"'),
    ("/interface list member", 'comment~"^sdwan:"'),
    ("/interface list", 'comment~"^sdwan:"'),
    ("/ip dhcp-server network", 'comment~"^sdwan:"'),
    ("/ip dhcp-server", 'comment~"^sdwan:"'),
    ("/ip pool", 'comment~"^sdwan:"'),
    ("/ip firewall filter", 'comment~"^sdwan:"'),
    ("/user", 'comment~"^sdwan:"'),
    ("/user group", 'comment~"^sdwan:"'),
    ("/ip address", 'comment~"^sdwan:"'),
    ("/interface wireguard peers", 'comment~"^sdwan:"'),
    ("/interface wireguard", 'comment~"^sdwan:"'),
]


def final_script() -> str:
    cmds = [f"{p} remove [find where {f}]" for p, f in STEP6]
    # Management-Interface zusätzlich über den Namen (falls ohne Kommentar angelegt)
    cmds.append(f'/interface wireguard remove [find where name="{get_settings().wg_device_interface}"]')
    cmds.append(f'/system scheduler remove [find name="{FINAL_SCHEDULER}"]')
    # ``:do {} on-error={}``: ein fehlender Pfad (z. B. ohne DHCP-Server) bricht den Rest nicht ab
    return "; ".join(f":do {{ {c} }} on-error={{}}" for c in cmds)


def _step(steps: list[dict[str, Any]], no: int, label: str, ok: bool, detail: Any = None) -> dict[str, Any]:
    s = {"step": no, "label": label, "ok": ok, "detail": detail, "at": utcnow().isoformat()}
    steps.append(s)
    return s


async def _print_opt(api: DeviceAPI, path: str) -> list[dict[str, Any]] | None:
    try:
        return await api.print(path)
    except RouterOSError:
        if path.startswith(_PACKAGE_PATHS):
            return None  # Paket nicht vorhanden
        raise


async def preview(db: AsyncSession, device: Device) -> dict[str, Any]:
    """Was würde das Bereinigen entfernen? (live, nur lesend) plus Warnungen."""
    from app.services.fw_defconf import remembered_rows

    out: dict[str, Any] = {"online": device.status != DeviceStatus.offline and device.pairing_status == PairingStatus.paired,
                           "counts": {}, "final": {}, "warnings": [], "defconf_disabled": len(await remembered_rows(db, device.id)),
                           "error": None}
    if not out["online"]:
        return out
    try:
        async with connect_device(device) as api:
            for path, label, sel in STEP3:
                rows = await _print_opt(api, path)
                n = sum(1 for r in rows or [] if sel(r))
                if n:
                    out["counts"][label] = n
            routes = await api.print("/ip/route")
            nat = await api.print("/ip/firewall/nat")
            wan_routes = [r for r in routes if _c(r).startswith("sdwan:wan:default")]
            if wan_routes:
                out["final"]["WAN-Routen"] = len(wan_routes)
                own_default = [r for r in routes if r.get("dst-address") == "0.0.0.0/0" and not _c(r).startswith("sdwan:")
                               and _norm(r.get("disabled", "false")) != "true"]
                if not own_default:
                    out["warnings"].append("Der Router hat außer den Plattform-WAN-Routen keine Default-Route (auch keine per DHCP) – "
                                           "nach dem Bereinigen hat er keinen Internetzugang.")
            wan_nat = [r for r in nat if _c(r).startswith("sdwan:wan:")]
            if wan_nat:
                out["final"]["WAN-NAT"] = len(wan_nat)
                if not [r for r in nat if r.get("action") == "masquerade" and not _c(r).startswith("sdwan:")]:
                    out["warnings"].append("Kein eigenes Masquerade außerhalb der Plattform – Clients im LAN erreichen danach das Internet nicht.")
            ztp = [r for r in await api.print("/ip/address") if _c(r).startswith("sdwan:ztp")]
            if ztp:
                out["final"]["ZTP-LAN"] = len(ztp)
                out["warnings"].append("Per Zero-Touch eingerichtete LAN-Adressen/DHCP werden entfernt.")
            if any(_c(r).startswith("sdwan:dns") for r in await api.print("/ip/dns/static")):
                out["warnings"].append("DNS-Server-Einstellungen des Content-Filters bleiben unverändert – bitte manuell prüfen.")
    except RouterOSError as exc:
        out["error"] = str(exc)
    return out


async def _step3(api: DeviceAPI) -> dict[str, int]:
    removed: dict[str, int] = {}
    for path, label, sel in STEP3:
        rows = await _print_opt(api, path)
        if rows is None:
            continue
        sel_rows = [r for r in rows if sel(r)]
        if path == "/ip/firewall/filter":  # zuerst verwerfende Regeln, damit kein Zwischenstand Zugriffe blockiert
            sel_rows.sort(key=lambda r: 0 if r.get("action") in ("drop", "reject", "tarpit") else 1)
        for r in sel_rows:
            await api.remove(path, r[".id"])
        if sel_rows:
            removed[label] = len(sel_rows)
    return removed


async def _step4(db: AsyncSession, api: DeviceAPI, device: Device) -> list[dict[str, Any]]:
    """Ursprungszustand je Dienst: aus der ältesten Sitzung, die ihn geändert und nicht zurückgestellt hat."""
    sessions = (await db.execute(select(RemoteSession).where(RemoteSession.device_id == device.id)
                                 .order_by(RemoteSession.created_at))).scalars().all()
    done: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in sessions:
        st = dict(s.service_restore or {})
        if st.get("changed") and not st.get("restored") and st.get("service") and st["service"] not in seen:
            seen.add(st["service"])
            for r in await api.print("/ip/service", name=st["service"]):
                await api.set("/ip/service", r[".id"], disabled=st["disabled"], address=st["address"])
            done.append({"service": st["service"], "disabled": st["disabled"], "address": st["address"]})
        if st.get("changed"):
            s.service_restore = {**st, "pending": False, "restored": True}
    return done


async def _step5(db: AsyncSession, api: DeviceAPI, device: Device, by: str) -> dict[str, int]:
    users = [u for u in await api.print("/user") if _c(u).startswith("sdwan:remote")]
    for u in users:
        await api.remove("/user", u[".id"])
    groups = [g for g in await api.print("/user/group") if _n(g) == REMOTE_GROUP]
    for g in groups:
        await api.remove("/user/group", g[".id"])
    for s in (await db.execute(select(RemoteSession).where(RemoteSession.device_id == device.id, RemoteSession.status == "active"))).scalars():
        s.status, s.closed_at, s.closed_by = "closed", utcnow(), by
    return {"users": len(users), "groups": len(groups)}


async def _step6(api: DeviceAPI) -> dict[str, Any]:
    from app.routeros.util import format_router_date, format_router_time, parse_router_datetime

    clock = (await api.print("/system/clock") or [{}])[0]
    parsed = parse_router_datetime(clock.get("date"), clock.get("time"))
    if parsed is None:
        raise RouterOSError("Router-Uhrzeit nicht lesbar")
    now, fmt = parsed
    at = now + dt.timedelta(seconds=30)
    for r in await api.print("/system/scheduler", name=FINAL_SCHEDULER):
        await api.remove("/system/scheduler", r[".id"])
    await api.add("/system/scheduler", name=FINAL_SCHEDULER, **{"start-date": format_router_date(at, fmt), "start-time": format_router_time(at),
                                                               "interval": "1m", "on-event": final_script()})
    return {"scheduler": FINAL_SCHEDULER, "start": f"{format_router_date(at, fmt)} {format_router_time(at)}",
            "removes": [p for p, _ in STEP6], "api_group": API_GROUP}


async def offboard(db: AsyncSession, device: Device, mode: str, by: str) -> dict[str, Any]:
    """Führt das Offboarding aus. Rückgabe {ok, steps, archive_id}. Bei ``ok=False`` bleibt das Gerät bestehen."""
    from app.services import fw_defconf
    from app.services.backup import BackupError, take_backup

    steps: list[dict[str, Any]] = []
    backup: ConfigBackup | None = None
    if mode == "platform_only":
        backup = (await db.execute(select(ConfigBackup).where(ConfigBackup.device_id == device.id)
                                   .order_by(ConfigBackup.created_at.desc()).limit(1))).scalar_one_or_none()
        _step(steps, 0, "Nur aus der Plattform entfernt – Router unverändert", True,
              {"archiviertes Backup": str(backup.id) if backup else None})
    else:
        if device.status == DeviceStatus.offline or device.pairing_status != PairingStatus.paired:
            raise OffboardError("Bereinigen ist nur bei erreichbaren Geräten möglich")
        # 1. Backup
        try:
            backup, _ = await take_backup(db, device, "offboarding", note="vor dem Offboarding", created_by=by)
            _step(steps, 1, "Backup (offboarding)", True, {"backup_id": str(backup.id), "sha256": backup.sha256})
        except (BackupError, RouterOSError, OSError) as exc:
            _step(steps, 1, "Backup (offboarding)", False, str(exc))
            return {"ok": False, "steps": steps, "archive_id": None}
        step = 2
        try:
            async with connect_device(device) as api:
                # 2. defconf zuerst wieder aktivieren
                rows = await fw_defconf.remembered_rows(db, device.id)
                res = await fw_defconf.enable(api, fw_defconf.records(rows)) if rows else {"enabled": [], "missing": [], "ambiguous": []}
                await fw_defconf.apply_result(db, device.id, res)
                if res.get("ambiguous"):
                    _step(steps, 2, "defconf-Regeln wieder aktivieren", False,
                          {**res, "error": "nicht eindeutig zuordenbare Regeln – bitte manuell prüfen, dann erneut offboarden"})
                    return {"ok": False, "steps": steps, "archive_id": None}
                _step(steps, 2, "defconf-Regeln wieder aktivieren", True, res)
                step = 3
                _step(steps, 3, "Verwaltete Objekte entfernen", True, await _step3(api))
                step = 4
                _step(steps, 4, "Dienste auf Ursprungszustand", True, await _step4(db, api, device))
                step = 5
                _step(steps, 5, "Fernzugriffs-Benutzer und Gruppe sdwan-remote entfernen", True, await _step5(db, api, device, by))
                step = 6
                _step(steps, 6, "API-Benutzer, sdwan-api, Management-Tunnel (Scheduler auf dem Router)", True, await _step6(api))
        except RouterOSError as exc:
            _step(steps, step, "Abgebrochen", False, str(exc))
            return {"ok": False, "steps": steps, "archive_id": None}
    archive = OffboardingArchive(
        tenant_id=device.tenant_id, device_name=device.name, serial=device.serial, model=device.model,
        routeros_version=device.routeros_version, mode=mode, backup_created_at=backup.created_at if backup else None,
        content=backup.content if backup else None, sha256=backup.sha256 if backup else None, steps=steps, created_by=by,
        expires_at=utcnow() + dt.timedelta(days=ARCHIVE_DAYS))
    db.add(archive)
    await db.flush()
    return {"ok": True, "steps": steps, "archive_id": str(archive.id)}


async def purge_archives() -> int:
    async with system_session() as db:
        res = await db.execute(delete(OffboardingArchive).where(OffboardingArchive.expires_at < utcnow()))
        await db.commit()
        return res.rowcount or 0
