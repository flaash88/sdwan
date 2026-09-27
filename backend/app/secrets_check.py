"""Start-Prüfung auf Standard-Geheimnisse (AUDIT-006/023).

In Produktion (``ENVIRONMENT=production``) startet die Plattform nicht, solange ``SECRET_KEY``, ``HUB_TOKEN``,
``BOOTSTRAP_ADMIN_PASSWORD`` oder – bei aktivem InfluxDB – ``INFLUX_TOKEN`` Standardwerte bzw. Platzhalter haben oder zu kurz
sind. Die Meldung nennt die Variable und den Befehl zum Erzeugen eines Werts. Grafana-/Influx-Admin-Passwörter liest die
App nicht; die prüft ``deploy/check-secrets.sh`` (update.sh/install.sh) in der ``.env``.
"""

from __future__ import annotations

from app.config import Settings

DEFAULTS: dict[str, tuple[str, ...]] = {
    "SECRET_KEY": ("change-me-please-change-me-please-32b",),
    "HUB_TOKEN": ("change-me-hub-token",),
    "BOOTSTRAP_ADMIN_PASSWORD": ("admin12345",),
    "INFLUX_TOKEN": ("sdwan-influx-token",),
}
MIN_LEN = {"SECRET_KEY": 32, "HUB_TOKEN": 16, "BOOTSTRAP_ADMIN_PASSWORD": 10, "INFLUX_TOKEN": 16}
HOW = "Neuen Wert erzeugen: openssl rand -hex 32 – in .env eintragen, dann deploy/update.sh"


class InsecureSecretsError(RuntimeError):
    pass


def problems(s: Settings) -> list[str]:
    values = {"SECRET_KEY": s.secret_key, "HUB_TOKEN": s.hub_token, "BOOTSTRAP_ADMIN_PASSWORD": s.bootstrap_admin_password}
    if s.influx_enabled:
        values["INFLUX_TOKEN"] = s.influx_token
    out = []
    for name, v in values.items():
        v = v or ""
        if v in DEFAULTS[name] or v.startswith("change-me"):
            out.append(f"{name}: Standardwert/Platzhalter")
        elif len(v) < MIN_LEN[name]:
            out.append(f"{name}: zu kurz ({len(v)} Zeichen, mindestens {MIN_LEN[name]})")
    return out


def enforce(s: Settings) -> None:
    """Bricht in Produktion mit :class:`InsecureSecretsError` ab."""
    if s.environment != "production":
        return
    found = problems(s)
    if found:
        raise InsecureSecretsError("Start verweigert – unsichere Geheimnisse: " + "; ".join(found) + f". {HOW}")
