"""Eigene Gruppe für den API-Benutzer: Onboarding/ZTP, Selbsttest-Hinweis, „Rechte einschränken“ mit Totmannschaltung."""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.services.wireguard import generate_keypair
from app.routeros.schema import API_GROUP, API_POLICIES, policy_set
from app.routeros.simulator import get_router
from app.routeros import RouterOSError
from app.services.api_rights import REVERT_SCHEDULER, revert_start
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
    assert c["status"] == "warn" and set(c["missing"]) == {"winbox", "web"}
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
    rt.clock_local = dt.datetime(2026, 9, 25, 14, 0, 30)
    body = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert body["status"] == "reverting" and body["revert_at"] == {"start-date": "2026-09-25", "start-time": "14:03:30"}
    assert _user(rt)["group"] == API_GROUP
    (sch,) = [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]
    assert (sch["start-date"], sch["start-time"], sch["interval"]) == ("2026-09-25", "14:03:30", "1m")
    # erst Gruppe zurückstellen, dann Scheduler entfernen
    ev = sch["on-event"]
    assert 'group="admins-alt"' in ev and ev.index("/user set") < ev.index("/system scheduler remove")
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


@pytest.mark.parametrize(("date", "time", "exp_date", "exp_time"), [
    ("2026-09-25", "10:00:00", "2026-09-25", "10:03:00"),          # ISO (ab 7.10)
    ("sep/25/2026", "10:00:00", "sep/25/2026", "10:03:00"),        # alt (bis 7.9), gleiches Format zurück
    ("2026-09-25", "23:58:30", "2026-09-26", "00:01:30"),          # Tageswechsel
    ("jan/31/2026", "23:59:00", "feb/01/2026", "00:02:00"),        # Monatswechsel, altes Format
    ("2026-12-31", "23:57:00", "2027-01-01", "00:00:00"),          # Jahreswechsel
    ("feb/28/2028", "23:59:59", "feb/29/2028", "00:02:59"),        # Schaltjahr
])
def test_revert_start_date_math(date, time, exp_date, exp_time):
    assert revert_start({"date": date, "time": time}) == {"start-date": exp_date, "start-time": exp_time}


def test_revert_start_unreadable_clock():
    with pytest.raises(RouterOSError):
        revert_start({"date": "gestern", "time": "12:00:00"})


async def test_restrict_scheduler_over_midnight_legacy_format(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _legacy(rt)
    rt.clock_local, rt.clock_format = dt.datetime(2026, 1, 31, 23, 58, 0), "legacy"
    rt.fail_next.add("/system/resource/print")  # Scheduler bleibt stehen -> Felder prüfen
    body = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert body["status"] == "reverting"
    (sch,) = [s for s in rt.tables["/system/scheduler"] if s["name"] == REVERT_SCHEDULER]
    assert (sch["start-date"], sch["start-time"], sch["interval"]) == ("feb/01/2026", "00:01:00", "1m")
    assert sch["policy"] == "read,write,policy,test"


async def test_restrict_unreadable_clock_changes_nothing(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _legacy(rt)
    rt.clock_format = "kaputt"
    rt._clock = lambda p: [{"date": "", "time": ""}]
    r = await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)
    assert r.status_code == 502 and "Uhrzeit" in r.text
    assert _user(rt)["group"] == "full" and not rt.tables["/system/scheduler"]
