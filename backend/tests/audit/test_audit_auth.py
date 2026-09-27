"""Audit B.3–B.5 – Gegenproben Anmeldung, 2FA, Tokens, interne Endpunkte (bestätigen korrektes Verhalten).

Diese Tests sind grün; sie sichern die geprüften Eigenschaften gegen Rückschritte ab. Abweichungen stehen als
xfail-Tests in ``test_audit_findings.py``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json

import jwt
import pytest

from app import totp
from app.config import get_settings
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

VALID_WG = "A" * 43 + "="


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


async def _me_status(client, token: str) -> int:
    return (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code


async def test_jwt_none_tampered_expired_and_wrong_type_rejected(client, msp):
    me = (await client.get("/api/v1/auth/me", headers=msp)).json()
    uid = me.get("id") or me["user"]["id"]
    now = int(dt.datetime.now(dt.UTC).timestamp())
    none_tok = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': uid, 'typ': 'access', 'exp': now + 600})}."
    assert await _me_status(client, none_tok) == 401
    wrong_key = jwt.encode({"sub": uid, "typ": "access", "exp": now + 600}, "falscher-schluessel", algorithm="HS256")
    assert await _me_status(client, wrong_key) == 401
    s = get_settings()
    expired = jwt.encode({"sub": uid, "typ": "access", "exp": now - 5}, s.secret_key, algorithm=s.jwt_algorithm)
    assert await _me_status(client, expired) == 401
    for typ in ("mfa", "mfa_setup"):  # Schritt-Token der 2FA gelten nicht als Anmeldung
        step = jwt.encode({"sub": uid, "typ": typ, "exp": now + 300}, s.secret_key, algorithm=s.jwt_algorithm)
        assert await _me_status(client, step) == 401
    hs512 = jwt.encode({"sub": uid, "typ": "access", "exp": now + 600}, s.secret_key, algorithm="HS512")
    assert await _me_status(client, hs512) == 401  # Algorithmus fest (kein alg-Wechsel)


async def test_2fa_cannot_be_skipped_and_codes_single_use(client, msp):
    t = await make_tenant(client, msp)
    await make_tenant_admin(client, msp, t["id"], email="u@acme.example.com")
    r = await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    setup = (await client.post("/api/v1/auth/2fa/setup", json={}, headers=h)).json()
    used = totp.code_at(setup["secret"])
    codes = (await client.post("/api/v1/auth/2fa/enable", json={"code": used}, headers=h)).json()["recovery_codes"]
    r = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    assert "access_token" not in r or not r.get("access_token")
    assert r.get("mfa_required") and r.get("mfa_token")
    assert await _me_status(client, r["mfa_token"]) == 401  # mfa_token ist keine Anmeldung
    ok = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r["mfa_token"], "code": codes[0]})
    assert ok.status_code == 200 and ok.json()["access_token"]
    r2 = (await client.post("/api/v1/auth/login", json={"email": "u@acme.example.com", "password": "password123"})).json()
    again = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r2["mfa_token"], "code": codes[0]})
    assert again.status_code == 401  # Wiederherstellungscode nur einmal
    # der beim Einschalten verwendete TOTP-Code ist verbraucht → Replay abgelehnt (exakt derselbe Code, kein Neuberechnen,
    # sonst wäre er nach einem 30-s-Wechsel ein neuer, gültiger Code)
    replay = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r2["mfa_token"], "code": used})
    assert replay.status_code == 401


async def test_internal_endpoints_require_hub_token(client):
    for h in ({}, {"X-Hub-Token": "falsch"}, {"X-Hub-Token": ""}):
        assert (await client.get("/api/v1/internal/hub/peers", headers=h)).status_code == 401
        assert (await client.post("/api/v1/internal/hub/stats", json=[], headers=h)).status_code == 401


async def test_pairing_token_single_use_and_unknown_token(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    r = await client.post("/api/v1/devices", json={"name": "p1"}, headers=h)
    token = r.json()["pairing"]["token"]
    ok = await client.post("/api/v1/pair", json={"token": token, "public_key": VALID_WG, "serial": "S1"})
    assert not ok.text.startswith(":error")
    again = await client.post("/api/v1/pair", json={"token": token, "public_key": "B" * 43 + "=", "serial": "S1"})
    assert again.text.startswith(":error")  # Einmal-Token
    assert (await client.get(f"/api/v1/onboard/{token}.rsc")).text.startswith(":error")
    assert (await client.get("/api/v1/onboard/erfunden.rsc")).text.startswith(":error")


@pytest.mark.parametrize("scope", ["read", "role"])
async def test_api_token_limits(client, msp, hub, scope):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    tok = (await client.post("/api/v1/auth/api-tokens", json={"name": "x", "scope": scope}, headers=h)).json()
    th = {"Authorization": f"Bearer {tok['token']}"}
    assert (await client.get("/api/v1/devices", headers=th)).status_code == 200
    w = await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=th)
    assert (w.status_code == 403) == (scope == "read")
    for method, path, body in (("post", "/api/v1/auth/api-tokens", {"name": "y"}), ("post", "/api/v1/auth/2fa/setup", {}),
                               ("post", "/api/v1/local-access/export", {"format": "zip", "password": "x" * 16})):
        assert (await getattr(client, method)(path, json=body, headers=th)).status_code in (403,)
    await client.delete(f"/api/v1/auth/api-tokens/{tok['id']}", headers=h)
    assert (await client.get("/api/v1/devices", headers=th)).status_code == 401
    assert (await client.get("/api/v1/devices", headers={"Authorization": "Bearer sdw_erfunden"})).status_code == 401
