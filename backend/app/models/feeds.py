"""Threat-Feeds (Phase 15): Blocklisten aus dem Internet als Address-List ``sdwan-feed-<slug>`` auf Geräten."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class ThreatFeed(IdMixin, GlobalOrTenantScoped, Base):
    __tablename__ = "threat_feeds"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(String(500))
    fmt: Mapped[str] = mapped_column(String(10), default="lines")  # lines (IP/CIDR je Zeile) | jsonl (ein JSON-Objekt je Zeile)
    json_field: Mapped[str] = mapped_column(String(40), default="cidr")
    comment_chars: Mapped[str] = mapped_column(String(10), default="#;")
    interval_min: Mapped[int] = mapped_column(Integer, default=360)
    max_entries: Mapped[int] = mapped_column(Integer, default=20000)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)
    # Status / letzte gültige Liste (bleibt bei Ladefehlern aktiv)
    entries: Mapped[list] = mapped_column(JSONType, default=list)
    entries_hash: Mapped[str | None] = mapped_column(String(64))
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    last_fetch_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class ThreatFeedAssignment(IdMixin, TenantScoped, Base):
    __tablename__ = "threat_feed_assignments"
    __table_args__ = (UniqueConstraint("feed_id", "device_id", name="uq_threat_feed_assignments_feed_device"),)

    feed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("threat_feeds.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | ok | skipped_memory | error
    synced_hash: Mapped[str | None] = mapped_column(String(64))
    synced_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
