"""Audit-Behebung AP2 (Anmeldung und Geheimnisse) – zusätzliche Tests (docs/AUDIT-2026-09.md, docs/PLAN-AUDIT-FIX.md)."""

from __future__ import annotations

import pathlib
import subprocess
import uuid

import httpx
import pytest

from app.config import get_settings
from tests.conftest import login, make_paired_device, make_tenant, make_tenant_admin

ROOT = pathlib.Path(__file__).resolve().parents[3]


# ----------------------------------------------------------------------------- AUDIT-003: vertrauenswürdige Proxys
def test_003_client_from_headers_rules():
    from app.proxy import client_from_headers, parse_trusted

    t = parse_trusted("172.30.0.30, 172.30.0.1")
    # Gegenstelle nicht vertrauenswürdig → Header ignoriert
    assert client_from_headers("198.51.100.7", "1.2.3.4", t) == "198.51.100.7"
    # nginx (.30) hängt echte Adresse an; gefälschter Eintrag links zählt nicht
    assert client_from_headers("172.30.0.30", "6.6.6.6, 198.51.100.7", t) == "198.51.100.7"
    # Host-Proxy → Gateway (.1) → nginx (.30): Gateway wird übersprungen
    assert client_from_headers("172.30.0.30", "6.6.6.6, 198.51.100.7, 172.30.0.1", t) == "198.51.100.7"
    # unlesbarer Eintrag: nicht raten
    assert client_from_headers("172.30.0.30", "kaputt", t) == "172.30.0.30"
    assert parse_trusted("") == [] and parse_trusted("kein-netz") == []


async def _via(app, peer: str, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url="http://test") as c:
        return await c.get(path, headers=headers or {})


async def test_003_middleware_sets_client_and_scheme():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from app.proxy import TrustedProxyMiddleware

    async def who(request):  # noqa: ANN001
        return JSONResponse({"ip": request.client.host if request.client else None, "scheme": request.url.scheme})

    wrapped = TrustedProxyMiddleware(Starlette(routes=[Route("/who", who)]), "172.30.0.30")
    h = {"X-Forwarded-For": "6.6.6.6, 198.51.100.7", "X-Forwarded-Proto": "https"}
    assert (await _via(wrapped, "172.30.0.30", "/who", h)).json() == {"ip": "198.51.100.7", "scheme": "https"}
    assert (await _via(wrapped, "198.51.100.99", "/who", h)).json() == {"ip": "198.51.100.99", "scheme": "http"}


async def test_003_audit_ip_uses_real_client(client, msp, monkeypatch):
    """Audit-IP beim Login = echte Adresse hinter dem Proxy (nicht die gefälschte)."""
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "trusted_proxies", "172.30.0.30")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.0.30", 1)), base_url="http://test") as c:
        await c.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "falsch-123"},
                     headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
    rows = (await client.get("/api/v1/audit?action=auth.login_failed", headers=msp)).json()
    assert rows and rows[0]["ip_address"] == "198.51.100.7"


def test_003_compose_and_install_configure_trusted_proxies():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "TRUSTED_PROXIES: ${TRUSTED_PROXIES:-${SDWAN_NET:-172.30.0}.30}" in compose
    assert 'ipv4_address: "${SDWAN_NET:-172.30.0}.30"' in compose
    install = (ROOT / "deploy" / "install.sh").read_text()
    assert 'setenv TRUSTED_PROXIES "${SDWAN_NET}.30,${SDWAN_NET}.1"' in install
    assert "TRUSTED_PROXIES" in (ROOT / "deploy" / "update.sh").read_text()


# ----------------------------------------------------------------------------- AUDIT-006/023: Standard-Geheimnisse
@pytest.mark.parametrize("field,value", [("secret_key", "change-me-please-change-me-please-32b"), ("hub_token", "change-me-hub-token"),
                                         ("bootstrap_admin_password", "admin12345"), ("influx_token", "sdwan-influx-token"),
                                         ("secret_key", "kurz")])
def test_006_enforce_names_variable(monkeypatch, field, value):
    from app.secrets_check import InsecureSecretsError, enforce

    s = get_settings().model_copy()
    for k, v in {"environment": "production", "secret_key": "a" * 64, "hub_token": "b" * 32, "bootstrap_admin_password": "c" * 20,
                 "influx_token": "d" * 32, "influx_enabled": True}.items():
        setattr(s, k, v)
    enforce(s)  # sicher → kein Fehler
    setattr(s, field, value)
    with pytest.raises(InsecureSecretsError) as exc:
        enforce(s)
    assert field.upper() in str(exc.value) and "openssl rand" in str(exc.value)
    s.environment = "development"
    enforce(s)  # Entwicklung: nur Produktion wird geprüft


def test_006_worker_refuses_default_secrets(monkeypatch):
    import asyncio

    from app.secrets_check import InsecureSecretsError
    from app.worker.__main__ import main

    monkeypatch.setattr(get_settings(), "environment", "production")
    monkeypatch.setattr(get_settings(), "hub_token", "change-me-hub-token")
    with pytest.raises(InsecureSecretsError):
        asyncio.run(main())


def _check(tmp_path, env: str) -> subprocess.CompletedProcess:
    f = tmp_path / ".env"
    f.write_text(env)
    return subprocess.run([str(ROOT / "deploy" / "check-secrets.sh"), str(f)], capture_output=True, text=True, timeout=30)


def test_023_check_secrets_script(tmp_path):
    good = "\n".join(f"{k}={'x' * 40}" for k in ("SECRET_KEY", "HUB_TOKEN", "BOOTSTRAP_ADMIN_PASSWORD", "INFLUX_TOKEN",
                                                   "INFLUX_ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"))
    assert _check(tmp_path, good).returncode == 0
    for var, bad in (("GRAFANA_ADMIN_PASSWORD", "admin"), ("INFLUX_TOKEN", "sdwan-influx-token"), ("SECRET_KEY", "change-me-x" * 4)):
        r = _check(tmp_path, good + f"\n{var}={bad}\n")
        assert r.returncode == 1 and var in r.stderr and "openssl rand" in r.stderr
    r = _check(tmp_path, "\n".join(ln for ln in good.splitlines() if not ln.startswith("GRAFANA")))
    assert r.returncode == 1 and "GRAFANA_ADMIN_PASSWORD: nicht gesetzt" in r.stderr


def test_006_update_checks_before_any_restart():
    upd = (ROOT / "deploy" / "update.sh").read_text()
    assert upd.index("deploy/check-secrets.sh") < upd.index("docker compose")  # vor jedem Docker-Aufruf
    inst = (ROOT / "deploy" / "install.sh").read_text()
    assert inst.index("deploy/check-secrets.sh") < inst.index("docker compose up -d --build")


def test_006_cli_prints_encryption_key(capsys):
    from app.cli import main
    from app.security import decrypt_secret, encrypt_secret

    assert main(["encryption-key"]) == 0
    key = capsys.readouterr().out.strip()
    from cryptography.fernet import Fernet

    assert Fernet(key.encode()).decrypt(encrypt_secret("x").encode()) == b"x"
    assert decrypt_secret(Fernet(key.encode()).encrypt(b"y").decode()) == "y"


# ----------------------------------------------------------------------------- AUDIT-009: Backups für Nur-Lesen maskiert
async def test_009_diff_masked_download_forbidden_technician_sees_all(client, msp, hub):
    from app.db import system_session
    from app.models import ConfigBackup

    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    ro = await make_tenant_admin(client, msp, t["id"], email="ro@acme.example.com", role="readonly")
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    dev = await make_paired_device(client, h)
    ids = []
    async with system_session() as db:
        for pw in ("AltesGeheimnis1", "NeuesGeheimnis2"):
            b = ConfigBackup(tenant_id=uuid.UUID(t["id"]), device_id=uuid.UUID(dev["id"]), sha256=str(len(ids)) * 64, size=60,
                             trigger="manual", content=f"/interface wifi security\nadd name=x passphrase={pw}\n")
            db.add(b)
            await db.flush()
            ids.append(b.id)
        await db.commit()
    diff = (await client.get(f"/api/v1/backups/{ids[1]}/diff?against={ids[0]}", headers=ro)).json()
    assert diff["masked"] and "Geheimnis" not in str(diff["lines"])
    assert (await client.get(f"/api/v1/backups/{ids[1]}/download", headers=ro)).status_code == 403
    full = (await client.get(f"/api/v1/backups/{ids[1]}", headers=tech)).json()
    assert "NeuesGeheimnis2" in full["content"] and not full.get("masked")
    assert (await client.get(f"/api/v1/backups/{ids[1]}/download", headers=tech)).status_code == 200
    tok = (await client.post("/api/v1/auth/api-tokens", json={"name": "r", "scope": "read"}, headers=h)).json()["token"]
    body = (await client.get(f"/api/v1/backups/{ids[1]}", headers={"Authorization": f"Bearer {tok}"})).text
    assert "NeuesGeheimnis2" not in body


# ----------------------------------------------------------------------------- AUDIT-029: Token-Version
async def test_029_role_change_deactivation_and_2fa_reset_end_sessions(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    uid = (await client.get("/api/v1/auth/me", headers=tech)).json()
    uid = uid.get("id") or uid["user"]["id"]
    assert (await client.patch(f"/api/v1/users/{uid}", json={"full_name": "Nur Name"}, headers=h)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=tech)).status_code == 200  # reine Namensänderung: bleibt gültig
    assert (await client.patch(f"/api/v1/users/{uid}", json={"role": "readonly"}, headers=h)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=tech)).status_code == 401
    tech2 = await login(client, "tech@acme.example.com", "password123")
    assert (await client.get("/api/v1/auth/me", headers=tech2)).status_code == 200
    assert (await client.post(f"/api/v1/users/{uid}/2fa/reset", headers=msp)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=tech2)).status_code == 401


# ----------------------------------------------------------------------------- AUDIT-024: Timing
async def test_024_unknown_account_still_runs_bcrypt(client, monkeypatch):
    import app.api.v1.auth as auth_mod

    calls = []
    real = auth_mod.verify_password

    def spy(pw: str, hashed: str) -> bool:
        calls.append(hashed)
        return real(pw, hashed)

    monkeypatch.setattr(auth_mod, "verify_password", spy)
    r = await client.post("/api/v1/auth/login", json={"email": "gibtsnicht@example.com", "password": "egal-123"})
    assert r.status_code == 401 and calls == [auth_mod._DUMMY_HASH]
