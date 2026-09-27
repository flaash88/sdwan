"""Sicherheitsmeldungen (Phase 23): global, vom MSP gepflegt (Seed nur als deaktiviertes Beispiel)."""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin


class SecurityAdvisory(IdMixin, Base):
    __tablename__ = "security_advisories"

    cve: Mapped[str] = mapped_column(String(40), index=True)  # CVE-ID oder eigene Kennung
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # general | hotspot | wlan | vrrp | wireguard | dns | rest-api | api | winbox | www | ssh | other
    function: Mapped[str] = mapped_column(String(20), default="general")
    severity: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high | critical
    affected_from: Mapped[str] = mapped_column(String(20))  # erste betroffene Version (inklusive)
    affected_to: Mapped[str | None] = mapped_column(String(20))  # letzte betroffene Version (inklusive), optional
    fixed_in: Mapped[str | None] = mapped_column(String(20))  # erste behobene Version (exklusiv), optional
    link: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(100), index=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
