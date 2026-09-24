"""Content-Filter-Profile (Phase 7, NextDNS)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class ContentFilterProfile(IdMixin, TenantScoped, Base):
    __tablename__ = "content_filter_profiles"

    name: Mapped[str] = mapped_column(String(200))
    nextdns_profile_id: Mapped[str | None] = mapped_column(String(32))
    categories: Mapped[list] = mapped_column(JSONType, default=list)  # Parental-Control-Kategorien
    services: Mapped[list] = mapped_column(JSONType, default=list)  # blockierte Dienste (tiktok, ...)
    security: Mapped[dict] = mapped_column(JSONType, default=dict)
    blocklists: Mapped[list] = mapped_column(JSONType, default=list)
    denylist: Mapped[list] = mapped_column(JSONType, default=list)
    allowlist: Mapped[list] = mapped_column(JSONType, default=list)
    safe_search: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    block_bypass: Mapped[bool] = mapped_column(Boolean, default=True)
    # DNS auf dem Router erzwingen (NAT-Redirect Port 53 aus den LANs)
    force_dns: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | synced | error
    last_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
