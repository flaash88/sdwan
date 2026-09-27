"""Sicherheitsmeldungen (Phase 23): Versionsbereiche, aktive Funktionen je Gerät, Betroffenheit, Blockade.

* Betroffen: ``affected_from <= Version`` UND (``affected_to`` leer ODER ``Version <= affected_to``) UND
  (``fixed_in`` leer ODER ``Version < fixed_in``).
* Gilt für ein Gerät, wenn die Version im Bereich liegt UND (Funktion ``general`` ODER Funktion auf dem Gerät aktiv).
  Ist nicht feststellbar, ob die Funktion aktiv ist → „möglicherweise betroffen“ (Anzeige, kein Alarm).
* Blockade: Neue Aktivierung/Ausrollen einer Funktion (Hotspot, WLAN …) wird verweigert, wenn eine Meldung mit
  Schweregrad high/critical für diese Funktion die Geräteversion betrifft. Entfernen bleibt erlaubt.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, HotspotInstance, SecurityAdvisory, VrrpInstance, WlanDeviceState

FUNCTIONS = ("general", "hotspot", "wlan", "vrrp", "wireguard", "dns", "rest-api", "api", "winbox", "www", "ssh", "other")
SEVERITIES = ("low", "medium", "high", "critical")
BLOCKING = ("high", "critical")
_STAGE = {"beta": 0, "rc": 1}


class AdvisoryError(ValueError):
    pass


def parse_version(v: str | None) -> tuple[int, int, int, int, int] | None:
    """``7.15.3`` → (7, 15, 3, 2, 0); ``7.16rc2`` → (7, 16, 0, 1, 2); ``7.16beta1`` → (7, 16, 0, 0, 1)."""
    if not v:
        return None
    m = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?\s*(?:(beta|rc)(\d+))?", str(v))
    if not m:
        return None
    stage = _STAGE.get(m.group(4) or "", 2)
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), stage, int(m.group(5) or 0)


def validate(data: dict[str, Any]) -> None:
    if data.get("function") not in FUNCTIONS:
        raise AdvisoryError(f"Funktion: {', '.join(FUNCTIONS)}")
    if data.get("severity") not in SEVERITIES:
        raise AdvisoryError(f"Schweregrad: {', '.join(SEVERITIES)}")
    lo = parse_version(data.get("affected_from"))
    if lo is None:
        raise AdvisoryError("„Betroffen ab“ als Version, z. B. 7.10 oder 7.14.2")
    for k in ("affected_to", "fixed_in"):
        if data.get(k) and parse_version(data[k]) is None:
            raise AdvisoryError(f"{k}: Version, z. B. 7.15.3")
    if not data.get("affected_to") and not data.get("fixed_in"):
        raise AdvisoryError("„Betroffen bis“ oder „Behoben in“ angeben")
    if data.get("fixed_in") and parse_version(data["fixed_in"]) <= lo:
        raise AdvisoryError("„Behoben in“ muss nach „Betroffen ab“ liegen")


def version_affected(adv: SecurityAdvisory, version: str | None) -> bool:
    v = parse_version(version)
    lo = parse_version(adv.affected_from)
    if v is None or lo is None or v < lo:
        return False
    hi = parse_version(adv.affected_to)
    if hi is not None and v > hi:
        return False
    fix = parse_version(adv.fixed_in)
    return not (fix is not None and v >= fix)


async def device_functions(db: AsyncSession, device: Device) -> dict[str, bool | None]:
    """Aktive Funktionen: True/False, None = unbekannt."""
    facts = device.facts or {}
    services = facts.get("services")
    out: dict[str, bool | None] = {"general": True, "wireguard": True, "api": True}  # Mgmt-Tunnel + API immer aktiv
    out["hotspot"] = (await db.execute(select(HotspotInstance.id).where(HotspotInstance.device_id == device.id).limit(1))).first() is not None
    out["wlan"] = bool(facts.get("wlan")) or (await db.execute(
        select(WlanDeviceState.id).where(WlanDeviceState.device_id == device.id).limit(1))).first() is not None
    out["vrrp"] = (await db.execute(select(VrrpInstance.id).where(VrrpInstance.device_id == device.id).limit(1))).first() is not None
    if isinstance(services, dict):
        for name, key in (("winbox", "winbox"), ("ssh", "ssh"), ("www", "www"), ("rest-api", "www-ssl")):
            s = services.get(key)
            out[name] = bool(s) and not s.get("disabled", False)
        if services.get("www") and not services["www"].get("disabled"):
            out["rest-api"] = True  # REST läuft über www bzw. www-ssl
    else:
        for name in ("winbox", "ssh", "www", "rest-api"):
            out[name] = None
    out.setdefault("dns", None)
    out.setdefault("other", None)
    return out


def match(adv: SecurityAdvisory, device: Device, funcs: dict[str, bool | None]) -> str | None:
    """None | 'affected' | 'possible'."""
    if not adv.enabled or not version_affected(adv, device.routeros_version):
        return None
    active = funcs.get(adv.function)
    if adv.function == "general" or active is True:
        return "affected"
    return "possible" if active is None else None


def advisory_out(a: SecurityAdvisory) -> dict[str, Any]:
    return {"id": str(a.id), "cve": a.cve, "title": a.title, "description": a.description, "function": a.function, "severity": a.severity,
            "affected_from": a.affected_from, "affected_to": a.affected_to, "fixed_in": a.fixed_in, "link": a.link, "enabled": a.enabled,
            "builtin": a.builtin}


async def enabled_advisories(db: AsyncSession) -> list[SecurityAdvisory]:
    return list((await db.execute(select(SecurityAdvisory).where(SecurityAdvisory.enabled.is_(True)))).scalars())


async def device_advisories(db: AsyncSession, device: Device, advs: list[SecurityAdvisory] | None = None) -> list[dict[str, Any]]:
    advs = advs if advs is not None else await enabled_advisories(db)
    if not advs:
        return []
    funcs = await device_functions(db, device)
    out = []
    for a in advs:
        st = match(a, device, funcs)
        if st:
            out.append({**advisory_out(a), "status": st})
    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    return sorted(out, key=lambda x: (x["status"] != "affected", order[x["severity"]]))


async def blocking(db: AsyncSession, device: Device, function: str) -> list[SecurityAdvisory]:
    """high/critical-Meldungen für ``function`` (oder general), die die Geräteversion betreffen."""
    return [a for a in await enabled_advisories(db)
            if a.severity in BLOCKING and a.function in (function, "general") and version_affected(a, device.routeros_version)]


def block_message(device: Device, advs: list[SecurityAdvisory]) -> str:
    fixes = sorted({a.fixed_in for a in advs if a.fixed_in}, key=lambda v: parse_version(v) or (0,))
    ids = ", ".join(a.cve for a in advs)
    return (f"{device.name}: RouterOS {device.routeros_version} ist von {ids} betroffen – erst Firmware aktualisieren"
            + (f" (behoben ab {fixes[-1]})" if fixes else "") + ". Siehe Firmware-Seite: /firmware")
