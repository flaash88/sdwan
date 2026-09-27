"""Zentrale Konfiguration (12-Factor: alles über Umgebungsvariablen)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Allgemein ---------------------------------------------------------
    # Branding (UI, Mails, PDF-Berichte). Technische Bezeichner ("sdwan:"-Kommentare, Interfaces,
    # DB, Docker-Dienste) bleiben bewusst unverändert, sonst verwaisen bestehende Router-Konfigurationen.
    product_name: str = "MikroTik-Fleet-Management"
    product_short: str = "MFM"  # Betreffzeilen und knappe Stellen
    environment: str = "development"
    # Öffentlich erreichbare Basis-URL der Control-Plane (für Onboarding-Scripts)
    public_url: str = "http://localhost:8000"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"])

    # --- Sicherheit ----------------------------------------------------------
    secret_key: str = "change-me-please-change-me-please-32b"
    # Fernet-Key für verschlüsselte Secrets in der DB (Device-API-Passwörter, PSKs, API-Keys).
    # Leer -> wird deterministisch aus secret_key abgeleitet.
    encryption_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    # Phase 22: 2FA für MSP-Admins immer Pflicht (Einrichtung bei der nächsten Anmeldung erzwungen)
    mfa_enforce_superuser: bool = True
    login_max_failures: int = 5  # je Konto (Passwort + 2FA), danach Sperre
    login_lock_minutes: int = 15
    login_ip_limit: int = 30  # Anmeldeversuche je IP im Fenster
    login_ip_window_s: int = 600
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "admin12345"
    # Shared Token zwischen Hub-Agent und Control-Plane (/internal/hub/*)
    hub_token: str = "change-me-hub-token"

    # --- Datenbanken ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://sdwan:sdwan@localhost:5432/sdwan"
    # Beim Start Tabellen anlegen (Dev/Test). In Produktion: alembic upgrade head.
    db_auto_create: bool = False
    redis_url: str = "redis://localhost:6379/0"
    # Ohne Redis (Tests/Single-Process) läuft der Event-Bus in-memory
    use_redis: bool = True

    influx_url: str = "http://localhost:8086"
    influx_token: str = "sdwan-influx-token"
    influx_org: str = "sdwan"
    influx_bucket: str = "metrics"
    influx_enabled: bool = True

    grafana_public_url: str = "http://localhost:3001"

    # --- WireGuard-Hub -------------------------------------------------------
    wg_network: str = "10.100.0.0/16"  # Management-Netz (Hub <-> Router)
    wg_hub_endpoint: str = "localhost"  # öffentlicher Hostname/IP des Hubs
    wg_hub_port: int = 51820
    wg_device_interface: str = "sdwan-mgmt"
    wg_device_listen_port: int = 13231
    # Transfer-Netz für das Site-to-Site-Mesh (pro Tenant ein /24 daraus)
    mesh_network: str = "10.200.0.0/16"
    mesh_listen_port: int = 13232
    mesh_interface: str = "sdwan-mesh"

    # --- RouterOS ------------------------------------------------------------
    routeros_api_port: int = 8728
    routeros_api_user: str = "sdwan"
    routeros_timeout: float = 10.0
    # "simulator" -> kein echter Router nötig (Demo/Tests); "api" -> librouteros über den Tunnel
    routeros_backend: str = "api"
    # Nur Simulator: Gruppen mit Policies, die der API-Benutzer selbst nicht hat, werden abgelehnt.
    # ANNAHME über das RouterOS-Verhalten, im Labor zu verifizieren (docs/LABORTEST.md, Fernzugriff).
    simulator_enforce_group_rights: bool = True

    # --- Worker / Jobs -------------------------------------------------------
    poll_interval_seconds: int = 60
    # Threat-Feeds (Phase 15): RAM-Reserve je Gerät, geschätzter Speicher je Eintrag, maximale Downloadgröße
    feed_min_free_mb: int = 32
    feed_bytes_per_entry: int = 200
    feed_max_download_mb: int = 10
    # Speedtest (Phase 18): btest-Server (RouterOS/CHR) – ohne Angabe ist der Speedtest deaktiviert
    speedtest_server: str = ""
    speedtest_user: str = ""
    speedtest_password: str = ""
    speedtest_duration_s: int = 10
    # Syslog (Phase 18): UDP-Port des Empfängers auf der Hub-Tunnel-IP
    syslog_port: int = 514
    # Phase 25: IPFIX-Collector (Top-Verbraucher), UDP auf der Hub-Tunnel-IP
    flow_port: int = 2055
    offline_after_seconds: int = 180
    pairing_token_ttl_hours: int = 72

    # --- Backups & Firmware (Phase 9) -----------------------------------------
    backup_hour_utc: int = 2
    backup_retention: int = 90  # automatische Backups pro Gerät (manuelle/gepinnte bleiben)
    ssh_port: int = 22
    firmware_reboot_timeout_s: int = 900

    # --- Plattform-Sicherung (Phase 21) ---------------------------------------
    platform_backup_hour_utc: int = 3
    platform_backup_dir: str = "/backups"
    platform_backup_keep_days: int = 14
    # age-Public-Key (age1…); der Private Key gehört NICHT auf den Server. Leer = keine Sicherung
    platform_backup_age_recipient: str = ""
    # optionales rclone-Ziel (S3-kompatibel oder SFTP), z. B. "offsite:sdwan-backups"; leer = nur lokal
    platform_backup_rclone_remote: str = ""
    platform_backup_influx: bool = False
    platform_backup_env_file: str = "/platform/.env"
    platform_backup_hub_dir: str = "/platform/hub"
    # Plattform-Benachrichtigungen (Sicherung, 2FA-Reset …) an MSP-Systeme; leer = aus
    platform_webhook_url: str = ""

    # --- SMTP (Alerts) -------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "sdwan@example.com"
    smtp_starttls: bool = True
    mail_accent_color: str = "#0f766e"  # Hex, Buttons/Akzente im HTML-Mail-Layout
    mail_logo_url: str = ""  # optional, https-URL eines Logos (max. ~180 px breit)
    mail_footer_text: str = ""  # z. B. "Netzwarte GmbH · Support: +43 … · support@…"
    mail_subject_emoji: bool = True  # 🔴/🟠/🔵/✅ im Betreff

    # --- NextDNS -------------------------------------------------------------
    nextdns_api_key: str = ""
    nextdns_api_url: str = "https://api.nextdns.io"

    # --- Remote Access -------------------------------------------------------
    remote_proxy_host: str = "localhost"  # Hostname, unter dem der Proxy für Techniker erreichbar ist
    remote_proxy_port_range: str = "40000-40099"
    remote_session_max_minutes: int = 240
    # Nur Tests/Simulator: Proxy-Ziel statt Tunnel-IP (z. B. 127.0.0.1)
    remote_proxy_target_override: str = ""

    @property
    def app_name(self) -> str:
        return self.product_name

    @property
    def wg_net(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(self.wg_network)

    @property
    def wg_hub_ip(self) -> str:
        return str(next(self.wg_net.hosts()))

    @property
    def remote_ports(self) -> tuple[int, int]:
        lo, hi = self.remote_proxy_port_range.split("-")
        return int(lo), int(hi)


@lru_cache
def get_settings() -> Settings:
    return Settings()
