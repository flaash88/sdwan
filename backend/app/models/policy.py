"""Firewall-/Security-Policies (Phase 5)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class FirewallPolicy(IdMixin, GlobalOrTenantScoped, Base):
    """Policy: global (tenant_id NULL, nur MSP) oder mandantenspezifisch."""

    __tablename__ = "firewall_policies"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # {"address_lists": [...], "filter": [...], "nat": [...]}
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class PolicyVersion(IdMixin, Base):
    """Unveränderlicher Snapshot jeder Policy-Version (Basis für Rollback)."""

    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),)

    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firewall_policies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(255))


class PolicyAssignment(IdMixin, TenantScoped, Base):
    __tablename__ = "policy_assignments"
    __table_args__ = (UniqueConstraint("policy_id", "device_id", name="uq_policy_assignments_policy_device"),)

    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firewall_policies.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=100)  # Reihenfolge mehrerer Policies auf einem Gerät
    deployed_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | deployed | failed | rolled_back
    last_error: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())


class PolicyDeployment(IdMixin, Base):
    """Ein Push-Vorgang auf mehrere Geräte. tenant_id NULL = MSP-weiter Push einer globalen Policy."""

    __tablename__ = "policy_deployments"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("firewall_policies.id", ondelete="SET NULL"), index=True)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    atomic: Mapped[bool] = mapped_column(default=False)
    # queued | running | success | partial | failed | rolled_back
    status: Mapped[str] = mapped_column(String(20), default="queued")
    started_by: Mapped[str | None] = mapped_column(String(255))
    results: Mapped[dict] = mapped_column(JSONType, default=dict)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
