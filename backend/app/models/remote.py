"""Remote-Access-Sessions (Phase 8)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TenantScoped, UTCDateTime


class RemoteSession(IdMixin, TenantScoped, Base):
    __tablename__ = "remote_sessions"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_email: Mapped[str] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(20))  # ssh | winbox | webfig
    target_port: Mapped[int] = mapped_column(Integer)
    listen_port: Mapped[int] = mapped_column(Integer, index=True)
    allowed_cidr: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    # Temporärer RouterOS-Benutzer für diese Session
    ros_username: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active | expired | closed | failed
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    closed_by: Mapped[str | None] = mapped_column(String(255))
    connections: Mapped[int] = mapped_column(Integer, default=0)
    bytes_in: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    bytes_out: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
