"""Letzter Hardware-Selbsttest je Gerät."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class DeviceSelftest(IdMixin, TenantScoped, Base):
    __tablename__ = "device_selftests"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(10))  # ok | warn | error
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ran_at: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    ran_by: Mapped[str | None] = mapped_column(String(255))
