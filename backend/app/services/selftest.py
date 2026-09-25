"""Hardware-Selbsttest: prüft, ob ein Router die Pfade und Felder liefert, die die Plattform liest.

Ausschließlich LESEND – es werden nur ``print``-Befehle, ein einzelner Ping und der Konfigurations-Export
(wie beim Backup, per SSH) ausgeführt; kein ``set``/``add``/``remove``, kein ``check-for-updates``.
Die erwarteten Felder stehen zentral in ``app/routeros/schema.py``.

Ampel je Prüfung: ``ok`` (grün) · ``warn`` (orange) · ``error`` (rot); Gesamtstatus = schlechteste Prüfung.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import time
from typing import Any

from app.config import get_settings
from app.db import utcnow
from app.models import Device
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.schema import KNOWN_ARCHITECTURES, OPTIONAL_POLICIES, PATH_SPECS, REQUIRED_POLICIES, PathSpec

RANK = {"ok": 0, "warn": 1, "error": 2}
CONNECTION_SAMPLE_LIMIT = 5000  # Verbindungstabelle nur bis zu dieser Größe für die Feldprüfung lesen
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def worst(*states: str) -> str:
    return max(states, key=lambda s: RANK.get(s, 0)) if states else "ok"


def _flag(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def check_fields(spec: PathSpec, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Vergleicht die Antwort mit der Feldliste. Pflichtfelder müssen in jeder Zeile stehen
    (bei Befehlen ohne Tabelle wie /ping genügt eine Zeile)."""
    data = [r for r in rows if set(r) - {".id", "ret"}] if spec.key != "fw_connection" else rows
    res: dict[str, Any] = {"rows": len(data), "missing": [], "missing_optional": [], "notes": [], "status": "ok"}
    if not data:
        if spec.must_have_rows:
            res["status"] = "warn"
            res["notes"].append("Keine Einträge – auf einem verbundenen Router erwartet")
        else:
            res["notes"].append("Keine Einträge – Feldprüfung übersprungen")
        return res
    per_row = spec.key != "ping"
    for f in spec.fields:
        absent = sum(1 for r in data if f not in r)
        if (per_row and absent) or (not per_row and absent == len(data)):
            res["missing"].append(f if absent == len(data) else f"{f} (fehlt in {absent} von {len(data)} Zeilen)")
    for f in spec.optional:
        if all(f not in r for r in data):
            res["missing_optional"].append(f)
            if f in spec.warn_if_missing:
                res["status"] = worst(res["status"], "warn")
                res["notes"].append(spec.warn_if_missing[f])
            elif f in spec.hints:
                res["notes"].append(spec.hints[f])
    if res["missing"]:
        res["status"] = "error"
    return res


async def _probe(api: DeviceAPI, spec: PathSpec) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entry: dict[str, Any] = {"key": spec.key, "label": spec.label, "command": spec.command, "used_by": spec.used_by,
                             "kind": "path", "expected": list(spec.fields), "optional": list(spec.optional)}
    params = dict(spec.params)
    if spec.key == "ping":
        params["address"] = get_settings().wg_hub_ip
    t0 = time.perf_counter()
    try:
        if spec.key == "fw_connection":
            cnt = await api.call(spec.command, **{"count-only": ""})
            total = next((int(r["ret"]) for r in cnt if str(r.get("ret", "")).isdigit()), None)
            entry["count"] = total
            if total is not None and total > CONNECTION_SAMPLE_LIMIT:
                entry.update({"status": "ok", "ms": round((time.perf_counter() - t0) * 1000, 1), "rows": 0, "missing": [], "missing_optional": [],
                              "notes": [f"{total} Verbindungen – Feldprüfung übersprungen (zu groß)"]})
                return entry, []
            rows = await api.call(spec.command, **{".proplist": ",".join(spec.optional)})
        else:
            rows = await api.call(spec.command, **params)
    except RouterOSError as exc:
        entry.update({"status": "error", "reachable": False, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": str(exc),
                      "rows": 0, "missing": [], "missing_optional": [], "notes": []})
        return entry, []
    entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    entry["reachable"] = True
    entry.update(check_fields(spec, rows))
    entry["sample_fields"] = sorted({k for r in rows for k in r if k != ".id"})[:60]
    return entry, rows


def _check(key: str, label: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": "check", "status": status, "notes": [detail], **extra}


def _parse_clock(row: dict[str, Any]) -> dt.datetime | None:
    """RouterOS liefert ``date`` als ``sep/25/2026`` (bis 7.9) oder ``2026-09-25`` (ab 7.10), dazu ``gmt-offset``."""
    date, clock = str(row.get("date", "")), str(row.get("time", ""))
    m = re.match(r"^([a-z]{3})/(\d{1,2})/(\d{4})$", date.lower())
    try:
        if m:
            y, mo, d = int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))
        else:
            y, mo, d = (int(x) for x in date.split("-"))
        hh, mm, ss = (int(x) for x in clock.split(":")[:3])
        local = dt.datetime(y, mo, d, hh, mm, ss)
    except (ValueError, KeyError):
        return None
    off = str(row.get("gmt-offset", "+00:00"))
    om = re.match(r"^([+-])(\d{1,2}):(\d{2})$", off)
    delta = dt.timedelta(hours=int(om.group(2)), minutes=int(om.group(3))) * (1 if om.group(1) == "+" else -1) if om else dt.timedelta()
    return (local - delta).replace(tzinfo=dt.UTC)


def _address_allows(addr: str, ip: str) -> bool:
    if not addr.strip():
        return True
    target = ipaddress.ip_address(ip)
    for part in addr.split(","):
        try:
            if target in ipaddress.ip_network(part.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def extra_checks(raw: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    s = get_settings()
    out: list[dict[str, Any]] = []
    res = (raw.get("resource") or [{}])[0]
    version = str(res.get("version", ""))
    m = re.match(r"^(\d+)\.(\d+)", version)
    if not m:
        out.append(_check("version", "RouterOS-Version", "error", f"Version nicht lesbar ({version or 'leer'})"))
    elif int(m.group(1)) < 7:
        out.append(_check("version", "RouterOS-Version", "error", f"RouterOS {version} – benötigt wird 7.x", value=version))
    else:
        out.append(_check("version", "RouterOS-Version", "ok", f"RouterOS {version}", value=version))
    arch = str(res.get("architecture-name", ""))
    out.append(_check("architecture", "Architektur", "ok" if arch in KNOWN_ARCHITECTURES else "warn",
                      arch if arch in KNOWN_ARCHITECTURES else f"Unbekannte Architektur '{arch or 'leer'}'", value=arch))

    # Rechte des API-Benutzers
    user = next((u for u in raw.get("user") or [] if u.get("name") == s.routeros_api_user), None)
    if user is None:
        out.append(_check("rights", "Rechte API-Benutzer", "error", f"Benutzer '{s.routeros_api_user}' nicht gefunden"))
    else:
        group = next((g for g in raw.get("user_group") or [] if g.get("name") == user.get("group")), None)
        pol = {p for p in str((group or {}).get("policy", "")).split(",") if p and not p.startswith("!")}
        missing = [p for p in REQUIRED_POLICIES if p not in pol]
        opt = [p for p in OPTIONAL_POLICIES if p not in pol]
        if group is None:
            out.append(_check("rights", "Rechte API-Benutzer", "error", f"Gruppe '{user.get('group')}' nicht lesbar"))
        elif missing:
            out.append(_check("rights", "Rechte API-Benutzer", "error", f"Gruppe '{group['name']}' ohne: {', '.join(missing)}", missing=missing))
        elif opt:
            out.append(_check("rights", "Rechte API-Benutzer", "warn",
                              f"Gruppe '{group['name']}' ohne 'sensitive' – im Export fehlen Schlüssel/Passwörter, das Backup ist für eine "
                              "Wiederherstellung unvollständig", missing=opt))
        else:
            out.append(_check("rights", "Rechte API-Benutzer", "ok", f"Gruppe '{group['name']}' hat alle benötigten Rechte"))

    # Dienste: API für die Plattform, SSH für den Backup-Export – beide aus dem Management-Tunnel erreichbar
    services = {str(r.get("name")): r for r in raw.get("ip_service") or []}
    for name, port, why in (("api", s.routeros_api_port, "Plattform-Zugriff"), ("ssh", s.ssh_port, "Backup-Export")):
        svc = services.get(name)
        label = f"Dienst {name}"
        if svc is None:
            out.append(_check(f"service_{name}", label, "error", f"Dienst '{name}' nicht gefunden ({why})"))
        elif _flag(svc.get("disabled")):
            out.append(_check(f"service_{name}", label, "error", f"Dienst '{name}' deaktiviert – {why} schlägt fehl"))
        elif str(svc.get("port", "")) != str(port):
            out.append(_check(f"service_{name}", label, "error", f"Port {svc.get('port')} statt {port} – {why} schlägt fehl"))
        elif not _address_allows(str(svc.get("address", "") or ""), s.wg_hub_ip):
            out.append(_check(f"service_{name}", label, "error",
                              f"'address' = {svc.get('address')} erlaubt den Hub {s.wg_hub_ip} nicht – {why} schlägt fehl"))
        else:
            allowed = svc.get("address") or "alle Adressen"
            out.append(_check(f"service_{name}", label, "ok", f"aktiv auf Port {port}, erlaubt: {allowed}"))

    # Uhrzeit
    clock = (raw.get("clock") or [{}])[0]
    router_now = _parse_clock(clock)
    if router_now is None:
        out.append(_check("clock_skew", "Uhrzeitabweichung", "warn", f"Uhrzeit nicht auswertbar (date={clock.get('date')}, time={clock.get('time')})"))
    else:
        diff = (router_now - utcnow()).total_seconds()
        st = "error" if abs(diff) > 300 else "warn" if abs(diff) > 60 else "ok"
        out.append(_check("clock_skew", "Uhrzeitabweichung", st, f"Abweichung Router ↔ Server: {diff:+.0f} s"
                          + (" – NTP prüfen (Zeitstempel, Tokens, Zertifikate)" if st != "ok" else ""), value=round(diff)))
    return out


async def run_selftest(device: Device) -> dict[str, Any]:
    """Führt den Selbsttest aus und liefert das Ergebnis (wird vom Aufrufer gespeichert)."""
    from app.services.backup import BackupError, export_config

    t0 = time.perf_counter()
    checks: list[dict[str, Any]] = []
    raw: dict[str, list[dict[str, Any]]] = {}
    try:
        async with connect_device(device) as api:
            for spec in PATH_SPECS:
                entry, rows = await _probe(api, spec)
                if spec.key == "resource" and not entry.get("reachable"):
                    raise RouterOSError(entry.get("error") or "keine Antwort")
                checks.append(entry)
                raw[spec.key] = rows
    except RouterOSError as exc:
        return {"status": "error", "duration_ms": round((time.perf_counter() - t0) * 1000), "checks": [
            _check("connect", "Verbindung", "error", f"Router über den Management-Tunnel nicht erreichbar: {exc}")]}
    checks += extra_checks(raw)

    # Export wie beim Backup (Produktion: SSH `/export terse`)
    e0 = time.perf_counter()
    try:
        text = await export_config(device)
        ms = round((time.perf_counter() - e0) * 1000, 1)
        if not text.strip():
            checks.append(_check("export", "Konfigurations-Export", "error", "Export ist leer", ms=ms))
        elif "/interface" not in text:
            checks.append(_check("export", "Konfigurations-Export", "warn", "Export enthält keinen /interface-Abschnitt", ms=ms, size=len(text)))
        else:
            checks.append(_check("export", "Konfigurations-Export", "ok", f"{len(text)} Zeichen in {ms:.0f} ms", ms=ms, size=len(text)))
    except (BackupError, RouterOSError) as exc:
        checks.append(_check("export", "Konfigurations-Export", "error", f"Export fehlgeschlagen: {exc}", ms=round((time.perf_counter() - e0) * 1000, 1)))

    return {"status": worst(*(c["status"] for c in checks)), "duration_ms": round((time.perf_counter() - t0) * 1000), "checks": checks,
            "summary": {k: sum(1 for c in checks if c["status"] == k) for k in RANK}}
