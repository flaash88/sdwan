"""Betrieb (Phase 18): Wartungsfenster, Speedtest-Ergebnisse, zentrales Syslog."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class MaintenanceWindow(IdMixin, TenantScoped, Base):
    """Wartungsfenster für den ganzen Mandanten, einen Standort oder ein Gerät (Zeitzone des Mandanten)."""

    __tablename__ = "maintenance_windows"

    name: Mapped[str] = mapped_column(String(200))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="once")  # once | weekly
    start_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())  # once
    weekdays: Mapped[list] = mapped_column(JSONType, default=list)  # weekly: 0=Mo … 6=So
    start_time: Mapped[str | None] = mapped_column(String(5))  # weekly: "HH:MM" lokale Zeit
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    suppress_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    firmware_allowed: Mapped[bool] = mapped_column(Boolean, default=True)  # Firmware-Rollouts mit „nur im Fenster“ erlaubt
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))


class SpeedtestResult(IdMixin, TenantScoped, Base):
    __tablename__ = "speedtest_results"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    wan_link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wan_links.id", ondelete="SET NULL"), index=True)
    slot: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | failed
    down_mbps: Mapped[float | None] = mapped_column(Float)
    up_mbps: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[int] = mapped_column(Integer, default=10)
    bytes_estimated: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class DeviceSyslog(IdMixin, TenantScoped, Base):
    """Syslog je Gerät (opt-in): welche Topics der Router an die Plattform schickt."""

    __tablename__ = "device_syslog"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    topics: Mapped[list] = mapped_column(JSONType, default=list)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class SyslogMessage(IdMixin, TenantScoped, Base):
    __tablename__ = "syslog_messages"
    __table_args__ = (Index("ix_syslog_messages_device_time", "device_id", "received_at"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)
    severity: Mapped[int | None] = mapped_column(Integer)  # 0 emerg … 7 debug (aus PRI)
    facility: Mapped[int | None] = mapped_column(Integer)
    topics: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
