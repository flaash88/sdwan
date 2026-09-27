"""Compliance und Config-Suche (Phase 16).

Regeltypen:

* Text gegen das letzte Backup (``/export terse``): ``contains``, ``not_contains``, ``regex``
  (``expect: match|no_match``).
* Strukturiert, live vom Router gelesen (zuverlässiger als das Parsen des Exports, der Standardwerte weglässt):
  ``service_disabled``, ``no_user``, ``ntp_enabled``, ``service_restricted_to_tunnel``, ``channel_in``,
  ``min_version``. Ist der Router nicht erreichbar, ist das Ergebnis ``unknown``, nicht ``fail``.

Auswertung nach jedem Backup (best effort) und manuell. Die Config-Suche maskiert Geheimnisse, bevor gesucht wird –
Passwörter/Schlüssel sind nie Treffer.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import utcnow
from app.models import ComplianceAssignment, ComplianceResult, ComplianceRuleSet, ConfigBackup, Device
from app.routeros import RouterOSError, connect_device
from app.services.targets import resolve_targets

log = logging.getLogger(__name__)

TEXT_TYPES = {"contains", "not_contains", "regex"}
LIVE_TYPES = {"service_disabled", "no_user", "ntp_enabled", "service_restricted_to_tunnel", "channel_in", "min_version"}
# Plattform-Daten statt Router-Abfrage (Phase 23 ff.)
PLATFORM_TYPES = {"no_security_advisory", "local_admin_present"}
TYPES = TEXT_TYPES | LIVE_TYPES | PLATFORM_TYPES
REGEX_MAX = 200
REGEX_TIMEOUT_S = 3.0
RESULT_RETENTION_DAYS = 180
# Verschachtelte Quantoren (z. B. (a+)+) sind die typische Ursache für katastrophales Backtracking
_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
# Geheimnisse im Export: key=wert (auch in Anführungszeichen) -> key=***
SECRET_KEYS = ("password", "passphrase", "secret", "private-key", "preshared-key", "pre-shared-key", "authentication-key",
               "wpa-pre-shared-key", "wpa2-pre-shared-key", "psk", "auth-key", "key", "api-key", "token")
_SECRET = re.compile(r'(?<![\w-])(' + "|".join(re.escape(k) for k in SECRET_KEYS) + r')=("(?:[^"\\]|\\.)*"|\S+)', re.IGNORECASE)


class ComplianceError(ValueError):
    pass


def mask_secrets(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}=***", text)


def check_regex(pattern: str) -> re.Pattern[str]:
    if len(pattern) > REGEX_MAX:
        raise ComplianceError(f"Regulärer Ausdruck zu lang (max. {REGEX_MAX} Zeichen)")
    if _NESTED_QUANT.search(pattern):
        raise ComplianceError("Verschachtelte Wiederholungen wie (a+)+ sind nicht erlaubt")
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ComplianceError(f"Ungültiger regulärer Ausdruck: {exc}") from exc


def validate_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, ids = [], set()
    for i, r in enumerate(rules):
        t = r.get("type")
        if t not in TYPES:
            raise ComplianceError(f"Regel {i + 1}: Typ {t!r} unbekannt")
        rid = str(r.get("id") or f"r{i + 1}")
        if not re.match(r"^[a-z0-9\-_]{1,40}\Z", rid) or rid in ids:
            raise ComplianceError(f"Regel {i + 1}: ID {rid!r} ungültig oder doppelt")
        ids.add(rid)
        p = dict(r.get("params") or {})
        if t in ("contains", "not_contains") and not str(p.get("text", "")).strip():
            raise ComplianceError(f"Regel {i + 1}: Text fehlt")
        if t == "regex":
            check_regex(str(p.get("pattern", "")))
            p["expect"] = p.get("expect", "match") if p.get("expect", "match") in ("match", "no_match") else "match"
        if t == "service_disabled" and not re.match(r"^[a-z\-]{2,20}\Z", str(p.get("service", ""))):
            raise ComplianceError(f"Regel {i + 1}: Dienst fehlt")
        if t == "no_user" and not str(p.get("name", "")).strip():
            raise ComplianceError(f"Regel {i + 1}: Benutzername fehlt")
        if t == "service_restricted_to_tunnel" and not p.get("services"):
            p["services"] = ["api", "ssh"]
        if t == "channel_in" and not p.get("channels"):
            raise ComplianceError(f"Regel {i + 1}: Kanäle fehlen")
        if t == "min_version" and not re.match(r"^\d+(\.\d+){0,2}\Z", str(p.get("version", ""))):
            raise ComplianceError(f"Regel {i + 1}: Version im Format 7.15 bzw. 7.15.3")
        out.append({"id": rid, "name": str(r.get("name") or t)[:120], "type": t, "params": p})
    return out


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v).split(" ")[0])[:3])


def _flag(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


async def read_live(device: Device) -> dict[str, Any] | None:
    """Live-Werte für strukturierte Prüfungen (nur lesend). None = Router nicht erreichbar."""
    try:
        async with connect_device(device) as api:
            live: dict[str, Any] = {"services": await api.print("/ip/service"), "users": await api.print("/user"),
                                    "resource": await api.resource()}
            try:
                live["ntp"] = (await api.call("/system/ntp/client/print") or [{}])[0]  # ANNAHME (Labor): RouterOS 7 Feld 'enabled'
            except RouterOSError:
                live["ntp"] = None
            try:
                live["update"] = (await api.call("/system/package/update/print") or [{}])[0]
            except RouterOSError:
                live["update"] = None
            return live
    except RouterOSError:
        return None


def _in_tunnel(address: str, local: list[str] | None = None) -> bool:
    """Nur Tunnel-Netz – plus die Netze eines aktiven Vor-Ort-Zugangs (Phase 24, bewusst lokal erlaubt)."""
    net = get_settings().wg_net
    parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
    if not parts:
        return False  # leer = von überall erlaubt
    allowed = [net] + [ipaddress.ip_network(x, strict=False) for x in local or []]
    try:
        return all(any(ipaddress.ip_network(p, strict=False).subnet_of(a) for a in allowed) for p in parts)
    except (ValueError, TypeError):
        return False


def evaluate_rule(rule: dict[str, Any], text: str | None, live: dict[str, Any] | None,
                  platform: dict[str, Any] | None = None) -> tuple[str, str]:
    t, p = rule["type"], rule["params"]
    if t == "no_security_advisory":
        affected = [a for a in (platform or {}).get("advisories", []) if a["status"] == "affected"]
        if affected:
            return "fail", ", ".join(f"{a['cve']} ({a['severity']})" for a in affected)[:300]
        possible = [a for a in (platform or {}).get("advisories", []) if a["status"] == "possible"]
        return "ok", (f"keine bekannten; möglicherweise: {', '.join(a['cve'] for a in possible)}" if possible else "keine bekannten Meldungen")
    if t == "local_admin_present":
        la = (platform or {}).get("local_access")
        if not la:
            return "fail", "Vor-Ort-Zugang nicht angelegt"
        if la["status"] != "active":
            label = {"not_created": "nicht angelegt", "pending": "ausstehend", "error": "Fehler", "disabled": "deaktiviert"}.get(la["status"], la["status"])
            return "fail", f"{label}: {la.get('reason') or ''}".strip(" :")[:300]
        users = [u for u in (live or {}).get("users", []) if u.get("name") == la["username"]]
        if live is not None and not users:
            return "fail", f"Benutzer {la['username']} fehlt auf dem Router"
        warns = []
        if la.get("missing_policies"):
            # Warnung, kein Fehler: Zugang vorhanden, aber ohne volle lokale Rechte (nachträglich über die API angelegt)
            warns.append(f"eingeschränkt – es fehlt {', '.join(la['missing_policies'])}; vollständig per Onboarding oder Terminal-Befehl")
        if la.get("wan_exceptions"):
            warns.append("Vor-Ort-Zugang aus WAN-Netz erlaubt: " + ", ".join(f"{e['network']} auf {e['interface']}" for e in la["wan_exceptions"]))
        if warns:
            return "warn", " · ".join(warns)[:300]
        return "ok", f"{la['username']} · {', '.join(la['networks'])}"[:300]
    if t in TEXT_TYPES:
        if text is None:
            return "unknown", "Kein Backup vorhanden"
        if t == "contains":
            return ("ok", "gefunden") if p["text"] in text else ("fail", f"„{p['text']}“ nicht im Export")
        if t == "not_contains":
            return ("fail", f"„{p['text']}“ im Export gefunden") if p["text"] in text else ("ok", "nicht enthalten")
        m = check_regex(p["pattern"]).search(text)
        if p.get("expect", "match") == "match":
            return ("ok", f"Treffer: {m.group(0)[:80]}") if m else ("fail", "kein Treffer")
        return ("fail", f"unerwünschter Treffer: {mask_secrets(m.group(0))[:80]}") if m else ("ok", "kein Treffer")
    if live is None:
        return "unknown", "Router nicht erreichbar"
    if t == "service_disabled":
        svc = next((s for s in live["services"] if s.get("name") == p["service"]), None)
        if svc is None:
            return "ok", "Dienst nicht vorhanden"
        return ("ok", "deaktiviert") if _flag(svc.get("disabled")) else ("fail", "aktiv")
    if t == "no_user":
        return ("fail", "Benutzer vorhanden") if any(u.get("name") == p["name"] for u in live["users"]) else ("ok", "nicht vorhanden")
    if t == "ntp_enabled":
        if live.get("ntp") is None:
            return "unknown", "NTP-Client nicht lesbar"
        return ("ok", "aktiv") if _flag(live["ntp"].get("enabled")) else ("fail", "nicht aktiv")
    if t == "service_restricted_to_tunnel":
        bad = []
        for name in p["services"]:
            svc = next((s for s in live["services"] if s.get("name") == name), None)
            if svc is None or _flag(svc.get("disabled")):
                continue
            if not _in_tunnel(str(svc.get("address") or ""), (platform or {}).get("local_networks")):
                bad.append(f"{name} ({svc.get('address') or 'von überall'})")
        return ("fail", "erreichbar außerhalb des Tunnels: " + ", ".join(bad)) if bad else ("ok", "nur Tunnel-Netz")
    if t == "channel_in":
        ch = (live.get("update") or {}).get("channel")
        if not ch:
            return "unknown", "Kanal nicht lesbar"
        return ("ok", ch) if ch in p["channels"] else ("fail", f"Kanal {ch}")
    if t == "min_version":
        v = str(live["resource"].get("version", ""))
        if not v:
            return "unknown", "Version nicht lesbar"
        return ("ok", v) if _ver(v) >= _ver(p["version"]) else ("fail", f"{v} < {p['version']}")
    return "unknown", "unbekannter Typ"


async def latest_backup(db: AsyncSession, device_id: Any) -> ConfigBackup | None:
    return (await db.execute(select(ConfigBackup).where(ConfigBackup.device_id == device_id)
                             .order_by(ConfigBackup.created_at.desc()).limit(1))).scalar_one_or_none()


async def rule_sets_for(db: AsyncSession, device: Device) -> list[ComplianceRuleSet]:
    out: dict[Any, ComplianceRuleSet] = {}
    for a in (await db.execute(select(ComplianceAssignment).where(ComplianceAssignment.tenant_id == device.tenant_id))).scalars():
        if device.id in {d.id for d in await resolve_targets(db, a.targets or {}, device.tenant_id)}:
            rs = await db.get(ComplianceRuleSet, a.rule_set_id)
            if rs is not None:
                out[rs.id] = rs
    return list(out.values())


async def evaluate_device(db: AsyncSession, device: Device, rule_sets: list[ComplianceRuleSet] | None = None) -> list[ComplianceResult]:
    sets = rule_sets if rule_sets is not None else await rule_sets_for(db, device)
    if not sets:
        return []
    backup = await latest_backup(db, device.id)
    text = backup.content if backup else None
    live = await read_live(device) if any(r["type"] in LIVE_TYPES for rs in sets for r in rs.rules or []) else None
    platform: dict[str, Any] = {}
    if any(r["type"] == "no_security_advisory" for rs in sets for r in rs.rules or []):
        from app.services.advisories import device_advisories

        platform["advisories"] = await device_advisories(db, device)
    from app.models import LocalAccess
    from app.services.local_access import out as la_out

    la = (await db.execute(select(LocalAccess).where(LocalAccess.device_id == device.id))).scalar_one_or_none()
    platform["local_access"] = la_out(la)
    if la is not None and la.status == "active":
        platform["local_networks"] = la.networks or []
    now = utcnow()
    results = []
    for rs in sets:
        rows = []
        for r in rs.rules or []:
            try:
                st, detail = evaluate_rule(r, text, live, platform)
            except ComplianceError as exc:
                st, detail = "unknown", str(exc)
            rows.append({"rule_id": r["id"], "name": r["name"], "status": st, "detail": detail})
        # Warnung („warn“) = bestanden mit Hinweis, zählt nicht als Verstoß
        res = ComplianceResult(tenant_id=device.tenant_id, device_id=device.id, rule_set_id=rs.id, evaluated_at=now,
                               backup_id=backup.id if backup else None, results=rows,
                               passed=sum(1 for x in rows if x["status"] in ("ok", "warn")), failed=sum(1 for x in rows if x["status"] == "fail"),
                               unknown=sum(1 for x in rows if x["status"] == "unknown"))
        db.add(res)
        results.append(res)
    await db.execute(delete(ComplianceResult).where(ComplianceResult.device_id == device.id,
                                                    ComplianceResult.evaluated_at < now - dt.timedelta(days=RESULT_RETENTION_DAYS)))
    await db.flush()
    return results


async def after_backup(db: AsyncSession, device: Device) -> None:
    """Hook am Ende von ``take_backup`` – best effort, ein Fehler darf das Backup nie scheitern lassen."""
    try:
        if (await db.execute(select(ComplianceAssignment.id).where(ComplianceAssignment.tenant_id == device.tenant_id).limit(1))).first():
            await evaluate_device(db, device)
    except Exception:  # noqa: BLE001
        log.exception("Compliance-Auswertung nach Backup für %s fehlgeschlagen", device.name)


async def latest_results(db: AsyncSession, device_ids: list[Any] | None = None) -> dict[tuple[Any, Any], ComplianceResult]:
    """Jeweils letztes Ergebnis je (Gerät, Regelset)."""
    q = select(ComplianceResult).order_by(ComplianceResult.evaluated_at.desc())
    if device_ids is not None:
        q = q.where(ComplianceResult.device_id.in_(device_ids))
    out: dict[tuple[Any, Any], ComplianceResult] = {}
    for r in (await db.execute(q)).scalars():
        out.setdefault((r.device_id, r.rule_set_id), r)
    return out


# ----------------------------------------------------------------------------- Config-Suche
async def search_backups(db: AsyncSession, query: str, regex: bool, context: int = 2, limit: int = 200,
                         tenant_id: Any | None = None) -> dict[str, Any]:
    """Volltext/Regex über das jeweils letzte Backup je Gerät. Geheimnisse sind vor der Suche maskiert."""
    if not query.strip():
        raise ComplianceError("Suchbegriff fehlt")
    pat = check_regex(query) if regex else None
    q = select(ConfigBackup).order_by(ConfigBackup.created_at.desc())
    if tenant_id is not None:
        q = q.where(ConfigBackup.tenant_id == tenant_id)
    latest: dict[Any, ConfigBackup] = {}
    for b in (await db.execute(q)).scalars():
        latest.setdefault(b.device_id, b)
    devices = {d.id: d for d in (await db.execute(select(Device).where(Device.id.in_(list(latest))))).scalars()} if latest else {}

    def run() -> list[dict[str, Any]]:
        hits, total = [], 0
        for dev_id, b in latest.items():
            lines = mask_secrets(b.content).splitlines()
            matches = []
            for i, line in enumerate(lines):
                ok = bool(pat.search(line)) if pat else query.lower() in line.lower()
                if ok:
                    total += 1
                    if total > limit:
                        break
                    lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                    matches.append({"line": i + 1, "context": [{"n": j + 1, "text": lines[j], "hit": j == i} for j in range(lo, hi)]})
            if matches:
                d = devices.get(dev_id)
                hits.append({"device_id": str(dev_id), "device": d.name if d else "?", "tenant_id": str(b.tenant_id),
                             "backup_id": str(b.id), "backup_at": b.created_at, "matches": matches})
            if total > limit:
                break
        return hits

    try:
        hits = await asyncio.wait_for(asyncio.to_thread(run), timeout=REGEX_TIMEOUT_S)
    except TimeoutError as exc:
        raise ComplianceError("Suche abgebrochen (Zeitlimit) – Ausdruck vereinfachen") from exc
    return {"hits": hits, "devices_searched": len(latest), "truncated": sum(len(h["matches"]) for h in hits) >= limit}
