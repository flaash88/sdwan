"""VRRP-Instanzen pro Gerät (Phase 11)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TenantScoped, UTCDateTime


class VrrpInstance(IdMixin, TenantScoped, Base):
    __tablename__ = "vrrp_instances"
    __table_args__ = (UniqueConstraint("device_id", "name", name="uq_vrrp_instances_device_name"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))  # Name des VRRP-Interfaces auf dem Router
    interface: Mapped[str] = mapped_column(String(100))  # Parent-Interface (z. B. ether2 oder VLAN)
    vrid: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    interval_ms: Mapped[int] = mapped_column(Integer, default=1000)
    preemption: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=3)
    vip: Mapped[str] = mapped_column(String(64))  # immer /32
    local_address: Mapped[str | None] = mapped_column(String(64))  # z. B. 192.168.110.21/24 auf dem Parent
    linked_wan_slot: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Laufzeit
    state: Mapped[str] = mapped_column(String(20), default="unknown")  # master | backup | disabled | unknown
    last_change_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
