"""Fernzugriff: temporäre Benutzer in der Gruppe sdwan-remote (ohne policy/api), kein Ausweichen auf full."""

from __future__ import annotations

from app.config import get_settings
from app.routeros.schema import REMOTE_GROUP, REMOTE_POLICIES, policy_set
from app.routeros.simulator import get_router
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp, monkeypatch):
    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41600-41609")
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, await make_paired_device(client, h)


async def _open(client, h, dev, protocol="ssh"):
    return await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions",
                             json={"protocol": protocol, "duration_minutes": 15, "allowed_cidr": "198.51.100.7/32"}, headers=h)


async def test_temp_user_in_remote_group(client, msp, hub, monkeypatch):
    h, dev = await _setup(client, msp, monkeypatch)
    rt = get_router(dev["tunnel_ip"])
    r = await _open(client, h, dev)
    assert r.status_code == 201, r.text
    g = next(g for g in rt.tables["/user/group"] if g["name"] == REMOTE_GROUP)
    assert policy_set(g["policy"]) == set(REMOTE_POLICIES)
    assert "policy" not in policy_set(g["policy"]) and "api" not in policy_set(g["policy"])
    u = next(u for u in rt.tables["/user"] if u["name"] == r.json()["username"])
    assert u["group"] == REMOTE_GROUP
    assert not any(u.get("group") == "full" for u in rt.tables["/user"] if str(u.get("name", "")).startswith("sdwan-rs-"))
    # vorhandene Gruppe mit abweichenden Policies wird aktualisiert
    g["policy"] = "read,ssh"
    assert (await _open(client, h, dev, "winbox")).status_code == 201
    assert policy_set(g["policy"]) == set(REMOTE_POLICIES)


async def test_group_creation_denied_gives_clear_error_no_full_fallback(client, msp, hub, monkeypatch):
    h, dev = await _setup(client, msp, monkeypatch)
    rt = get_router(dev["tunnel_ip"])
    # API-Gruppe ohne winbox/web -> RouterOS (Annahme) verweigert die Gruppe sdwan-remote
    api_group = next(g for g in rt.tables["/user/group"] if g["name"] == "sdwan-api")
    api_group["policy"] = "read,write,api,policy,reboot,test,ssh,sensitive"
    r = await _open(client, h, dev)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert f"Gruppe {REMOTE_GROUP} konnte nicht angelegt werden" in detail and "not enough permissions" in detail
    assert not any(str(u.get("name", "")).startswith("sdwan-rs-") for u in rt.tables["/user"])  # kein Temp-User, kein full
    assert not any(g["name"] == REMOTE_GROUP for g in rt.tables["/user/group"])
    sessions = (await client.get(f"/api/v1/devices/{dev['id']}/remote-sessions", headers=h)).json()
    assert sessions[0]["status"] == "failed"


async def test_rights_check_can_be_disabled(client, msp, hub, monkeypatch):
    h, dev = await _setup(client, msp, monkeypatch)
    monkeypatch.setattr(get_settings(), "simulator_enforce_group_rights", False)
    rt = get_router(dev["tunnel_ip"])
    next(g for g in rt.tables["/user/group"] if g["name"] == "sdwan-api")["policy"] = "read,write,api,policy,reboot,test,ssh"
    assert (await _open(client, h, dev)).status_code == 201
