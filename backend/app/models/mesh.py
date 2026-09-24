"""Site-to-Site-VPN (Phase 2)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TenantScoped, UTCDateTime


class VpnPeer(IdMixin, TenantScoped, Base):
    """Eine WireGuard-Verbindung zwischen zwei Geräten desselben Tenants (ungeordnetes Paar a<b)."""

    __tablename__ = "vpn_peers"
    __table_args__ = (UniqueConstraint("device_a_id", "device_b_id", name="uq_vpn_peers_pair"),)

    device_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    device_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    # hub_spoke | full_mesh
    kind: Mapped[str] = mapped_column(String(20), default="hub_spoke")
    psk_enc: Mapped[str] = mapped_column(Text)
    # up | down | unknown | no_endpoint
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_handshake_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    rx_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
