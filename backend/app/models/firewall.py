"""Firewall-Editor (Phase 14): Objekte, Dienste, Zonen, Bausteine, Zonen-Zuordnung, Trefferzähler.

Objekte/Dienste/Zonen/Bausteine sind global (tenant_id NULL, MSP) oder mandantenweit – wie Policies.
Vordefinierte Einträge kommen als Seed-Daten (``app/seeds``), erkennbar an ``builtin``/``seed_key``;
sie sind schreibgeschützt und werden über „Kopieren“ angepasst.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class _Seeded:
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)


class FwObject(IdMixin, GlobalOrTenantScoped, _Seeded, Base):
    """Netzwerk-Objekt → Address-List ``sdwan-obj-<slug>``."""

    __tablename__ = "fw_objects"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(60), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # host | network | range | group | feed
    values: Mapped[list] = mapped_column(JSONType, default=list)  # Adressen (host/network/range)
    members: Mapped[list] = mapped_column(JSONType, default=list)  # Objekt-IDs (group)
    description: Mapped[str | None] = mapped_column(Text)


class FwService(IdMixin, GlobalOrTenantScoped, _Seeded, Base):
    """Dienst: [{protocol, ports}] oder Dienstgruppe (members = Dienst-IDs)."""

    __tablename__ = "fw_services"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(60), index=True)
    entries: Mapped[list] = mapped_column(JSONType, default=list)
    members: Mapped[list] = mapped_column(JSONType, default=list)
    description: Mapped[str | None] = mapped_column(Text)


class FwZone(IdMixin, GlobalOrTenantScoped, _Seeded, Base):
    """Zone → Interface-List ``sdwan-zone-<slug>`` (source=wan: bestehende Liste ``sdwan-wan``)."""

    __tablename__ = "fw_zones"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(60), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | wan
    management: Mapped[bool] = mapped_column(Boolean, default=False)  # Management-Zugriff erlaubt
    description: Mapped[str | None] = mapped_column(Text)


class FwBlock(IdMixin, GlobalOrTenantScoped, _Seeded, Base):
    """Baustein: parametrisierte Regeln/NAT, per Klick in eine einfache Policy einfügbar."""

    __tablename__ = "fw_blocks"

    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[list] = mapped_column(JSONType, default=list)  # [{key, type: zone|object|service, label, optional}]
    rules: Mapped[list] = mapped_column(JSONType, default=list)
    nat: Mapped[list] = mapped_column(JSONType, default=list)


class DeviceZoneMember(IdMixin, TenantScoped, Base):
    __tablename__ = "device_zone_members"
    __table_args__ = (UniqueConstraint("device_id", "interface", name="uq_device_zone_members_device_iface"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    zone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fw_zones.id", ondelete="CASCADE"), index=True)
    interface: Mapped[str] = mapped_column(String(100))


class FwRuleHit(IdMixin, TenantScoped, Base):
    """Trefferzähler je Gerät und verwalteter Regel (Schlüssel ``<policy-tag>:<regel-id>``)."""

    __tablename__ = "fw_rule_hits"
    __table_args__ = (UniqueConstraint("device_id", "key", name="uq_fw_rule_hits_device_key"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80), index=True)
    packets: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    last_hit_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
