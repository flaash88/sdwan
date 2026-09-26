"""Compliance (Phase 16): Regelsets, Zuweisung (Geräte/Standorte/Tags) und Ergebnisse je Auswertung."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class ComplianceRuleSet(IdMixin, GlobalOrTenantScoped, Base):
    __tablename__ = "compliance_rule_sets"

    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[list] = mapped_column(JSONType, default=list)  # [{id, name, type, params}]
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)


class ComplianceAssignment(IdMixin, TenantScoped, Base):
    """Zuweisung eines Regelsets an Ziele eines Mandanten (aufgelöst bei jeder Auswertung)."""

    __tablename__ = "compliance_assignments"

    rule_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("compliance_rule_sets.id", ondelete="CASCADE"), index=True)
    targets: Mapped[dict] = mapped_column(JSONType, default=dict)  # {device_ids, site_ids, tags}


class ComplianceResult(IdMixin, TenantScoped, Base):
    __tablename__ = "compliance_results"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    rule_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("compliance_rule_sets.id", ondelete="CASCADE"), index=True)
    evaluated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)
    backup_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("config_backups.id", ondelete="SET NULL"))
    results: Mapped[list] = mapped_column(JSONType, default=list)  # [{rule_id, name, status: ok|fail|unknown, detail}]
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    unknown: Mapped[int] = mapped_column(Integer, default=0)
