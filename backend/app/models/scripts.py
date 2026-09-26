"""Script-Bibliothek und Massen-Ausführung (Phase 17)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class Script(IdMixin, GlobalOrTenantScoped, Base):
    __tablename__ = "scripts"

    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(10), default="read")  # read (nur lesend) | change (ändernd)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class ScriptVersion(IdMixin, Base):
    __tablename__ = "script_versions"
    __table_args__ = (UniqueConstraint("script_id", "version", name="uq_script_versions_script_version"),)

    script_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(10))
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(255))


class ScriptRun(IdMixin, Base):
    """Ausführung auf mehreren Geräten, gestaffelt. tenant_id NULL = MSP-weit über mehrere Mandanten."""

    __tablename__ = "script_runs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    script_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scripts.id", ondelete="SET NULL"), index=True)
    script_name: Mapped[str] = mapped_column(String(100))
    script_version: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)  # Stand bei Start (unveränderlich)
    targets: Mapped[dict] = mapped_column(JSONType, default=dict)
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    max_failures: Mapped[int] = mapped_column(Integer, default=1)
    current_batch: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | paused | completed | failed | cancelled
    created_by: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class ScriptRunItem(IdMixin, TenantScoped, Base):
    __tablename__ = "script_run_items"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("script_runs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | success | failed | skipped | cancelled
    rendered: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    backup_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("config_backups.id", ondelete="SET NULL"))
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
