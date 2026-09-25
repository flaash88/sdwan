"""Regelbasierte Alerts (Phase 10).

Zustandsmaschine je (Regel, Subjekt): Bedingung erfüllt -> ``pending`` (seit ``started_at``);
liegt sie ``duration_s`` an -> ``firing`` + E-Mail; Bedingung weg -> ``resolved`` (+ optional
Entwarnungs-Mail), ein ``pending``-Alert, der nie gefeuert hat, wird verworfen.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Alert, AlertRule, Device, DeviceStatus, PairingStatus, Tenant, VpnPeer, VrrpInstance, WanLink
from app.services.mailer import send_mail

log = logging.getLogger(__name__)
TYPES = {
    "device_offline": "Gerät offline",
    "wan_down": "WAN-Link ausgefallen",
    "latency": "Latenz über Schwelle",
    "mesh_down": "VPN-Tunnel down",
    "cpu_high": "CPU-Last hoch",
    "vrrp_master": "VRRP: Router ist Master (Hauptsystem ausgefallen)",
    "wan_backup_active": "Backup-WAN aktiv",
    "wan_volume": "WAN-Datenvolumen (80 % / 100 % des Monatslimits)",
}
DEFAULT_RULES = [
    {"name": "Gerät offline", "type": "device_offline", "severity": "critical", "duration_s": 300},
    {"name": "WAN-Link ausgefallen", "type": "wan_down", "severity": "warning", "duration_s": 120},
    {"name": "WAN-Latenz > 150 ms", "type": "latency", "severity": "warning", "duration_s": 300, "params": {"threshold": 150, "metric": "wan"}},
    {"name": "VPN-Tunnel down", "type": "mesh_down", "severity": "warning", "duration_s": 300},
    {"name": "VRRP: Standort auf Backup (Master)", "type": "vrrp_master", "severity": "warning", "duration_s": 30},
]


@dataclass
class Condition:
    device: Device
    subject: str
    message: str
    value: float | None = None
    since: dt.datetime | None = None  # seit wann die Bedingung bekannt anliegt


REBOOT_SUPPRESS = dt.timedelta(minutes=5)


def reboot_suppressed(dev: Device, now: dt.datetime | None = None) -> bool:
    """Offline-Alarm nach einem über die Plattform ausgelösten Neustart unterdrücken.

    ``POST /devices/{id}/reboot`` setzt ``facts.reboot.until`` = Zeitpunkt + 5 Minuten. Bis dahin gilt
    „offline“ als erwartet (Router bootet) und die Bedingung ``device_offline`` wird für dieses Gerät
    übersprungen. Andere Alarmtypen bleiben aktiv. Ist das Gerät nach Ablauf nicht zurück, alarmiert die
    Regel ganz normal – ``since`` ist dann der letzte Kontakt vor dem Neustart, die Verzögerung der Regel
    läuft also bereits.
    """
    until = ((dev.facts or {}).get("reboot") or {}).get("until")
    if not until:
        return False
    try:
        return (now or utcnow()) < dt.datetime.fromisoformat(until)
    except ValueError:
        return False


def _in_scope(rule: AlertRule, dev: Device) -> bool:
    if rule.device_ids and str(dev.id) not in rule.device_ids:
        return False
    if rule.site_ids and (not dev.site_id or str(dev.site_id) not in rule.site_ids):
        return False
    return True


async def conditions(db: AsyncSession, rule: AlertRule, devices: list[Device]) -> list[Condition]:
    devs = [d for d in devices if _in_scope(rule, d)]
    by_id = {d.id: d for d in devs}
    out: list[Condition] = []
    p = rule.params or {}
    if rule.type == "device_offline":
        for d in devs:
            if d.status == DeviceStatus.offline and not reboot_suppressed(d):
                out.append(Condition(d, "device", f"{d.name} ist offline (zuletzt gesehen {d.last_seen_at:%d.%m. %H:%M} UTC)" if d.last_seen_at else f"{d.name} ist offline", since=d.last_seen_at))
    elif rule.type in ("wan_down", "latency"):
        links = (await db.execute(select(WanLink).where(WanLink.device_id.in_(list(by_id)), WanLink.enabled.is_(True)))).scalars().all()
        if rule.type == "wan_down":
            for lk in links:
                if lk.status == "down":
                    d = by_id[lk.device_id]
                    out.append(Condition(d, f"wan:{lk.id}", f"{d.name}: WAN {lk.name} ({lk.interface}) ist ausgefallen", since=lk.last_change_at))
        else:
            thr = float(p.get("threshold", 150))
            if p.get("metric", "wan") == "mgmt":
                for d in devs:
                    rtt = (d.facts or {}).get("mgmt_rtt_ms")
                    if d.status == DeviceStatus.online and rtt is not None and float(rtt) > thr:
                        out.append(Condition(d, "mgmt-latency", f"{d.name}: Latenz zur Cloud {float(rtt):.0f} ms > {thr:.0f} ms", float(rtt)))
            else:
                for lk in links:
                    if lk.status in ("up", "degraded") and lk.last_latency_ms is not None and lk.last_latency_ms > thr:
                        d = by_id[lk.device_id]
                        out.append(Condition(d, f"latency:{lk.id}", f"{d.name}: WAN {lk.name} Latenz {lk.last_latency_ms:.0f} ms > {thr:.0f} ms", lk.last_latency_ms))
    elif rule.type == "mesh_down":
        peers = (await db.execute(select(VpnPeer).where(VpnPeer.status == "down"))).scalars().all()
        for pr in peers:
            a, b = by_id.get(pr.device_a_id), by_id.get(pr.device_b_id)
            if a and b:
                out.append(Condition(a, f"mesh:{pr.id}", f"VPN-Tunnel {a.name} ↔ {b.name} ist down"))
    elif rule.type == "vrrp_master":
        insts = (await db.execute(select(VrrpInstance).where(VrrpInstance.device_id.in_(list(by_id)), VrrpInstance.enabled.is_(True),
                                                              VrrpInstance.state == "master"))).scalars().all()
        for inst in insts:
            d = by_id[inst.device_id]
            out.append(Condition(d, f"vrrp:{inst.id}", f"{d.name}: VRRP {inst.name} (VRID {inst.vrid}) ist Master – Hauptsystem nicht erreichbar, "
                                                       f"Router übernimmt {inst.vip}", since=inst.last_change_at))
    elif rule.type == "wan_backup_active":
        links = (await db.execute(select(WanLink).where(WanLink.device_id.in_(list(by_id)), WanLink.enabled.is_(True)))).scalars().all()
        best: dict[uuid.UUID, int] = {}
        for lk in links:
            best[lk.device_id] = min(best.get(lk.device_id, lk.priority), lk.priority)
        for lk in links:
            d = by_id[lk.device_id]
            # nur Failover: bei Lastverteilung tragen alle Links planmäßig Traffic
            if d.wan_mode == "failover" and lk.active and lk.priority > best[lk.device_id]:
                out.append(Condition(d, f"wanactive:{lk.id}", f"{d.name}: Backup-WAN {lk.name} ({lk.interface}) trägt die Default-Route",
                                     since=lk.active_since))
    elif rule.type == "wan_volume":
        month = utcnow().strftime("%Y-%m")
        steps = sorted({float(x) for x in p.get("thresholds", [80, 100])})
        links = (await db.execute(select(WanLink).where(WanLink.device_id.in_(list(by_id)), WanLink.monthly_limit_gb.is_not(None)))).scalars().all()
        for lk in links:
            if lk.vol_month != month or not lk.monthly_limit_gb:
                continue  # neuer Monat -> Zähler beginnt bei 0, offene Alarme werden behoben
            d = by_id[lk.device_id]
            pct = 100 * (lk.vol_bytes or 0) / (lk.monthly_limit_gb * 1e9)
            for thr in steps:
                if pct >= thr:
                    out.append(Condition(d, f"volume{thr:g}:{lk.id}", f"{d.name}: WAN {lk.name} hat {pct:.0f} % des Monatsvolumens verbraucht "
                                                                      f"({(lk.vol_bytes or 0) / 1e9:.1f} von {lk.monthly_limit_gb:g} GB, Schwelle {thr:g} %)", round(pct, 1)))
    elif rule.type == "cpu_high":
        thr = float(p.get("threshold", 90))
        for d in devs:
            cpu = (d.facts or {}).get("cpu_load")
            if d.status == DeviceStatus.online and cpu is not None and float(cpu) > thr:
                out.append(Condition(d, "cpu", f"{d.name}: CPU {float(cpu):.0f}% > {thr:.0f}%", float(cpu)))
    return out


def _recipients(rule: AlertRule, tenant: Tenant | None) -> list[str]:
    if rule.recipients:
        return list(rule.recipients)
    return [tenant.contact_email] if tenant and tenant.contact_email else []


async def _notify(db: AsyncSession, rule: AlertRule, alert: Alert, tenant: Tenant | None, resolved: bool) -> None:
    from app.services import mail_render

    ctx = await mail_render.alert_context(db, rule, alert, tenant, resolved)
    subject, text, html = mail_render.render_alert(ctx)
    if await send_mail(_recipients(rule, tenant), subject, text, html=html):
        alert.notified = True
    if rule.webhook_url_enc:
        from app.security import decrypt_secret
        from app.services import webhook

        payload = webhook.build_payload(
            rule.webhook_format, title=subject, text=ctx["headline"], severity=alert.severity, resolved=resolved,
            facts={"Mandant": ctx["tenant"] or "-", "Standort": ctx["site"] or "", "Gerät": ctx["device_line"] or "",
                   "Typ": ctx["type_label"], "Status": "behoben" if resolved else "aktiv",
                   "Beginn": ctx["start"], "Ende": ctx["end"] or "", "Dauer": ctx["duration"]},
            link=ctx["device_url"],
            extra={"event": "alert.resolved" if resolved else "alert.firing", "alert_id": str(alert.id), "rule": rule.name, "type": rule.type,
                   "tenant": tenant.name if tenant else None, "device_id": str(alert.device_id) if alert.device_id else None,
                   "subject": alert.subject, "message": alert.message, "value": alert.value, "started_at": alert.started_at.isoformat(),
                   "resolved_at": alert.resolved_at.isoformat() if resolved and alert.resolved_at else None},
        )
        if await webhook.send(decrypt_secret(rule.webhook_url_enc), payload):
            alert.notified = True


async def evaluate_tenant(db: AsyncSession, tenant: Tenant) -> dict[str, int]:
    stats = {"fired": 0, "resolved": 0, "pending": 0}
    rules = (await db.execute(select(AlertRule).where(AlertRule.tenant_id == tenant.id, AlertRule.enabled.is_(True)))).scalars().all()
    if not rules:
        return stats
    devices = list((await db.execute(select(Device).where(Device.tenant_id == tenant.id, Device.pairing_status == PairingStatus.paired))).scalars())
    open_alerts = (await db.execute(select(Alert).where(Alert.tenant_id == tenant.id, Alert.status.in_(("pending", "firing"))))).scalars().all()
    by_key = {(a.rule_id, a.subject, a.device_id): a for a in open_alerts}
    now = utcnow()
    for rule in rules:
        seen: set[tuple[Any, ...]] = set()
        for c in await conditions(db, rule, devices):
            key = (rule.id, c.subject, c.device.id)
            seen.add(key)
            alert = by_key.get(key)
            if alert is None:
                start = min(c.since, now) if c.since else now
                alert = Alert(tenant_id=tenant.id, rule_id=rule.id, device_id=c.device.id, subject=c.subject, status="pending",
                              severity=rule.severity, message=c.message, value=c.value, started_at=start)
                db.add(alert)
                by_key[key] = alert
            else:
                alert.message, alert.value = c.message, c.value
            if alert.status == "pending":
                if (now - alert.started_at).total_seconds() >= rule.duration_s:
                    alert.status, alert.fired_at = "firing", now
                    stats["fired"] += 1
                    await _notify(db, rule, alert, tenant, resolved=False)
                    await events.publish(tenant.id, "alert.firing", {"rule": rule.name, "message": alert.message, "severity": alert.severity,
                                                                     "device_id": str(c.device.id)})
                else:
                    stats["pending"] += 1
        for key, alert in list(by_key.items()):
            if key[0] != rule.id or key in seen:
                continue
            if alert.status == "pending":
                await db.delete(alert)
            elif alert.status == "firing":
                alert.status, alert.resolved_at = "resolved", now
                stats["resolved"] += 1
                if rule.notify_resolved:
                    await _notify(db, rule, alert, tenant, resolved=True)
                await events.publish(tenant.id, "alert.resolved", {"rule": rule.name, "message": alert.message, "device_id": str(alert.device_id)})
            by_key.pop(key)
    return stats


async def evaluate_all() -> None:
    async with system_session() as db:
        for tenant in (await db.execute(select(Tenant).where(Tenant.is_active.is_(True)))).scalars().all():
            try:
                await evaluate_tenant(db, tenant)
            except Exception:  # noqa: BLE001
                log.exception("Alert-Auswertung %s fehlgeschlagen", tenant.slug)
        await db.commit()


async def create_default_rules(db: AsyncSession, tenant_id: uuid.UUID) -> list[AlertRule]:
    rules = [AlertRule(tenant_id=tenant_id, **{"params": {}, **r}) for r in DEFAULT_RULES]
    db.add_all(rules)
    await db.flush()
    return rules
