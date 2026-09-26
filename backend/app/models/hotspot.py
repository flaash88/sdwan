"""Hotspot / Gäste-Portal (Phase 20): Portale (Vorlagen als Seed), Hotspots je Gerät, Voucher, Gäste-Registrierungen."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, GlobalOrTenantScoped, IdMixin, JSONType, TenantScoped, UTCDateTime


class HotspotPortal(IdMixin, GlobalOrTenantScoped, Base):
    """Aussehen und Anmeldeart der Login-Seite. Global = Vorlage (Seed, schreibgeschützt), sonst mandantenweit."""

    __tablename__ = "hotspot_portals"

    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)
    login_type: Mapped[str] = mapped_column(String(10), default="voucher")  # voucher | click | form
    # {"primary": "#…", "background": "#…", "text": "#…", "logo": "data:image/…" | None}
    design: Mapped[dict] = mapped_column(JSONType, default=dict)
    # {"de": {"title", "welcome", "button", "terms_label", "terms", "success"}, "en": {…}}
    texts: Mapped[dict] = mapped_column(JSONType, default=dict)
    terms_required: Mapped[bool] = mapped_column(Boolean, default=True)
    # [{"key", "label_de", "label_en", "type": text|email|tel|checkbox, "required", "max_len"}]
    form_fields: Mapped[list] = mapped_column(JSONType, default=list)
    # eigene Login-Seiten: {"login.html": "<…>", …} – ersetzen die erzeugten Dateien
    custom_files: Mapped[dict] = mapped_column(JSONType, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class HotspotInstance(IdMixin, TenantScoped, Base):
    """Hotspot auf einem Interface/VLAN eines Geräts."""

    __tablename__ = "hotspot_instances"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_hotspot_instances_tenant_slug"),)

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(20))  # Router-Objekte sdwan-hs-<slug>
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    portal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hotspot_portals.id", ondelete="RESTRICT"), index=True)
    interface: Mapped[str] = mapped_column(String(64))
    hotspot_address: Mapped[str | None] = mapped_column(String(64))  # leer = IP des Interfaces
    dns_name: Mapped[str | None] = mapped_column(String(200))
    walled_garden: Mapped[list] = mapped_column(JSONType, default=list)  # Hosts, zusätzlich zur Plattform
    session_timeout_min: Mapped[int] = mapped_column(Integer, default=240)  # Klick-/Formular-Anmeldung
    idle_timeout_min: Mapped[int] = mapped_column(Integer, default=15)
    rate_limit: Mapped[str | None] = mapped_column(String(40))  # z. B. "5M/10M" (Upload/Download), leer = unbegrenzt
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    applied_version: Mapped[int | None] = mapped_column(Integer)
    applied_portal_version: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | ok | error
    last_error: Mapped[str | None] = mapped_column(Text)


class VoucherProfile(IdMixin, TenantScoped, Base):
    __tablename__ = "voucher_profiles"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(20))
    validity_min: Mapped[int] = mapped_column(Integer, default=1440)  # Online-Zeit (limit-uptime)
    data_limit_mb: Mapped[int | None] = mapped_column(Integer)  # limit-bytes-total
    rate_limit: Mapped[str | None] = mapped_column(String(40))
    shared_users: Mapped[int] = mapped_column(Integer, default=1)  # Geräte je Voucher


class VoucherBatch(IdMixin, TenantScoped, Base):
    __tablename__ = "voucher_batches"

    instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hotspot_instances.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_profiles.id", ondelete="RESTRICT"), index=True)
    count: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(255))


class Voucher(IdMixin, TenantScoped, Base):
    __tablename__ = "vouchers"
    __table_args__ = (UniqueConstraint("instance_id", "code", name="uq_vouchers_instance_code"),)

    instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hotspot_instances.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_batches.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_profiles.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="new")  # new | active | used | blocked
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    first_used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    uptime_s: Mapped[int] = mapped_column(Integer, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)


class GuestRegistration(IdMixin, TenantScoped, Base):
    """Formular-Anmeldung: nur die im Portal definierten Felder, gelöscht nach der Aufbewahrungsfrist des Mandanten."""

    __tablename__ = "guest_registrations"

    instance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hotspot_instances.id", ondelete="CASCADE"), index=True)
    portal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hotspot_portals.id", ondelete="SET NULL"))
    data: Mapped[dict] = mapped_column(JSONType, default=dict)
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    lang: Mapped[str | None] = mapped_column(String(2))
