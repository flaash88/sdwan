"""Zero-Touch-Provisioning (Phase 6)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class ProvisioningTemplate(IdMixin, TenantScoped, Base):
    __tablename__ = "provisioning_templates"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # identity_pattern, timezone, ntp_servers, dns_servers, wan_interface, lan{...}, wan{...}, policy_ids[]
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
