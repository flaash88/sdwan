"""WLAN-Verwaltung (Phase 19): Profile, Zuweisungen (Geräte/Standorte/Tags) und Ausroll-Status je Gerät."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, JSONType, TenantScoped, UTCDateTime


class WlanProfile(IdMixin, TenantScoped, Base):
    """Ein WLAN (SSID) mit Sicherheit, Funk- und Netz-Einstellungen. Mandantenweit, weil es Schlüssel enthält."""

    __tablename__ = "wlan_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_wlan_profiles_tenant_slug"),)

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(20))  # Teil der Router-Objektnamen sdwan-wifi-<slug>
    description: Mapped[str | None] = mapped_column(Text)
    ssid: Mapped[str] = mapped_column(String(32))
    # wpa2-psk | wpa2-wpa3-psk | wpa3-psk | wpa2-eap | wpa3-eap
    security: Mapped[str] = mapped_column(String(20), default="wpa2-wpa3-psk")
    passphrase_enc: Mapped[str | None] = mapped_column(Text)
    radius_server: Mapped[str | None] = mapped_column(String(100))
    radius_port: Mapped[int] = mapped_column(Integer, default=1812)
    radius_secret_enc: Mapped[str | None] = mapped_column(Text)
    band: Mapped[str] = mapped_column(String(10), default="both")  # 2ghz | 5ghz | both
    channel_width: Mapped[str] = mapped_column(String(10), default="auto")  # auto | 20 | 40 | 80 | 160
    country_code: Mapped[str | None] = mapped_column(String(2))  # leer = Ländercode des Mandanten
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    bridge: Mapped[str] = mapped_column(String(64), default="bridge")
    client_isolation: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule: Mapped[dict | None] = mapped_column(JSONType)  # {"start": "07:00", "end": "22:00"} täglich
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    psk_rotate_days: Mapped[int | None] = mapped_column(Integer)  # automatische Rotation (nur Gäste-WLAN), leer = aus
    psk_rotated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(255))


class WlanAssignment(IdMixin, TenantScoped, Base):
    """Ziele eines Profils. ``mode=local``: WLAN auf den Radios des Geräts; ``capsman``: Gerät ist CAPsMAN-Controller
    und verteilt das Profil per Provisioning an seine CAPs."""

    __tablename__ = "wlan_assignments"

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wlan_profiles.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(10), default="local")
    targets: Mapped[dict] = mapped_column(JSONType, default=dict)  # {device_ids, site_ids, tags}


class WlanDeviceState(IdMixin, TenantScoped, Base):
    """Ausroll-Status je Gerät und Profil (Profil NULL = Ergebnis des letzten Abgleichs ohne Profil)."""

    __tablename__ = "wlan_device_states"
    __table_args__ = (UniqueConstraint("device_id", "profile_id", name="uq_wlan_device_states_device_profile"),)

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wlan_profiles.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(10), default="local")
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending | ok | error | unsupported_driver | no_wlan | offline
    applied_version: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime())
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)  # z. B. {"interfaces": [...], "radios_missing": [...]}
    error: Mapped[str | None] = mapped_column(Text)
