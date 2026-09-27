"""WLAN-Verwaltung (Phase 19).

Treiber-Erkennung: RouterOS 7 kennt zwei WLAN-Pakete – ``wifi`` (neu, ``/interface/wifi``) und ``wireless`` (alt,
``/interface/wireless``). Konfiguriert wird **nur** ``wifi``; für ``wireless`` wird nur gelesen (Status/Clients).
Vor jedem Schreiben wird der Treiber neu erkannt – an einen Router ohne ``wifi`` geht nie ein ``/interface/wifi``-
Schreibbefehl.

Verwaltete Objekte auf dem Router:
* ``/interface/wifi/security|datapath|channel|configuration`` mit ``name=sdwan-wifi-<slug>`` (Erkennung über das
  Namenspräfix ``sdwan-wifi-``).
* Virtuelle APs ``/interface/wifi`` (``master-interface=<Radio>``, Kommentar ``sdwan:wifi:<slug>``). Physische Radios
  (Hauptinterfaces) werden **nie** verändert – vorhandene WLANs bleiben unberührt.
* CAPsMAN-Controller: ``/interface/wifi/provisioning`` je Band (Kommentar ``sdwan:wifi:prov:<band>``), angehängt
  **hinter** vorhandene Regeln, damit bestehende CAPs unverändert provisioniert werden.
* Zeitplan: ``/system/scheduler`` ``sdwan-wifi-<slug>-on|off`` (täglich), RADIUS: ``/radius`` (Kommentar
  ``sdwan:wifi:<slug>``).

ANNAHME (Labor): Pfade und Feldnamen des ``wifi``-Pakets (``authentication-types``, ``passphrase``, ``client-isolation``,
``vlan-id``, ``width``, ``country`` als Ländername, ``hide-ssid``, ``/interface/wifi/radio`` mit ``bands``,
``/interface/wifi/provisioning`` mit ``master-configuration``/``slave-configurations``/``supported-bands``).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, PairingStatus, Tenant, WlanAssignment, WlanDeviceState, WlanProfile
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI, _norm
from app.security import decrypt_secret, encrypt_secret
from app.services.targets import resolve_targets

log = logging.getLogger(__name__)

PREFIX = "sdwan-wifi-"
COMMENT = "sdwan:wifi:"
SECURITY_TYPES = ("wpa2-psk", "wpa2-wpa3-psk", "wpa3-psk", "wpa2-eap", "wpa3-eap")
AUTH_TYPES = {"wpa2-psk": "wpa2-psk", "wpa2-wpa3-psk": "wpa2-psk,wpa3-psk", "wpa3-psk": "wpa3-psk",
              "wpa2-eap": "wpa2-eap", "wpa3-eap": "wpa3-eap"}
WIDTHS = {"auto": None, "20": "20mhz", "40": "20/40mhz", "80": "20/40/80mhz", "160": "20/40/80/160mhz"}
BANDS = ("2ghz", "5ghz", "both")
# ANNAHME (Labor): Bandbezeichnungen für supported-bands der Provisioning-Regeln
PROV_BANDS = {"2ghz": "2ghz-g,2ghz-n,2ghz-ax", "5ghz": "5ghz-a,5ghz-n,5ghz-ac,5ghz-ax"}
POLL_INTERVAL_S = 600
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# ISO-3166 → Ländername, wie ihn das wifi-Paket für ``country`` erwartet (ANNAHME Labor). Normtabelle, keine Kundendaten.
COUNTRIES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "CH": "Switzerland", "CY": "Cyprus", "CZ": "Czech Republic",
    "DE": "Germany", "DK": "Denmark", "EE": "Estonia", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GR": "Greece", "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IS": "Iceland",
    "IT": "Italy", "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MT": "Malta",
    "NL": "Netherlands", "NO": "Norway", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RS": "Serbia",
    "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia", "TR": "Turkey", "UA": "Ukraine", "US": "United States",
    "CA": "Canada", "AU": "Australia", "NZ": "New Zealand", "ZA": "South Africa",
}


class WlanError(ValueError):
    pass


# ----------------------------------------------------------------------------- Validierung / Hilfen
def validate_profile(data: dict[str, Any], has_passphrase: bool, has_radius_secret: bool) -> None:
    if not _SLUG.match(data.get("slug") or ""):
        raise WlanError("Kürzel: a–z, 0–9, '-' (max. 20 Zeichen)")
    ssid = data.get("ssid") or ""
    if not 1 <= len(ssid.encode()) <= 32:
        raise WlanError("SSID: 1–32 Byte")
    if data.get("security") not in SECURITY_TYPES:
        raise WlanError(f"Sicherheit: {', '.join(SECURITY_TYPES)}")
    if data["security"].endswith("-psk"):
        if not has_passphrase:
            raise WlanError("PSK fehlt (8–63 Zeichen)")
    else:
        if not data.get("radius_server") or not has_radius_secret:
            raise WlanError("Enterprise (802.1X) braucht RADIUS-Server und -Secret")
    if data.get("band") not in BANDS:
        raise WlanError("Band: 2ghz | 5ghz | both")
    if data.get("channel_width") not in WIDTHS:
        raise WlanError("Kanalbreite: auto | 20 | 40 | 80 | 160")
    cc = data.get("country_code")
    if cc and cc not in COUNTRIES:
        raise WlanError(f"Ländercode {cc} nicht unterstützt")
    vlan = data.get("vlan_id")
    if vlan is not None and not 1 <= int(vlan) <= 4094:
        raise WlanError("VLAN-ID 1–4094")
    sch = data.get("schedule")
    if sch:
        if not _HHMM.match(str(sch.get("start", ""))) or not _HHMM.match(str(sch.get("end", ""))) or sch["start"] == sch["end"]:
            raise WlanError("Zeitplan: Beginn und Ende als HH:MM, verschieden")
    if data.get("psk_rotate_days") and not data.get("is_guest"):
        raise WlanError("Automatische PSK-Rotation nur für Gäste-WLANs")


def check_passphrase(p: str) -> None:
    if not 8 <= len(p) <= 63 or any(ord(c) < 32 or ord(c) > 126 for c in p):
        raise WlanError("PSK: 8–63 druckbare ASCII-Zeichen")


def generate_psk() -> str:
    """Gut lesbares Gäste-PSK (ohne verwechselbare Zeichen), z. B. ``k7m2-p9x4-t3wq``."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def wifi_qr_payload(ssid: str, psk: str | None, hidden: bool, security: str) -> str:
    def esc(v: str) -> str:
        return re.sub(r'([\\;,:"])', r"\\\1", v)

    t = "WPA" if psk else "nopass"
    return f"WIFI:T:{t};S:{esc(ssid)};" + (f"P:{esc(psk)};" if psk else "") + ("H:true;" if hidden else "") + ";"


def qr_svg(payload: str) -> str:
    import io

    import segno

    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False)
    return buf.getvalue().decode()


def country_for(p: WlanProfile, tenant: Tenant | None) -> str:
    code = p.country_code or (tenant.country_code if tenant else None) or "AT"
    return COUNTRIES.get(code, COUNTRIES["AT"])


def base(p: WlanProfile) -> str:
    return f"{PREFIX}{p.slug}"


# ----------------------------------------------------------------------------- Erkennung
async def detect(api: DeviceAPI) -> dict[str, Any]:
    """Treiber und Radios lesen – nur ``print``-Befehle. Fehlschläge = Paket nicht vorhanden."""
    try:
        rows = await api.print("/interface/wifi")
    except RouterOSError:
        rows = None
    if rows is not None:
        radios_info: dict[str, str] = {}
        try:
            for r in await api.print("/interface/wifi/radio"):
                radios_info[str(r.get("interface", ""))] = str(r.get("bands", ""))
        except RouterOSError:
            pass
        radios = [{"name": r.get("name"), "bands": radios_info.get(str(r.get("name")), ""), "ssid": r.get("configuration.ssid") or r.get("ssid"),
                   "disabled": _norm(r.get("disabled")) == "true", "managed": str(r.get("comment", "")).startswith(COMMENT),
                   "master": r.get("master-interface") or None}
                  for r in rows]
        capsman = cap = False
        try:
            capsman = any(_norm(x.get("enabled")) == "true" for x in await api.print("/interface/wifi/capsman"))
        except RouterOSError:
            pass
        try:
            cap = any(_norm(x.get("enabled")) == "true" for x in await api.print("/interface/wifi/cap"))
        except RouterOSError:
            pass
        return {"driver": "wifi", "radios": radios, "capsman": capsman, "cap": cap}
    try:
        rows = await api.print("/interface/wireless")
    except RouterOSError:
        return {"driver": None, "radios": [], "capsman": False, "cap": False}
    return {"driver": "wireless", "radios": [{"name": r.get("name"), "bands": str(r.get("band", "")), "ssid": r.get("ssid"),
                                              "disabled": _norm(r.get("disabled")) == "true", "managed": False,
                                              "master": r.get("master-interface") or None} for r in rows],
            "capsman": False, "cap": False}


async def wlan_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    """Poll-Hook (alle 10 min): ``facts.wlan``. Ohne WLAN-Paket bleibt ``facts.wlan`` leer (kein WLAN-Tab)."""
    import time

    last = float((device.facts or {}).get("_wlan_at") or 0)
    if time.time() - last < POLL_INTERVAL_S:
        return None
    info = await detect(api)
    clients = None
    if info["driver"]:
        try:
            clients = len(await api.print(f"/interface/{info['driver']}/registration-table"))
        except RouterOSError:
            pass
    has = bool(info["driver"] and (info["radios"] or info["capsman"]))
    return {"_wlan_at": time.time(), "wlan": ({**info, "clients": clients, "checked_at": utcnow().isoformat()} if has else None)}


def is_radio(r: dict[str, Any]) -> bool:
    return not r.get("master")


def radio_band_ok(bands: str, band: str) -> bool:
    if band == "both" or not bands:
        return True  # Bänder unbekannt → alle Radios (ANNAHME Labor)
    return band in bands


# ----------------------------------------------------------------------------- Soll-Zustand
def desired_objects(p: WlanProfile, tenant: Tenant | None) -> dict[str, dict[str, Any]]:
    b = base(p)
    sec: dict[str, Any] = {"name": b, "authentication-types": AUTH_TYPES[p.security]}
    if p.security.endswith("-psk") and p.passphrase_enc:
        sec["passphrase"] = decrypt_secret(p.passphrase_enc)
    dp: dict[str, Any] = {"name": b, "bridge": p.bridge, "client-isolation": "yes" if p.client_isolation else "no"}
    if p.vlan_id:
        dp["vlan-id"] = str(p.vlan_id)
    cfg: dict[str, Any] = {"name": b, "ssid": p.ssid, "country": country_for(p, tenant), "hide-ssid": "yes" if p.hidden else "no",
                           "security": b, "datapath": b, "mode": "ap"}
    out = {"/interface/wifi/security": sec, "/interface/wifi/datapath": dp}
    width = WIDTHS[p.channel_width]
    if width:
        out["/interface/wifi/channel"] = {"name": b, "width": width}
        cfg["channel"] = b
    out["/interface/wifi/configuration"] = cfg
    return out


async def _sync_named(api: DeviceAPI, path: str, want: dict[str, dict[str, Any]], stats: dict[str, int]) -> None:
    """Objekte mit Namenspräfix sdwan-wifi- angleichen (hinzufügen/ändern); Entfernen separat (Reihenfolge)."""
    have = {str(r.get("name")): r for r in await api.print(path) if str(r.get("name", "")).startswith(PREFIX)}
    for name, attrs in want.items():
        row = have.get(name)
        if row is None:
            await api.add(path, **attrs)
            stats["added"] += 1
            continue
        diff = {k: v for k, v in attrs.items() if k != "name" and _norm(row.get(k, "")) != _norm(v)}
        if diff:
            await api.set(path, row[".id"], **diff)
            stats["changed"] += 1


async def _remove_named(api: DeviceAPI, path: str, keep: set[str], stats: dict[str, int]) -> None:
    for r in await api.print(path):
        name = str(r.get("name", ""))
        if name.startswith(PREFIX) and name not in keep:
            await api.remove(path, r[".id"])
            stats["removed"] += 1


async def _sync_commented(api: DeviceAPI, path: str, want: dict[str, dict[str, Any]], stats: dict[str, int], append: bool = False) -> None:
    """Einträge mit Kommentar sdwan:wifi:… angleichen, Schlüssel = Kommentar. Fremde Einträge bleiben unberührt."""
    rows = await api.print(path)
    have = {str(r.get("comment")): r for r in rows if str(r.get("comment", "")).startswith(COMMENT)}
    for key, attrs in want.items():
        row = have.pop(key, None)
        if row is None:
            await api.add(path, **attrs, comment=key)  # ohne place-before → am Ende (hinter vorhandene Regeln)
            stats["added"] += 1
            continue
        diff = {k: v for k, v in attrs.items() if _norm(row.get(k, "")) != _norm(v)}
        if diff:
            await api.set(path, row[".id"], **diff)
            stats["changed"] += 1
    for row in have.values():
        await api.remove(path, row[".id"])
        stats["removed"] += 1


def _schedulers(p: WlanProfile) -> dict[str, dict[str, Any]]:
    if not p.schedule:
        return {}
    b = base(p)
    sel = f'[find where comment="{COMMENT}{p.slug}"]'
    return {f"{COMMENT}{p.slug}:on": {"name": f"{b}-on", "start-time": f"{p.schedule['start']}:00", "interval": "1d",
                                      "on-event": f"/interface wifi enable {sel}"},
            f"{COMMENT}{p.slug}:off": {"name": f"{b}-off", "start-time": f"{p.schedule['end']}:00", "interval": "1d",
                                       "on-event": f"/interface wifi disable {sel}"}}


async def apply_to_router(api: DeviceAPI, device: Device, items: list[tuple[WlanProfile, str]], tenant: Tenant | None) -> dict[str, Any]:
    """Alle dem Gerät zugewiesenen Profile (``(profil, mode)``) herstellen, nicht mehr zugewiesene entfernen."""
    info = await detect(api)
    if info["driver"] != "wifi":
        return {"status": "unsupported_driver" if info["driver"] == "wireless" else "no_wlan", "info": info}
    stats = {"added": 0, "changed": 0, "removed": 0}
    items = [(p, m) for p, m in items if p.enabled]
    objs: dict[str, dict[str, dict[str, Any]]] = {}
    for p, _m in items:
        for path, attrs in desired_objects(p, tenant).items():
            objs.setdefault(path, {})[attrs["name"]] = attrs
    radios = [r for r in info["radios"] if is_radio(r)]
    ifaces: dict[str, dict[str, Any]] = {}
    per_profile: dict[uuid.UUID, dict[str, Any]] = {}
    prov: dict[str, dict[str, Any]] = {}
    for p, mode in items:
        if mode == "capsman":
            if not info["capsman"]:
                per_profile[p.id] = {"status": "error", "error": "CAPsMAN ist auf diesem Gerät nicht aktiviert"}
                continue
            for band in (("2ghz", "5ghz") if p.band == "both" else (p.band,)):
                key = f"{COMMENT}prov:{band}"
                if key not in prov:
                    prov[key] = {"action": "create-dynamic-enabled", "master-configuration": base(p), "supported-bands": PROV_BANDS[band]}
                else:
                    slaves = [s for s in str(prov[key].get("slave-configurations", "")).split(",") if s]
                    prov[key]["slave-configurations"] = ",".join([*slaves, base(p)])
            per_profile[p.id] = {"status": "ok", "interfaces": [], "provisioning": True}
            continue
        used = [r for r in radios if radio_band_ok(str(r.get("bands", "")), p.band)]
        if not used:
            per_profile[p.id] = {"status": "error", "error": "Kein passendes Radio für das gewählte Band"}
            continue
        names = []
        for r in used:
            name = f"{base(p)}-{r['name']}"[:60]
            ifaces[f"{COMMENT}{p.slug}:{r['name']}"] = {"name": name, "master-interface": r["name"], "configuration": base(p), "disabled": "no"}
            names.append(name)
        per_profile[p.id] = {"status": "ok", "interfaces": names,
                             "radios_disabled": [r["name"] for r in used if r.get("disabled")]}
    # Reihenfolge: Unterobjekte → Konfiguration → Interfaces/Provisioning → Zeitplan/RADIUS; Entfernen umgekehrt
    for path in ("/interface/wifi/security", "/interface/wifi/datapath", "/interface/wifi/channel", "/interface/wifi/configuration"):
        await _sync_named(api, path, objs.get(path, {}), stats)
    # Interfaces: Schlüssel Kommentar, Präfix je Profil
    await _sync_commented_ifaces(api, ifaces, stats)
    if info["capsman"] or prov:
        await _sync_commented(api, "/interface/wifi/provisioning", prov, stats)
    sched: dict[str, dict[str, Any]] = {}
    radius: dict[str, dict[str, Any]] = {}
    for p, _m in items:
        sched.update(_schedulers(p))
        if p.security.endswith("-eap") and p.radius_server and p.radius_secret_enc:
            radius[f"{COMMENT}{p.slug}"] = {"service": "wireless", "address": p.radius_server,
                                           "authentication-port": str(p.radius_port), "secret": decrypt_secret(p.radius_secret_enc)}
    await _sync_commented(api, "/system/scheduler", sched, stats)
    await _sync_commented(api, "/radius", radius, stats)
    for path in ("/interface/wifi/configuration", "/interface/wifi/channel", "/interface/wifi/datapath", "/interface/wifi/security"):
        await _remove_named(api, path, set(objs.get(path, {})), stats)
    return {"status": "ok", "stats": stats, "profiles": per_profile, "info": info}


async def _sync_commented_ifaces(api: DeviceAPI, want: dict[str, dict[str, Any]], stats: dict[str, int]) -> None:
    """Nur virtuelle Interfaces mit Kommentar sdwan:wifi: – Radios (ohne master-interface) werden nie angefasst."""
    rows = [r for r in await api.print("/interface/wifi") if r.get("master-interface") and str(r.get("comment", "")).startswith(COMMENT)]
    have = {str(r.get("comment")): r for r in rows}
    for key, attrs in want.items():
        row = have.pop(key, None)
        if row is None:
            await api.add("/interface/wifi", **attrs, comment=key)
            stats["added"] += 1
            continue
        diff = {k: v for k, v in attrs.items() if k != "disabled" and _norm(row.get(k, "")) != _norm(v)}
        if diff:
            await api.set("/interface/wifi", row[".id"], **diff)
            stats["changed"] += 1
    for row in have.values():
        await api.remove("/interface/wifi", row[".id"])
        stats["removed"] += 1


# ----------------------------------------------------------------------------- Zuweisungen / Ausrollen
async def device_items(db: AsyncSession, device: Device) -> list[tuple[WlanProfile, str]]:
    out: list[tuple[WlanProfile, str]] = []
    rows = (await db.execute(select(WlanAssignment, WlanProfile).join(WlanProfile, WlanProfile.id == WlanAssignment.profile_id)
                             .where(WlanAssignment.tenant_id == device.tenant_id))).all()
    for a, p in rows:
        if any(d.id == device.id for d in await resolve_targets(db, a.targets or {}, device.tenant_id)):
            if all(x[0].id != p.id for x in out):
                out.append((p, a.mode))
    return out


async def profile_devices(db: AsyncSession, profile: WlanProfile) -> list[tuple[Device, str]]:
    out: dict[uuid.UUID, tuple[Device, str]] = {}
    for a in (await db.execute(select(WlanAssignment).where(WlanAssignment.profile_id == profile.id))).scalars():
        for d in await resolve_targets(db, a.targets or {}, profile.tenant_id):
            out.setdefault(d.id, (d, a.mode))
    return sorted(out.values(), key=lambda x: x[0].name)


async def _state(db: AsyncSession, device: Device, profile_id: uuid.UUID | None) -> WlanDeviceState:
    st = (await db.execute(select(WlanDeviceState).where(WlanDeviceState.device_id == device.id,
                                                         WlanDeviceState.profile_id.is_(None) if profile_id is None
                                                         else WlanDeviceState.profile_id == profile_id))).scalar_one_or_none()
    if st is None:
        st = WlanDeviceState(tenant_id=device.tenant_id, device_id=device.id, profile_id=profile_id)
        db.add(st)
    return st


async def apply_device(db: AsyncSession, device: Device) -> dict[str, Any]:
    """Gerät vollständig abgleichen und Status je Profil speichern."""
    items = await device_items(db, device)
    tenant = await db.get(Tenant, device.tenant_id)
    now = utcnow()
    states = {p.id: await _state(db, device, p.id) for p, _ in items}
    for p, m in items:
        states[p.id].mode = m
    # Status-Zeilen nicht mehr zugewiesener Profile: erst nach erfolgreichem Abgleich löschen (sie merken sich,
    # dass auf dem Router noch etwas zu entfernen ist)
    stale = [st for st in (await db.execute(select(WlanDeviceState).where(WlanDeviceState.device_id == device.id))).scalars()
             if st.profile_id is not None and st.profile_id not in states]
    if device.pairing_status != PairingStatus.paired or device.status == DeviceStatus.offline:
        for st in states.values():
            st.status, st.error = "offline", "Gerät nicht erreichbar – wird beim nächsten Ausrollen übertragen"
        return {"status": "offline"}
    if items:  # Phase 23: Sicherheitsmeldung für WLAN → nicht ausrollen (Entfernen ohne Profile bleibt erlaubt)
        from app.services.advisories import block_message, blocking

        advs = await blocking(db, device, "wlan")
        if advs:
            for st in states.values():
                st.status, st.error = "blocked", block_message(device, advs)
            return {"status": "blocked", "error": block_message(device, advs)}
    try:
        async with connect_device(device) as api:
            res = await apply_to_router(api, device, items, tenant)
    except RouterOSError as exc:
        for st in states.values():
            st.status, st.error = "error", str(exc)
        return {"status": "error", "error": str(exc)}
    if res["status"] != "ok":
        msg = {"unsupported_driver": "Treiber „wireless“: nur Anzeige, Konfiguration nicht unterstützt",
               "no_wlan": "Kein WLAN-Paket auf dem Gerät"}[res["status"]]
        for st in states.values():
            st.status, st.error = res["status"], msg
        return res
    for st in stale:
        await db.delete(st)
    for p, _m in items:
        st, pr = states[p.id], res["profiles"].get(p.id) or {"status": "ok"}
        if not p.enabled:
            st.status, st.error, st.detail = "ok", None, {"disabled": True}
            st.applied_version, st.applied_at = p.version, now
            continue
        st.status, st.error = pr["status"], pr.get("error")
        st.detail = {k: v for k, v in pr.items() if k not in ("status", "error")}
        if pr["status"] == "ok":
            st.applied_version, st.applied_at = p.version, now
    return res


async def apply_devices_bg(tenant_id: uuid.UUID, device_ids: list[uuid.UUID]) -> None:
    async with system_session() as db:
        for did in device_ids:
            dev = await db.get(Device, did)
            if dev is None or dev.tenant_id != tenant_id:
                continue
            try:
                await apply_device(db, dev)
            except Exception:  # noqa: BLE001 - nächstes Gerät trotzdem
                log.exception("WLAN-Abgleich %s", dev.name)
            await db.commit()


async def rotate_psk(profile: WlanProfile) -> str:
    psk = generate_psk()
    profile.passphrase_enc = encrypt_secret(psk)
    profile.psk_rotated_at = utcnow()
    profile.version += 1
    return psk


async def rotation_tick() -> None:
    """Worker-Job (täglich): automatische Gäste-PSK-Rotation (opt-in je Profil) und Ausrollen."""
    from app.audit import audit

    async with system_session() as db:
        rows = (await db.execute(select(WlanProfile).where(WlanProfile.is_guest.is_(True), WlanProfile.psk_rotate_days.is_not(None)))).scalars().all()
        for p in rows:
            last = p.psk_rotated_at or p.created_at
            if not p.psk_rotate_days or utcnow() - last < dt.timedelta(days=p.psk_rotate_days):
                continue
            await rotate_psk(p)
            await audit(db, "wlan.psk.rotate", tenant_id=p.tenant_id, target_type="wlan_profile", target_id=p.id, details={"auto": True})
            await db.commit()
            devs = [d.id for d, _ in await profile_devices(db, p)]
            await apply_devices_bg(p.tenant_id, devs)


# ----------------------------------------------------------------------------- Status (live)
async def live_status(api: DeviceAPI) -> dict[str, Any]:
    info = await detect(api)
    clients: list[dict[str, Any]] = []
    channels: dict[str, Any] = {}
    if info["driver"] == "wifi":
        try:
            for c in await api.print("/interface/wifi/registration-table"):
                clients.append({"interface": c.get("interface"), "mac": c.get("mac-address"), "signal": c.get("signal"),
                                "tx_rate": c.get("tx-rate"), "rx_rate": c.get("rx-rate"), "uptime": c.get("uptime"), "ssid": c.get("ssid")})
        except RouterOSError:
            pass
        for r in info["radios"]:
            if not is_radio(r) or r.get("disabled"):
                continue
            try:  # ANNAHME (Labor): monitor once liefert den aktuellen Kanal ohne Scan
                mon = await api.call("/interface/wifi/monitor", numbers=r["name"], once="")
                if mon:
                    channels[r["name"]] = mon[0].get("channel")
            except RouterOSError:
                pass
    elif info["driver"] == "wireless":
        try:
            for c in await api.print("/interface/wireless/registration-table"):
                clients.append({"interface": c.get("interface"), "mac": c.get("mac-address"), "signal": c.get("signal-strength"),
                                "tx_rate": c.get("tx-rate"), "rx_rate": c.get("rx-rate"), "uptime": c.get("uptime"), "ssid": None})
        except RouterOSError:
            pass
        try:
            for r in await api.print("/interface/wireless"):
                channels[str(r.get("name"))] = r.get("frequency")
        except RouterOSError:
            pass
    caps: list[dict[str, Any]] = []
    if info.get("capsman"):
        try:
            caps = [{"name": c.get("name") or c.get("identity"), "address": c.get("address"), "state": c.get("state")}
                    for c in await api.print("/interface/wifi/capsman/remote-cap")]
        except RouterOSError:
            pass
    return {**info, "clients": clients, "channels": channels, "caps": caps}
