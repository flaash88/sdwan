"""Eigene Gruppe für den API-Benutzer: Onboarding/ZTP, Selbsttest-Hinweis, „Rechte einschränken“ mit Totmannschaltung."""

from __future__ import annotations

from app.config import get_settings
from app.services.wireguard import generate_keypair
from app.routeros.schema import API_GROUP, API_POLICIES, policy_set
from app.routeros.simulator import get_router
from app.services.api_rights import REVERT_SCHEDULER
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def _user(rt):
    return next(u for u in rt.tables["/user"] if u["name"] == get_settings().routeros_api_user)


def _legacy(rt):
    """Altgerät: API-Benutzer in 'full', Gruppe sdwan-api existiert noch nicht."""
    _user(rt)["group"] = "full"
    rt.tables["/user/group"] = [g for g in rt.tables["/user/group"] if g["name"] != API_GROUP]


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, await make_paired_device(client, h, name="alt")


async def _rights(client, h, dev):
    t = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    return next(c for c in t["checks"] if c["key"] == "rights")


async def test_pair_script_uses_own_group_not_full(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    token = (await client.post("/api/v1/devices", json={"name": "neu"}, headers=h)).json()["pairing"]["token"]
    _, pub = generate_keypair()
    resp = (await client.post("/api/v1/pair", json={"token": token, "public_key": pub, "serial": "X1"})).text
    pol = ",".join(API_POLICIES)
    assert f'/user group add name="{API_GROUP}" policy={pol}' in resp
    assert f'/user group set [find name="{API_GROUP}"] policy={pol}' in resp  # vorhandene Gruppe nur aktualisieren
    assert f'group="{API_GROUP}"' in resp and "group=full" not in resp and 'group="full"' not in resp


async def test_ztp_pairing_uses_same_group(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tpl = (await client.post("/api/v1/ztp/templates", json={"name": "T", "config": {}}, headers=h)).json()
    staged = (await client.post("/api/v1/ztp/stage", json={"template_id": tpl["id"], "devices": [{"name": "z1", "serial": "ZZ1"}]}, headers=h)).json()
    _, pub = generate_keypair()
    resp = (await client.post("/api/v1/pair", json={"token": staged[0]["token"], "public_key": pub, "serial": "ZZ1"})).text
    assert ":error" not in resp and f'group="{API_GROUP}"' in resp and "group=full" not in resp


async def test_selftest_rights_hints(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    c = await _rights(client, h, dev)
    assert c["status"] == "ok" and c["group"] == API_GROUP
    _legacy(rt)
    c = await _rights(client, h, dev)
    assert c["status"] == "warn" and c["action"] == "restrict_api_user"
    assert "API-Benutzer in Gruppe full – Umstellung empfohlen" in c["notes"][0]
    # eigene Gruppe ohne winbox/web -> orange mit Hinweis Fernzugriff
    _user(rt)["group"] = "eigen"
    rt._insert("/user/group", {"name": "eigen", "policy": "api,read,write,policy,reboot,test,ssh,sensitive"})
    c = await _rights(client, h, dev)
    assert c["status"] == "warn" and set(c["missing"]) == {"winbox", "web", "local"}
    assert "Fernzugriff funktioniert ohne diese Policies nicht" in c["notes"][0]
    # Kern-Policy fehlt -> rot
    next(g for g in rt.tables["/user/group"] if g["name"] == "eigen")["policy"] = "api,read,write,policy,test,ssh"
    assert (await _rights(client, h, dev))["status"] == "error"


async def test_restrict_success_removes_scheduler(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _legacy(rt)
    r = await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["previous_group"] == "full"
    assert _user(rt)["group"] == API_GROUP
    g = next(g for g in rt.tables["/user/group"] if g["name"] == API_GROUP)
    assert policy_set(g["policy"]) == set(API_POLICIES)
    assert not [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]
    rights = next(c for c in body["selftest"]["checks"] if c["key"] == "rights")
    assert rights["status"] == "ok"
    # gespeicherter Selbsttest = Ergebnis nach der Umstellung
    assert (await client.get(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()["ran_at"] == body["selftest"]["ran_at"]
    audit = (await client.get("/api/v1/audit?action=device.restrict_api_user", headers=h)).json()
    assert audit and audit[0]["success"] is True
    # erneuter Aufruf: nichts zu tun
    assert (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()["status"] == "unchanged"


async def test_restrict_failure_keeps_scheduler_which_reverts(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _legacy(rt)
    _user(rt)["group"] = "admins-alt"  # vorherige Gruppe wird gelesen, nicht fest 'full'
    rt._insert("/user/group", {"name": "admins-alt", "policy": "local,ssh,read,write,policy,test,winbox,web,reboot,sensitive,api"})
    rt.fail_next.add("/system/resource/print")  # Selbsttest nach der Umstellung schlägt rot fehl
    body = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert body["status"] == "reverting" and body["revert_after"] == "3m"
    assert _user(rt)["group"] == API_GROUP
    (sch,) = [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]
    assert sch["interval"] == "3m" and 'group="admins-alt"' in sch["on-event"]
    rt.fire_scheduler(REVERT_SCHEDULER)  # Router führt die Totmannschaltung aus
    assert _user(rt)["group"] == "admins-alt"
    assert not [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]
    audit = (await client.get("/api/v1/audit?action=device.restrict_api_user", headers=h)).json()
    assert audit[0]["success"] is False


async def test_restrict_readback_mismatch_does_not_switch(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _legacy(rt)
    rt.drop_policies = {"sensitive"}
    body = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert body["status"] == "readback_mismatch"
    assert _user(rt)["group"] == "full"
    assert not [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]


async def test_restrict_rbac(client, msp, hub):
    h, dev = await _setup(client, msp)
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@acme.example.com", role="readonly")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=ro)).status_code == 403
