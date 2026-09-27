"""Vor-Ort-Zugang (Break-Glass, Phase 24) und API-Tokens."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class LocalAccess(IdMixin, TenantScoped, Base):
    """Lokaler Notfall-Benutzer je Router (eigenes Passwort je Gerät)."""

    __tablename__ = "local_access"
    __table_args__ = (UniqueConstraint("device_id", name="uq_local_access_device"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # pending | active | not_created | error | disabled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reason: Mapped[str | None] = mapped_column(Text)  # Grund bei not_created/error
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str | None] = mapped_column(Text)
    password_set_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    viewed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    rotate_due_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())  # „nach Anzeige rotieren“
    networks: Mapped[list] = mapped_column(JSONType, default=list)  # zuletzt verwendete lokale Netze
    interfaces: Mapped[list] = mapped_column(JSONType, default=list)  # Mitglieder von sdwan-local-access
    manual_networks: Mapped[list] = mapped_column(JSONType, default=list)  # manuell erlaubte Netze
    # Service-Port: {"enabled", "interface", "network", "bridge_before": {...}}
    service_port: Mapped[dict] = mapped_column(JSONType, default=dict)
    # gemerkte Vorzustände (Rückstellung beim Deaktivieren/Offboarding)
    mac_winbox_before: Mapped[dict | None] = mapped_column(JSONType)
    services_before: Mapped[dict | None] = mapped_column(JSONType)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    # Policies aus LOCAL_POLICIES_FULL, die der Gruppe fehlen (nachträglich über den API-Benutzer angelegt)
    missing_policies: Mapped[list | None] = mapped_column(JSONType)
    # Bestätigte Ausnahmen „privates WAN-Netz“: [{network, interface, wan_network, confirmed_by, confirmed_at}]
    wan_exceptions: Mapped[list | None] = mapped_column(JSONType)


class ApiToken(IdMixin, Base):
    """API-Token eines Benutzers (gehasht). ``scope``: ``role`` = Rechte der Rolle, ``read`` = nur lesend.

    Nicht ``TenantScoped``: MSP-Admins haben keinen Mandanten; Abfragen filtern immer über ``user_id``."""

    __tablename__ = "api_tokens"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(10), default="read")
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    last_used_ip: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
