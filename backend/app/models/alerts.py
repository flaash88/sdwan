"""Alerts, Statusverlauf und SLA-Reports (Phase 10)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class StatusEvent(IdMixin, TenantScoped, Base):
    """Statuswechsel eines Geräts ("device") oder WAN-Links ("wan:<link_id>") – Basis für SLA."""

    __tablename__ = "status_events"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(64), default="device", index=True)
    status: Mapped[str] = mapped_column(String(20))  # online/offline bzw. up/down/degraded
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)


class AlertRule(IdMixin, TenantScoped, Base):
    __tablename__ = "alert_rules"

    name: Mapped[str] = mapped_column(String(200))
    # device_offline | wan_down | latency | mesh_down | cpu_high | vrrp_master | wan_backup_active | wan_volume
    type: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(20), default="warning")  # info | warning | critical
    # {"threshold": 150, "metric": "wan"|"mgmt"}
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    duration_s: Mapped[int] = mapped_column(Integer, default=120)  # Bedingung muss so lange anliegen
    site_ids: Mapped[list] = mapped_column(JSONType, default=list)  # leer = alle
    device_ids: Mapped[list] = mapped_column(JSONType, default=list)
    recipients: Mapped[list] = mapped_column(JSONType, default=list)
    notify_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Webhook zusätzlich zur E-Mail (Phase 11); URL verschlüsselt, da sie oft eine Signatur enthält
    webhook_url_enc: Mapped[str | None] = mapped_column(Text)
    webhook_format: Mapped[str] = mapped_column(String(20), default="generic", server_default="generic")  # generic | teams


class Alert(IdMixin, TenantScoped, Base):
    __tablename__ = "alerts"

    rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(100))  # z. B. "device", "wan:<id>", "mesh:<id>"
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending | firing | resolved
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column()
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    fired_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(255))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class SlaReport(IdMixin, TenantScoped, Base):
    __tablename__ = "sla_reports"

    period_start: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    period_end: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    summary: Mapped[dict] = mapped_column(JSONType, default=dict)
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
    sent_to: Mapped[list] = mapped_column(JSONType, default=list)
