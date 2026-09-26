"""Zentrales Syslog (Phase 18).

Router senden per ``/system logging action`` (target=remote) über den Management-Tunnel an die Hub-Adresse; der
Empfänger (``app/syslog_receiver.py``, eigener Container im Netz-Namespace des Hubs) ordnet die Quell-IP der
Tunnel-IP eines Geräts zu. Opt-in je Gerät; die Plattform verwaltet nur die Aktion ``sdwan-syslog`` und die
Logging-Regeln mit ``action=sdwan-syslog`` (ANNAHME Labor: Aktionen/Regeln haben kein Kommentarfeld – Erkennung über
Namen/Aktion). Aufbewahrung je Mandant (``tenant.settings.syslog_retention_days``, Standard 30 Tage).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceSyslog, SyslogMessage, Tenant
from app.routeros.client import DeviceAPI

ACTION = "sdwan-syslog"
DEFAULT_TOPICS = ["critical", "error", "warning", "info"]
ALLOWED_TOPICS = {"critical", "error", "warning", "info", "debug", "system", "firewall", "dhcp", "wireless", "wifi", "interface",
                  "account", "script", "ipsec", "l2tp", "pppoe", "ppp", "ovpn", "wireguard", "vrrp", "hotspot", "dns", "route",
                  "bgp", "ospf", "caps", "certificate", "health", "netwatch", "backup", "ntp", "ssh", "web-proxy", "event"}
DEFAULT_RETENTION_DAYS = 30
_PRI = re.compile(r"^<(\d{1,3})>")
_TS = re.compile(r"^(?:[A-Z][a-z]{2}\s+\d{1,2}\s+\d\d:\d\d:\d\d|\d{4}-\d\d-\d\dT\S+)\s+")
_TOPICS = re.compile(r"^([a-z0-9\-]+(?:,[a-z0-9\-]+)+|[a-z0-9\-]+)\s+(.*)$", re.DOTALL)


def validate_topics(topics: list[str]) -> list[str]:
    bad = [t for t in topics if t not in ALLOWED_TOPICS]
    if bad:
        raise ValueError(f"Unbekannte Topics: {', '.join(bad)}")
    return list(dict.fromkeys(topics)) or DEFAULT_TOPICS


def parse(data: bytes) -> dict[str, Any]:
    """RFC3164-ähnliche RouterOS-Meldung -> {severity, facility, topics, message}. Robust gegen fehlende Teile."""
    text = data.decode("utf-8", errors="replace").strip()
    sev = fac = None
    m = _PRI.match(text)
    if m:
        pri = int(m.group(1))
        fac, sev = pri // 8, pri % 8
        text = text[m.end():]
    text = _TS.sub("", text, count=1)
    parts = text.split(" ", 1)
    # optionaler Hostname vor den Topics (ohne Komma, danach Topics mit Komma)
    if len(parts) == 2 and "," not in parts[0] and _TOPICS.match(parts[1]) and "," in parts[1].split(" ", 1)[0]:
        text = parts[1]
    topics = None
    tm = _TOPICS.match(text)
    if tm and ("," in tm.group(1) or tm.group(1) in ALLOWED_TOPICS):
        topics, text = tm.group(1), tm.group(2)
    return {"severity": sev, "facility": fac, "topics": topics, "message": text[:2000]}


def desired(device: Device, topics: list[str]) -> dict[str, Any]:
    s = get_settings()
    return {
        "action": {"name": ACTION, "target": "remote", "remote": s.wg_hub_ip, "remote-port": str(s.syslog_port),
                   "src-address": device.tunnel_ip or ""},
        "rules": [{"topics": t, "action": ACTION} for t in topics],
    }


async def apply(api: DeviceAPI, device: Device, enabled: bool, topics: list[str]) -> dict[str, int]:
    stats = {"rules_added": 0, "rules_removed": 0}
    rules = [r for r in await api.print("/system/logging") if r.get("action") == ACTION]
    actions = [a for a in await api.print("/system/logging/action") if a.get("name") == ACTION]
    if not enabled:
        for r in rules:
            await api.remove("/system/logging", r[".id"])
            stats["rules_removed"] += 1
        for a in actions:
            await api.remove("/system/logging/action", a[".id"])
        return stats
    want = desired(device, topics)
    if actions:
        a = actions[0]
        diff = {k: v for k, v in want["action"].items() if str(a.get(k, "")) != v and k != "name"}
        if diff:
            await api.set("/system/logging/action", a[".id"], **diff)
    else:
        await api.add("/system/logging/action", **want["action"])
    have = {str(r.get("topics")): r for r in rules}
    for t in topics:
        if t not in have:
            await api.add("/system/logging", topics=t, action=ACTION)
            stats["rules_added"] += 1
    for t, r in have.items():
        if t not in topics:
            await api.remove("/system/logging", r[".id"])
            stats["rules_removed"] += 1
    return stats


class DeviceMap:
    """Quell-IP (Tunnel-IP) -> (device_id, tenant_id), regelmäßig aus der DB aktualisiert."""

    def __init__(self) -> None:
        self.by_ip: dict[str, tuple[Any, Any]] = {}
        self.loaded_at: dt.datetime | None = None

    async def refresh(self, db: AsyncSession) -> None:
        enabled = {s.device_id for s in (await db.execute(select(DeviceSyslog).where(DeviceSyslog.enabled.is_(True)))).scalars()}
        self.by_ip = {d.tunnel_ip: (d.id, d.tenant_id) for d in (await db.execute(select(Device))).scalars()
                      if d.tunnel_ip and d.id in enabled}
        self.loaded_at = utcnow()


async def store(db: AsyncSession, dmap: DeviceMap, batch: list[tuple[str, bytes, dt.datetime]]) -> int:
    """Nachrichten speichern; unbekannte Quellen (kein Gerät/Syslog nicht aktiv) werden verworfen."""
    n = 0
    for ip, data, at in batch:
        target = dmap.by_ip.get(ip)
        if target is None:
            continue
        p = parse(data)
        db.add(SyslogMessage(tenant_id=target[1], device_id=target[0], received_at=at, severity=p["severity"], facility=p["facility"],
                             topics=p["topics"], message=p["message"]))
        n += 1
    return n


async def purge_old() -> int:
    """Worker-Job (täglich): Aufbewahrung je Mandant durchsetzen."""
    total = 0
    async with system_session() as db:
        for t in (await db.execute(select(Tenant))).scalars():
            days = int((t.settings or {}).get("syslog_retention_days") or DEFAULT_RETENTION_DAYS)
            res = await db.execute(delete(SyslogMessage).where(SyslogMessage.tenant_id == t.id,
                                                               SyslogMessage.received_at < utcnow() - dt.timedelta(days=days)))
            total += res.rowcount or 0
        await db.commit()
    return total
