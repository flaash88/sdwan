"""HTML-/Text-Mails (Alarme, Test-Mail, SLA-Bericht) aus Jinja2-Templates unter ``app/templates/mail``.

Layout Outlook-tauglich: Tabellen, nur Inline-CSS, max. 600 px, keine externen Fonts, kein JavaScript.
Der Klartext wird immer mitgeschickt (multipart/alternative).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import utcnow

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "mail"
DEFAULT_TZ = "Europe/Vienna"

SEVERITY = {  # Label, Emoji, Balkenfarbe
    "critical": ("KRITISCH", "🔴", "#dc2626"),
    "warning": ("WARNUNG", "🟠", "#ea580c"),
    "info": ("INFO", "🔵", "#2563eb"),
}
RESOLVED = ("BEHOBEN", "✅", "#16a34a")

NEXT_STEPS: dict[str, list[str]] = {
    "device_offline": [
        "Stromversorgung und Uplink des Routers vor Ort prüfen.",
        "Prüfen, ob der Standort insgesamt erreichbar ist (Provider-Störung?).",
        "Nach Rückkehr die Uptime ansehen: Ein Neustart deutet auf Strom- oder Hardwareproblem hin.",
    ],
    "wan_down": [
        "Modem/ONT und Kabel des betroffenen WAN-Anschlusses prüfen.",
        "Störungsmeldung des Providers prüfen bzw. Ticket eröffnen.",
        "Der Traffic läuft – sofern vorhanden – automatisch über die übrigen WAN-Links.",
    ],
    "latency": [
        "Auslastung des Anschlusses prüfen (Metriken: Interface-Durchsatz).",
        "Bei anhaltender Latenz Provider kontaktieren oder Leitung testen.",
    ],
    "mesh_down": [
        "Erreichbarkeit beider Standorte prüfen – meist ist eine Seite offline.",
        "Öffentliche IP/Endpoint der Gegenstelle prüfen (dynamische IP geändert?).",
    ],
    "cpu_high": [
        "Prozesse unter /tool profile auf dem Router prüfen.",
        "Firewall-Regeln und Verbindungsanzahl auf ungewöhnlichen Traffic prüfen.",
    ],
    "vrrp_master": [
        "Standort läuft über Backup-Router/5G. Glasfaser-Strecke und FortiGate prüfen.",
        "Kassen und kritische Dienste am Standort auf Funktion prüfen.",
        "Datenvolumen des Backup-WAN im Auge behalten.",
    ],
    "wan_backup_active": [
        "Standort läuft über das Backup-WAN. Primären Anschluss prüfen.",
        "Datenvolumen des Backup-WAN im Auge behalten.",
    ],
    "wan_volume": [
        "Verbrauch im WAN-Tab prüfen; ggf. Datenpaket beim Provider aufstocken.",
        "Prüfen, warum das Backup-WAN so lange Traffic trägt (Primärleitung gestört?).",
    ],
}


@dataclass
class Brand:
    name: str
    short: str
    accent: str
    logo_url: str
    footer: str
    emoji: bool


def brand() -> Brand:
    s = get_settings()
    return Brand(s.product_name, s.product_short, s.mail_accent_color, s.mail_logo_url, s.mail_footer_text, s.mail_subject_emoji)


@lru_cache
def _env(html: bool) -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]) if html else False,
                       trim_blocks=not html, lstrip_blocks=not html)


def _render(name: str, **ctx: Any) -> str:
    return _env(name.endswith(".html")).get_template(name).render(brand=brand(), **ctx)


# ----------------------------------------------------------------------------- Formatierung
def tzinfo(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def fmt_local(ts: dt.datetime | None, tz: str | None) -> str:
    """UTC -> lokale Zeit des Mandanten, z. B. ``25.09.2026, 11:39 Uhr``."""
    if ts is None:
        return "–"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts.astimezone(tzinfo(tz)).strftime("%d.%m.%Y, %H:%M Uhr")


def fmt_duration(seconds: float | None) -> str:
    s = max(int(seconds or 0), 0)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def _ms(v: float | None) -> str:
    return f"{v:.0f} ms" if v is not None else "–"


# ----------------------------------------------------------------------------- Alarm-Kontext
def _join(device: str, kurz: str) -> str:
    return f"{device} {kurz}" if kurz.startswith("ist ") else f"{device}: {kurz}"


def finish(a: dict[str, Any]) -> dict[str, Any]:
    """Ergänzt Überschrift, Farben, Dauer und Betreff aus den Basisfeldern."""
    b = brand()
    label, emoji, color = RESOLVED if a["resolved"] else SEVERITY.get(a["severity"], SEVERITY["warning"])
    a["color"] = color
    a["severity_label"] = SEVERITY.get(a["severity"], SEVERITY["warning"])[0].capitalize()
    loc = f"Standort {a['site']} – " if a.get("site") else ""
    a["headline"] = loc + _join(a["device"], a["kurz"]) if a.get("device") else a["kurz"]
    who = (f"{a['site']} – " if a.get("site") else "") + (f"{a['device']}: " if a.get("device") else "")
    kurz = a["kurz"][4:] if a["kurz"].startswith("ist ") else a["kurz"]
    subject = f"[{b.short}] {emoji + ' ' if b.emoji else ''}{label} | {who}{kurz}"
    if a["resolved"]:
        subject += f" (Dauer {a['duration']})"
        a["summary"] = (f"Der Alarm „{a['type_label']}“ war {a['duration']} aktiv und ist seit {a['end']} behoben. "
                        "Es ist keine weitere Aktion nötig.")
    a["subject"] = subject
    return a


def _wan_rows(links: list[Any]) -> list[list[str]]:
    return [[f"WAN{lk.slot} {lk.name}", lk.interface, lk.status, "ja" if lk.active else "nein", _ms(lk.last_latency_ms),
             f"{lk.last_loss_pct:.0f} %" if lk.last_loss_pct is not None else "–"] for lk in links]


async def alert_context(db: AsyncSession, rule: Any, alert: Any, tenant: Any, resolved: bool) -> dict[str, Any]:
    from app.models import Device, Site, VpnPeer, VrrpInstance, WanLink
    from app.services.alerts import TYPES

    tz = getattr(tenant, "timezone", None)
    dev = await db.get(Device, alert.device_id) if alert.device_id else None
    site = await db.get(Site, dev.site_id) if dev and dev.site_id else None
    facts = (dev.facts or {}) if dev else {}
    end = alert.resolved_at if resolved else None
    duration = ((end or utcnow()) - alert.started_at).total_seconds()
    prefix, _, ref = alert.subject.partition(":")
    ref_id = None
    try:
        ref_id = uuid.UUID(ref) if ref else None
    except ValueError:
        pass
    p = rule.params or {}
    kurz, block = alert.message, None
    t = rule.type
    links = list((await db.execute(select(WanLink).where(WanLink.device_id == dev.id).order_by(WanLink.slot))).scalars()) if dev else []
    link = next((lk for lk in links if lk.id == ref_id), None)
    if t == "device_offline":
        kurz = "ist offline"
        block = {"kind": "offline", "last_seen": fmt_local(dev.last_seen_at, tz) if dev else "–", "rtt": _ms(facts.get("mgmt_rtt_ms"))}
    elif t in ("wan_down", "wan_backup_active", "wan_volume") or (t == "latency" and prefix == "latency"):
        name = link.name if link else "?"
        val = alert.value
        kurz = {"wan_down": f"WAN {name} ausgefallen", "wan_backup_active": f"Backup-WAN {name} aktiv",
                "latency": f"Latenz WAN {name} {val or 0:.0f} ms", "wan_volume": f"WAN {name} bei {val or 0:.0f} % des Datenvolumens"}[t]
        block = {"kind": "wan", "rows": _wan_rows(links), "volume": None}
        if t == "wan_volume" and link and link.monthly_limit_gb:
            block["volume"] = f"Verbrauch {link.name} diesen Monat: {(link.vol_bytes or 0) / 1e9:.1f} von {link.monthly_limit_gb:g} GB."
    elif t == "latency":  # Latenz zur Cloud
        kurz = f"Latenz zur Cloud {alert.value or 0:.0f} ms"
        block = {"kind": "value", "label": "Latenz zur Cloud", "value": _ms(alert.value), "threshold": _ms(float(p.get("threshold", 150)))}
    elif t == "cpu_high":
        kurz = f"CPU-Last {alert.value or 0:.0f} %"
        block = {"kind": "value", "label": "CPU-Last", "value": f"{alert.value or 0:.0f} %", "threshold": f"{float(p.get('threshold', 90)):.0f} %"}
    elif t == "vrrp_master" and ref_id:
        inst = await db.get(VrrpInstance, ref_id)
        if inst:
            kurz = f"VRRP {inst.name} ist Master"
            wl = next((lk for lk in links if lk.slot == inst.linked_wan_slot), None)
            block = {"kind": "vrrp", "name": inst.name, "interface": inst.interface, "vip": inst.vip, "vrid": str(inst.vrid),
                     "since": fmt_local(inst.last_change_at if inst.state == "master" else alert.started_at, tz),
                     "wan": f"WAN{wl.slot} {wl.name} ({wl.interface})" if wl else ("–" if not inst.linked_wan_slot else f"WAN{inst.linked_wan_slot}")}
    elif t == "mesh_down" and ref_id:
        peer = await db.get(VpnPeer, ref_id)
        if peer and dev:
            other_id = peer.device_b_id if peer.device_a_id == dev.id else peer.device_a_id
            other = await db.get(Device, other_id)
            kurz = f"VPN-Tunnel zu {other.name if other else '?'} down"
            block = {"kind": "mesh", "peer": other.name if other else "?", "topology": "Full-Mesh" if peer.kind == "full_mesh" else "Hub-and-Spoke"}
    base = get_settings().public_url.rstrip("/")
    model = " · ".join(x for x in (dev.model if dev else None, f"RouterOS {dev.routeros_version}" if dev and dev.routeros_version else None) if x)
    return finish({
        "type": t, "type_label": TYPES.get(t, t), "severity": alert.severity, "resolved": resolved,
        "tenant": tenant.name if tenant else None, "site": site.name if site else None,
        "device": dev.name if dev else None, "device_line": f"{dev.name} ({model})" if dev and model else (dev.name if dev else None),
        "rule": rule.name, "message": alert.message, "kurz": kurz, "block": block, "steps": NEXT_STEPS.get(t, []),
        "start": fmt_local(alert.started_at, tz), "end": fmt_local(end, tz) if end else None, "duration": fmt_duration(duration),
        "device_url": f"{base}/devices/{dev.id}" if dev else None, "alerts_url": f"{base}/alerts",
    })


def render_alert(a: dict[str, Any]) -> tuple[str, str, str]:
    """-> (Betreff, Klartext, HTML)"""
    return a["subject"], _render("alert.txt", a=a), _render("alert.html", a=a, title=a["subject"])


# ----------------------------------------------------------------------------- Beispieldaten (Vorschau/Tests)
def sample_context(t: str, resolved: bool, tz: str = DEFAULT_TZ) -> dict[str, Any]:
    from app.services.alerts import TYPES

    start = dt.datetime(2026, 9, 25, 9, 15, tzinfo=dt.UTC)
    end = start + dt.timedelta(minutes=24)
    down = t in ("wan_down", "wan_backup_active", "wan_volume", "vrrp_master") and not resolved
    wan = {"kind": "wan", "volume": None, "rows": [["WAN1 Glasfaser-Core", "ether2", "down" if down else "up", "nein" if down or t == "wan_backup_active" else "ja", "–" if down else "12 ms", "100 %" if down else "0 %"],
                                                   ["WAN2 5G", "ether8", "up", "ja" if down or t == "wan_backup_active" else "nein", "38 ms", "0 %"]]}
    samples: dict[str, tuple[str, dict[str, Any] | None]] = {
        "device_offline": ("ist offline", {"kind": "offline", "last_seen": fmt_local(start - dt.timedelta(minutes=1), tz), "rtt": "23 ms"}),
        "wan_down": ("WAN Glasfaser-Core ausgefallen", wan),
        "wan_backup_active": ("Backup-WAN 5G aktiv", wan),
        "latency": ("Latenz WAN Glasfaser-Core 187 ms", wan),
        "wan_volume": ("WAN 5G bei 85 % des Datenvolumens", {**wan, "volume": "Verbrauch 5G diesen Monat: 42.5 von 50 GB."}),
        "mesh_down": ("VPN-Tunnel zu zentrale-01 down", {"kind": "mesh", "peer": "zentrale-01", "topology": "Hub-and-Spoke"}),
        "cpu_high": ("CPU-Last 97 %", {"kind": "value", "label": "CPU-Last", "value": "97 %", "threshold": "90 %"}),
        "vrrp_master": ("VRRP vrrp-kassen ist Master", {"kind": "vrrp", "name": "vrrp-kassen", "interface": "ether2", "vip": "192.168.110.1/32",
                                                         "vrid": "110", "since": fmt_local(start, tz), "wan": "WAN1 Glasfaser-Core (ether2)"}),
    }
    kurz, block = samples.get(t, ("Beispielmeldung", None))
    base = get_settings().public_url.rstrip("/")
    return finish({
        "type": t, "type_label": TYPES.get(t, t), "severity": "critical" if t == "device_offline" else "warning", "resolved": resolved,
        "tenant": "Beispiel GmbH", "site": "Gutshof", "device": "routerboard", "device_line": "routerboard (L009UiGS-RM · RouterOS 7.19.4)",
        "rule": f"Beispielregel {TYPES.get(t, t)}", "message": kurz, "kurz": kurz, "block": block, "steps": NEXT_STEPS.get(t, []),
        "start": fmt_local(start, tz), "end": fmt_local(end, tz) if resolved else None, "duration": fmt_duration((end - start).total_seconds()),
        "device_url": f"{base}/devices/00000000-0000-0000-0000-000000000000", "alerts_url": f"{base}/alerts",
    })


# ----------------------------------------------------------------------------- Test-Mail & SLA
def render_test(rule: str, tenant: Any) -> tuple[str, str, str]:
    b = brand()
    sent = fmt_local(utcnow(), getattr(tenant, "timezone", None))
    subject = f"[{b.short}] {'🧪 ' if b.emoji else ''}TEST | {rule}"
    text = (f"Test-Benachrichtigung der Alarmregel '{rule}' für {tenant.name}.\n"
            f"Wenn Sie diese Mail lesen können, kommen Alarme dieser Regel bei Ihnen an. Gesendet am {sent}.\n\n--\n{b.name}"
            + (f"\n{b.footer}" if b.footer else ""))
    return subject, text, _render("test.html", rule=rule, tenant=tenant.name, sent_at=sent, title=subject)


def render_sla(rep: dict[str, Any], tenant: Any, start: dt.datetime, end: dt.datetime) -> tuple[str, str, str]:
    b = brand()
    tz = getattr(tenant, "timezone", None)
    pct = lambda v: f"{v:.3f} %" if v is not None else "keine Daten"  # noqa: E731
    fleet = pct(rep["fleet_availability_pct"])
    rows = [[d["device"], d["site"], pct(d["availability_pct"]), fmt_duration(d["downtime_s"]),
             fmt_duration(d.get("backup_wan_s")) if d.get("has_backup_wan") else "–",
             fmt_duration(d.get("vrrp_master_s")) if d.get("has_vrrp") else "–"] for d in rep["devices"]]
    last = end - dt.timedelta(seconds=1)
    period = f"{start:%m/%Y}"
    period_long = f"{fmt_local(start, tz)[:10]} – {fmt_local(last, tz)[:10]}"
    sev = rep["alerts_by_severity"]
    alerts = f"{rep['alert_count']} (kritisch {sev['critical']}, Warnung {sev['warning']})"
    subject = f"[{b.short}] SLA-Bericht {tenant.name} {period}"
    lines = [f"Verfügbarkeitsbericht {tenant.name}", f"Zeitraum: {period_long}", f"Gesamtverfügbarkeit: {fleet}", f"Alarme: {alerts}", "",
             "Verfügbarkeit je Gerät:"]
    lines += [f"- {r[0]} ({r[1]}): {r[2]}, Ausfallzeit {r[3]}" for r in rows]
    from app.services.sla import _backup_lines

    text = "\n".join(lines) + "\n" + _backup_lines(rep) + "\nDer vollständige Bericht liegt als PDF bei.\n\n--\n" + b.name + (f"\n{b.footer}" if b.footer else "")
    html = _render("sla.html", tenant=tenant.name, period=period, period_long=period_long, fleet=fleet, alerts=alerts, devices=rows, title=subject)
    return subject, text, html
