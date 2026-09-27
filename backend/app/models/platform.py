"""Plattform-Betrieb (Phase 21): Sicherungen der Plattform selbst und plattformweite Alarme (ohne Mandant)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, UTCDateTime


class PlatformBackup(IdMixin, Base):
    __tablename__ = "platform_backups"

    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | failed | not_configured
    trigger: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | manual | cli
    filename: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    targets: Mapped[dict] = mapped_column(JSONType, default=dict)  # {"local": "ok", "remote": "ok"|"Fehler …"}
    contents: Mapped[dict] = mapped_column(JSONType, default=dict)  # Manifest-Auszug (Teile, Größen, Warnungen)
    error: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class PlatformAlert(IdMixin, Base):
    """Plattformweiter Alarm (z. B. Sicherung fehlgeschlagen); sichtbar nur für MSP-Admins."""

    __tablename__ = "platform_alerts"

    type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default="firing", index=True)  # firing | resolved
    severity: Mapped[str] = mapped_column(String(20), default="critical")
    message: Mapped[str] = mapped_column(Text)
    fired_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
