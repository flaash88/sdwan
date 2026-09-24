"""Kernmodelle: Tenants, Benutzer, Sites, Devices, Audit-Log, System-Settings."""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class Role(str, enum.Enum):
    admin = "admin"
    technician = "technician"
    readonly = "readonly"


ROLE_RANK = {Role.readonly: 0, Role.technician: 1, Role.admin: 2}


class Tenant(IdMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Topologie für das Site-to-Site-Mesh (Phase 2): hub_spoke | full_mesh | none
    mesh_topology: Mapped[str] = mapped_column(String(20), default="hub_spoke")
    # /24 aus settings.mesh_network, wird bei Bedarf vergeben
    mesh_subnet: Mapped[str | None] = mapped_column(String(32), unique=True)
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)

    sites: Mapped[list[Site]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(IdMixin, Base):
    """Benutzer. ``tenant_id`` NULL + ``is_superuser`` = MSP-Mitarbeiter mit Zugriff auf alle Tenants."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=20), default=Role.readonly)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class Site(IdMixin, TenantScoped, Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_sites_tenant_name"),)

    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    # LAN-Präfixe der Site, die ins Mesh announced werden (z. B. ["192.168.10.0/24"])
    lan_subnets: Mapped[list] = mapped_column(JSONType, default=list)
    # Rolle im Hub-and-Spoke-Mesh
    is_mesh_hub: Mapped[bool] = mapped_column(Boolean, default=False)
    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()

    tenant: Mapped[Tenant] = relationship(back_populates="sites")
    devices: Mapped[list[Device]] = relationship(back_populates="site")


class PairingStatus(str, enum.Enum):
    pending = "pending"  # angelegt, wartet auf Onboarding-Script
    paired = "paired"  # Public-Key registriert, Tunnel konfiguriert
    revoked = "revoked"  # gesperrt, Peer vom Hub entfernt


class DeviceStatus(str, enum.Enum):
    unknown = "unknown"
    online = "online"
    offline = "offline"


class Device(IdMixin, TenantScoped, Base):
    __tablename__ = "devices"

    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    serial: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(100))
    identity: Mapped[str | None] = mapped_column(String(200))
    routeros_version: Mapped[str | None] = mapped_column(String(64))
    architecture: Mapped[str | None] = mapped_column(String(32))

    # Management-Tunnel
    tunnel_ip: Mapped[str] = mapped_column(String(45), unique=True)
    wg_public_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    api_password_enc: Mapped[str | None] = mapped_column(Text)

    pairing_status: Mapped[PairingStatus] = mapped_column(
        Enum(PairingStatus, native_enum=False, length=20), default=PairingStatus.pending
    )
    pairing_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pairing_expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    paired_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())

    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, native_enum=False, length=20), default=DeviceStatus.unknown
    )
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_handshake_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    uptime: Mapped[str | None] = mapped_column(String(64))
    # Site-to-Site-Mesh (Phase 2)
    mesh_ip: Mapped[str | None] = mapped_column(String(45))
    mesh_public_key: Mapped[str | None] = mapped_column(String(64))
    # Öffentliche Adresse für eingehende Mesh-Verbindungen (leer = vom Hub erkannte Adresse)
    mesh_endpoint: Mapped[str | None] = mapped_column(String(255))

    # Zero-Touch-Provisioning (Phase 6): none | staged | paired | provisioning | provisioned | failed
    ztp_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provisioning_templates.id", ondelete="SET NULL"), index=True
    )
    ztp_state: Mapped[str] = mapped_column(String(20), default="none")
    ztp_log: Mapped[list] = mapped_column(JSONType, default=list)

    # WAN (Phase 3): failover | loadbalance_pcc | loadbalance_ecmp
    wan_mode: Mapped[str] = mapped_column(String(30), default="failover")
    wan_options: Mapped[dict] = mapped_column(JSONType, default=dict)

    tags: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    # Zuletzt gemeldete Systeminfos (Board, Speicher, ...)
    facts: Mapped[dict] = mapped_column(JSONType, default=dict)

    site: Mapped[Site | None] = relationship(back_populates="devices")


class AuditLog(IdMixin, Base):
    """Audit-Log für alle schreibenden Aktionen. tenant_id NULL = MSP-/Systemebene."""

    __tablename__ = "audit_logs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    user_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class SystemSetting(Base):
    """Einfacher Key/Value-Store (z. B. Hub-Public-Key)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class HubPeerStat(Base):
    """Vom Hub-Agent gemeldete WireGuard-Peer-Statistik (1 Zeile pro Peer, überschrieben)."""

    __tablename__ = "hub_peer_stats"

    public_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str | None] = mapped_column(String(100))
    latest_handshake: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    rx_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), default=0)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
