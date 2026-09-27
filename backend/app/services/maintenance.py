"""Wartungsfenster (Phase 18): einmalig oder wöchentlich, in der Zeitzone des Mandanten.

Während eines Fensters (mit ``suppress_alerts``) lösen anliegende Bedingungen keinen Alarm aus: der Alarm bleibt
„ausstehend“ und trägt ``suppressed_reason`` (sichtbar). Besteht die Bedingung nach dem Fenster noch, wird normal
alarmiert. Bereits ausgelöste Alarme bleiben unverändert. Firmware-Jobs mit ``only_in_window`` starten ein Gerät nur,
wenn für dieses Gerät ein Fenster mit ``firmware_allowed`` aktiv ist; sonst wartet es auf das nächste Fenster.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, MaintenanceWindow, Tenant
from app.services.mail_render import tzinfo

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")


class MaintenanceError(ValueError):
    pass


def validate(kind: str, start_at: dt.datetime | None, weekdays: list[int], start_time: str | None, duration_min: int) -> None:
    if kind not in ("once", "weekly"):
        raise MaintenanceError("Art: einmalig oder wöchentlich")
    if not 1 <= duration_min <= 7 * 24 * 60:
        raise MaintenanceError("Dauer: 1 Minute bis 7 Tage")
    if kind == "once" and start_at is None:
        raise MaintenanceError("Einmaliges Fenster braucht einen Beginn")
    if kind == "weekly":
        if not weekdays or any(d not in range(7) for d in weekdays):
            raise MaintenanceError("Wöchentlich: mindestens ein Wochentag (0=Mo … 6=So)")
        if not start_time or not _HHMM.match(start_time):
            raise MaintenanceError("Wöchentlich: Uhrzeit im Format HH:MM")


def is_active(w: MaintenanceWindow, now: dt.datetime, tz_name: str | None) -> bool:
    if not w.enabled:
        return False
    dur = dt.timedelta(minutes=w.duration_min)
    if w.kind == "once":
        return w.start_at is not None and w.start_at <= now < w.start_at + dur
    tz = tzinfo(tz_name)
    local = now.astimezone(tz)
    hh, mm = (int(x) for x in (w.start_time or "00:00").split(":"))
    # Fenster können über Mitternacht (und über mehrere Tage) reichen: Starts der letzten 7 Tage prüfen
    for back in range(0, 8):
        day = (local - dt.timedelta(days=back)).date()
        if day.weekday() not in (w.weekdays or []):
            continue
        start = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        if start <= local < start + dur:
            return True
    return False


def applies(w: MaintenanceWindow, device: Device) -> bool:
    if w.device_id is not None:
        return w.device_id == device.id
    if w.site_id is not None:
        return w.site_id == device.site_id
    return w.tenant_id == device.tenant_id


async def active_windows(db: AsyncSession, tenant: Tenant, now: dt.datetime) -> list[MaintenanceWindow]:
    rows = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.tenant_id == tenant.id,
                                                            MaintenanceWindow.enabled.is_(True)))).scalars().all()
    return [w for w in rows if is_active(w, now, tenant.timezone)]


def window_for(windows: list[MaintenanceWindow], device: Device, what: str = "alerts") -> MaintenanceWindow | None:
    for w in windows:
        if applies(w, device) and (w.suppress_alerts if what == "alerts" else w.firmware_allowed):
            return w
    return None


async def devices_in_window(db: AsyncSession, devices: list[Device], now: dt.datetime) -> dict[Any, MaintenanceWindow]:
    """Gerät -> aktives Firmware-Fenster (für Firmware-Jobs mit ``only_in_window``)."""
    out: dict[Any, MaintenanceWindow] = {}
    cache: dict[Any, list[MaintenanceWindow]] = {}
    for d in devices:
        if d.tenant_id not in cache:
            tenant = await db.get(Tenant, d.tenant_id)
            cache[d.tenant_id] = await active_windows(db, tenant, now) if tenant else []
        w = window_for(cache[d.tenant_id], d, "firmware")
        if w is not None:
            out[d.id] = w
    return out
