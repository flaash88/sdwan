"""Phase 22 – Zwei-Faktor (TOTP): RFC-Vektoren, Einrichtung, Anmeldung, Wiederherstellungscodes, Pflicht, Sperre, Reset/CLI."""

from __future__ import annotations

import base64
import time

from sqlalchemy import select

from app import totp
from app.config import get_settings
from app.db import system_session
from app.models import AuditLog, User
from tests.conftest import make_tenant, make_tenant_admin


def test_rfc6238_vectors():
    # RFC 6238 Anhang B (SHA1, 8 Stellen), Schlüssel "12345678901234567890"
    key = b"12345678901234567890"
    for t, want in ((59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")):
        assert totp.hotp(key, t // 30, digits=8) == want
    secret = base64.b32encode(key).decode()
    now = 1_700_000_000
    code = totp.code_at(secret, now)
    step = totp.verify(secret, code, None, now)
    assert step == now // 30
    assert totp.verify(secret, code, step, now) is None  # Wiederverwendung abgelehnt
    assert totp.verify(secret, totp.code_at(secret, now - 30), None, now) is not None  # ±1 Schritt
    assert totp.verify(secret, totp.code_at(secret, now - 90), None, now) is None
    assert len(set(totp.generate_recovery_codes())) == 10


async def _user(client, msp, email="u@acme.example.com", role="admin"):
    t = await make_tenant(client, msp)
    await make_tenant_admin(client, msp, t["id"], email=email, role=role)
    return t


async def _enable(client, email, password="password123"):
    """Anmelden, 2FA einrichten → (Header, Secret, Wiederherstellungscodes)."""
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    setup = (await client.post("/api/v1/auth/2fa/setup", json={}, headers=h)).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and setup["qr_svg"].startswith("<svg")
    assert (await client.post("/api/v1/auth/2fa/enable", json={"code": "000000"}, headers=h)).status_code == 401
    r = await client.post("/api/v1/auth/2fa/enable", json={"code": totp.code_at(setup["secret"])}, headers=h)
    assert r.status_code == 200, r.text
    return h, setup["secret"], r.json()["recovery_codes"]


async def test_setup_login_recovery_and_disable(client, msp):
    await _user(client, msp)
    h, secret, codes = await _enable(client, "u@acme.example.com")
    assert len(codes) == 10
    async with system_session() as db:
        u = (await db.execute(select(User).where(User.email == "u@acme.example.com"))).scalar_one()
        assert u.totp_enabled and codes[0] not in str(u.recovery_codes) and secret not in (u.totp_secret_enc or "")
    # Anmeldung zweistufig
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    assert r["mfa_required"] and "access_token" not in r
    assert (await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": "123456"})).status_code == 401
    ok = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": codes[0]})
    assert ok.status_code == 200 and ok.json()["access_token"]
    # Wiederherstellungscode nur einmal
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    assert (await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": codes[0]})).status_code == 401
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["user"]["totp_enabled"] and me["recovery_codes_left"] == 9
    # mfa_token ist kein Access-Token
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r['mfa_token']}"})).status_code == 401
    # Deaktivieren nur mit gültigem Code (TOTP des nächsten Zeitschritts, da der aktuelle ggf. schon verbraucht ist)
    assert (await client.post("/api/v1/auth/2fa/disable", json={"code": "000000"}, headers=h)).status_code == 401
    assert (await client.post("/api/v1/auth/2fa/disable", json={"code": codes[1]}, headers=h)).json() == {"totp_enabled": False}
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    assert r["access_token"]


async def test_required_for_tenant_and_superuser(client, msp, monkeypatch):
    t = await _user(client, msp)
    admin = await make_tenant_admin(client, msp, t["id"], email="boss@acme.example.com")
    assert (await client.put("/api/v1/tenants/current/security", json={"require_2fa": True}, headers=admin)).json() == {"require_2fa": True}
    # bestehende Sitzungen bleiben gültig
    assert (await client.get("/api/v1/auth/me", headers=admin)).status_code == 200
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    assert r["mfa_setup_required"] and "access_token" not in r
    setup = (await client.post("/api/v1/auth/2fa/setup", json={"setup_token": r["setup_token"]})).json()
    done = (await client.post("/api/v1/auth/2fa/enable", json={"setup_token": r["setup_token"], "code": totp.code_at(setup["secret"])})).json()
    assert done["access_token"] and len(done["recovery_codes"]) == 10
    h = {"Authorization": f"Bearer {done['access_token']}"}
    # Pflicht: Deaktivieren nicht möglich
    assert (await client.post("/api/v1/auth/2fa/disable", json={"code": done["recovery_codes"][0]}, headers=h)).status_code == 409
    # MSP-Admins: Pflicht per Einstellung
    monkeypatch.setattr(get_settings(), "mfa_enforce_superuser", True)
    r = (await client.post("/api/v1/auth/login", json={"email": "msp@test.example.com", "password": "mspadmin123"})).json()
    assert r["mfa_setup_required"]


async def test_lockout_ip_limit_and_audit(client, msp, monkeypatch):
    await _user(client, msp)
    for _ in range(5):
        assert (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "falsch"})).status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})
    assert r.status_code == 429 and "gesperrt" in r.text
    async with system_session() as db:
        actions = [a.action for a in (await db.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()]
    assert actions.count("auth.login_failed") >= 5 and "auth.locked" in actions and "auth.login_blocked" in actions
    # Entsperren durch den Admin des Mandanten
    users = (await client.get("/api/v1/users", headers=msp)).json()
    uid = next(u["id"] for u in users if u["email"] == "u@acme.example.com")
    assert next(u for u in users if u["id"] == uid)["locked_until"]
    assert (await client.post(f"/api/v1/users/{uid}/unlock", headers=msp)).status_code == 200
    assert (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).status_code == 200
    # IP-Limit über Fehlversuche (unabhängig vom Konto)
    monkeypatch.setattr(get_settings(), "login_ip_limit", 3)
    for i in range(3):
        await client.post("/api/v1/auth/login", json={"email": f"nope{i}@example.com", "password": "x"})
    assert (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).status_code == 429


async def test_totp_failures_count_and_replay(client, msp):
    await _user(client, msp)
    _h, secret, _codes = await _enable(client, "u@acme.example.com")
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    # der beim Aktivieren benutzte Code ist verbraucht
    assert (await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": totp.code_at(secret)})).status_code == 401
    ok = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": totp.code_at(secret, time.time() + 30)})
    assert ok.status_code == 200
    for _ in range(5):
        await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": "000000"})
    assert (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).status_code == 429


async def test_reset_by_msp_and_cli(client, msp, monkeypatch):
    t = await _user(client, msp)
    await _enable(client, "u@acme.example.com")
    users = (await client.get("/api/v1/users", headers=msp)).json()
    uid = next(u["id"] for u in users if u["email"] == "u@acme.example.com")
    tenant_admin = await make_tenant_admin(client, msp, t["id"], email="boss@acme.example.com")
    assert (await client.post(f"/api/v1/users/{uid}/2fa/reset", headers=tenant_admin)).status_code == 403
    sent = []

    async def fake_send(url, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(get_settings(), "platform_webhook_url", "https://hooks.example.com/x")
    monkeypatch.setattr("app.services.webhook.validate_url", lambda u: u)
    monkeypatch.setattr("app.services.webhook.send", fake_send)
    r = await client.post(f"/api/v1/users/{uid}/2fa/reset", headers=msp)
    assert r.status_code == 200 and r.json()["totp_enabled"] is False and sent
    audit = (await client.get("/api/v1/audit?action=auth.2fa_reset", headers=msp)).json()
    assert audit and audit[0]["details"]["via"] == "ui"
    # CLI
    await _enable(client, "u@acme.example.com")
    from app.cli import _run

    assert await _run("reset-2fa", "u@acme.example.com") == 0
    assert await _run("unlock", "u@acme.example.com") == 0
    assert await _run("unlock", "gibtsnicht@example.com") == 1
    async with system_session() as db:
        u = (await db.execute(select(User).where(User.email == "u@acme.example.com"))).scalar_one()
        assert not u.totp_enabled
        vias = [a.details.get("via") for a in (await db.execute(select(AuditLog).where(AuditLog.action.in_(("auth.2fa_reset", "auth.unlock"))))).scalars()]
    assert vias.count("cli") == 2
