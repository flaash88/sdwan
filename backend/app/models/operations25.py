"""Phase 25: Nachbarn, Top-Verbraucher (IPFIX-Aggregate), Inventar, EOL-Liste."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime

_BIG = BigInteger().with_variant(Integer(), "sqlite")


class DeviceNeighbor(IdMixin, TenantScoped, Base):
    """Eintrag aus ``/ip/neighbor`` (je Poll ersetzt)."""

    __tablename__ = "device_neighbors"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    interface: Mapped[str | None] = mapped_column(String(100))
    identity: Mapped[str | None] = mapped_column(String(200))
    platform: Mapped[str | None] = mapped_column(String(100))
    board: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[str | None] = mapped_column(String(100))
    mac_address: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(String(64))
    seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime())


class DeviceFlow(IdMixin, TenantScoped, Base):
    """Opt-in je Gerät: IPFIX an den Plattform-Collector (Top-Verbraucher)."""

    __tablename__ = "device_flows"
    __table_args__ = (UniqueConstraint("device_id", name="uq_device_flows_device"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interfaces: Mapped[list] = mapped_column(JSONType, default=list)  # WAN-Interfaces
    ifindex: Mapped[dict] = mapped_column(JSONType, default=dict)  # IPFIX-Interface-Index -> Name (ANNAHME)
    before: Mapped[dict | None] = mapped_column(JSONType)  # Vorzustand /ip/traffic-flow
    status: Mapped[str] = mapped_column(String(20), default="off")  # off | active | error
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class FlowAggregate(IdMixin, TenantScoped, Base):
    """5-Minuten-Aggregat: lokaler Host ↔ Gegenstelle je WAN. Keine Einzel-Flows, keine Ports."""

    __tablename__ = "flow_aggregates"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    bucket: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)
    wan: Mapped[str | None] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(64))
    peer: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(_BIG, default=0)
    packets: Mapped[int] = mapped_column(_BIG, default=0)


class DeviceInventory(IdMixin, TenantScoped, Base):
    __tablename__ = "device_inventory"
    __table_args__ = (UniqueConstraint("device_id", name="uq_device_inventory_device"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    purchase_date: Mapped[dt.date | None] = mapped_column(Date)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date)
    supplier: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class EolModel(IdMixin, Base):
    """Abgekündigte Modelle (global, Seed + MSP-pflegbar)."""

    __tablename__ = "eol_models"

    model: Mapped[str] = mapped_column(String(100), index=True)  # Vergleich ohne Groß-/Kleinschreibung und Leerzeichen
    status: Mapped[str] = mapped_column(String(20), default="eol")  # end_of_sale | eol
    since: Mapped[dt.date | None] = mapped_column(Date)
    successor: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)
