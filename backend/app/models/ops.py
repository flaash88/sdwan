"""Backups & Firmware (Phase 9)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class ConfigBackup(IdMixin, TenantScoped, Base):
    __tablename__ = "config_backups"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | manual | pre-update
    routeros_version: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    # Diff zum vorherigen Backup: {"previous_id", "added", "removed", "lines": [...]}
    diff: Mapped[dict] = mapped_column(JSONType, default=dict)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(500))


class FirmwareJob(IdMixin, Base):
    """Fleet-Update. tenant_id NULL = MSP-weiter Job über mehrere Mandanten."""

    __tablename__ = "firmware_jobs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), default="stable")
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    batch_interval_s: Mapped[int] = mapped_column(Integer, default=300)
    max_failures: Mapped[int] = mapped_column(Integer, default=1)
    upgrade_routerboard: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | paused | completed | failed | cancelled
    current_batch: Mapped[int] = mapped_column(Integer, default=0)
    next_batch_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    created_by: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class FirmwareJobItem(IdMixin, TenantScoped, Base):
    __tablename__ = "firmware_job_items"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firmware_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[int] = mapped_column(Integer, default=0)
    # queued | updating | rebooting | success | skipped | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    from_version: Mapped[str | None] = mapped_column(String(64))
    to_version: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
