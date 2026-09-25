"""WAN-Links pro Gerät (Phase 3)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TenantScoped, UTCDateTime


class WanLink(IdMixin, TenantScoped, Base):
    __tablename__ = "wan_links"
    __table_args__ = (UniqueConstraint("device_id", "slot", name="uq_wan_links_device_slot"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int] = mapped_column(Integer)  # 1..4, stabil -> Routing-Tabelle sdwan-wan<slot>
    name: Mapped[str] = mapped_column(String(100))
    interface: Mapped[str] = mapped_column(String(100))
    # IP-Adresse, Interface-Name (PPPoE/LTE) oder "dhcp" (wird vom DHCP-Client gelesen)
    gateway: Mapped[str] = mapped_column(String(100))
    resolved_gateway: Mapped[str | None] = mapped_column(String(100))
    priority: Mapped[int] = mapped_column(Integer, default=1)  # 1 = bevorzugt (Failover)
    weight: Mapped[int] = mapped_column(Integer, default=1)  # Lastverteilung (PCC)
    check_type: Mapped[str] = mapped_column(String(10), default="ping")  # ping | http
    check_target: Mapped[str] = mapped_column(String(255))  # IP (ping) oder IP für HTTP-Check
    check_interval_s: Mapped[int] = mapped_column(Integer, default=10)
    check_timeout_ms: Mapped[int] = mapped_column(Integer, default=1000)
    loss_threshold_pct: Mapped[int] = mapped_column(Integer, default=50)
    latency_threshold_ms: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_limit_gb: Mapped[float | None] = mapped_column(Float)  # Datenvolumen-Limit (z. B. 5G-Tarif)

    # Laufzeitstatus (vom Poller)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # up | down | degraded | disabled | unknown
    active: Mapped[bool] = mapped_column(Boolean, default=False)  # trägt aktuell die Default-Route
    active_since: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())  # letzter Wechsel von ``active``
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_loss_pct: Mapped[float | None] = mapped_column(Float)
    last_check_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_change_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())

    # Monatsvolumen (rx+tx des Interfaces), aufsummiert aus den Zähler-Deltas jedes Polls
    vol_month: Mapped[str | None] = mapped_column(String(7))  # "YYYY-MM" (UTC)
    vol_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    vol_last_rx: Mapped[int | None] = mapped_column(BigInteger)
    vol_last_tx: Mapped[int | None] = mapped_column(BigInteger)
