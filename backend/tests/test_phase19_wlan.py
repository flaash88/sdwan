"""Phase 19 – WLAN: Treiber-Erkennung, Profile, Ausrollen nur für wifi, Radios unberührt, CAPsMAN, PSK-Rotation mit QR."""

from __future__ import annotations

import time

import pytest

from app.routeros.simulator import _seed_wlan, get_router
from app.services import wlan as wl
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def _wifi_router(dev: dict, driver: str | None = "wifi"):
    r = get_router(dev["tunnel_ip"])
    r.wlan_driver = driver
    for p in [p for p in r.tables if p.startswith("/interface/wifi") or p.startswith("/interface/wireless")]:
        r.tables[p] = []
    _seed_wlan(r)
    return r


def _log_calls(r):
    calls: list[str] = []
    orig = r.call

    def call(cmd, params):
        calls.append(cmd)
        return orig(cmd, params)

    r.call = call
    return calls


PROFILE = {"name": "Büro", "slug": "office", "ssid": "Beispiel-Office", "security": "wpa2-wpa3-psk", "passphrase": "geheim-12345",
           "band": "both", "channel_width": "80", "vlan_id": 20, "client_isolation": False}


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="ap1")
    return t, h, dev


def test_validation_qr_and_psk():
    with pytest.raises(wl.WlanError):
        wl.validate_profile({**PROFILE, "ssid": "x" * 33}, True, False)
    with pytest.raises(wl.WlanError):
        wl.validate_profile({**PROFILE, "security": "wpa2-eap"}, True, False)  # RADIUS fehlt
    with pytest.raises(wl.WlanError):
        wl.validate_profile({**PROFILE, "country_code": "XX"}, True, False)
    with pytest.raises(wl.WlanError):
        wl.validate_profile({**PROFILE, "psk_rotate_days": 7, "is_guest": False}, True, False)
    wl.validate_profile({**PROFILE, "schedule": {"start": "07:00", "end": "22:00"}}, True, False)
    psk = wl.generate_psk()
    assert len(psk) == 14 and psk.count("-") == 2
    assert wl.wifi_qr_payload('a;b"c', "p:w", True, "wpa2-psk") == 'WIFI:T:WPA;S:a\\;b\\"c;P:p\\:w;H:true;;'
    assert wl.qr_svg("WIFI:T:WPA;S:x;P:y;;").startswith("<svg")


async def test_detection_poll_facts_and_no_wlan(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    _wifi_router(dev, None)
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert not (d.get("facts") or {}).get("wlan")  # kein WLAN → kein Tab
    _wifi_router(dev, "wifi")
    from app.db import system_session
    from app.models import Device

    async with system_session() as db:  # Poll-Intervall zurücksetzen
        x = await db.get(Device, __import__("uuid").UUID(dev["id"]))
        x.facts = {**(x.facts or {}), "_wlan_at": time.time() - 10_000}
        await db.commit()
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    w = d["facts"]["wlan"]
    assert w["driver"] == "wifi" and [x["name"] for x in w["radios"]] == ["wifi1", "wifi2"] and w["clients"] == 2


async def test_apply_local_creates_virtual_aps_and_leaves_radios(client, msp, hub):
    t, h, dev = await _setup(client, msp)
    r = _wifi_router(dev, "wifi")
    radios_before = [dict(x) for x in r.tables["/interface/wifi"]]
    p = (await client.post("/api/v1/wlan/profiles", json=PROFILE, headers=h)).json()
    assert p["undeployed"] == [] and p["has_passphrase"] and "passphrase" not in p
    p = (await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[{"mode": "local", "device_ids": [dev["id"]]}], headers=h)).json()
    assert p["undeployed"] == ["ap1"]  # Änderungen nicht ausgerollt
    assert (await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)).status_code == 202
    sec = r.tables["/interface/wifi/security"]
    assert [(x["name"], x["authentication-types"], x["passphrase"]) for x in sec] == [("sdwan-wifi-office", "wpa2-psk,wpa3-psk", "geheim-12345")]
    (dp,) = r.tables["/interface/wifi/datapath"]
    assert dp["vlan-id"] == "20" and dp["bridge"] == "bridge" and dp["client-isolation"] == "no"
    (ch,) = r.tables["/interface/wifi/channel"]
    assert ch["width"] == "20/40/80mhz"
    (cfg,) = r.tables["/interface/wifi/configuration"]
    assert cfg["ssid"] == "Beispiel-Office" and cfg["country"] == "Austria" and cfg["channel"] == "sdwan-wifi-office"
    virt = [x for x in r.tables["/interface/wifi"] if x.get("master-interface")]
    assert sorted(x["master-interface"] for x in virt) == ["wifi1", "wifi2"]
    assert all(x["comment"].startswith("sdwan:wifi:office:") and x["configuration"] == "sdwan-wifi-office" for x in virt)
    # physische Radios unverändert
    assert [x for x in r.tables["/interface/wifi"] if not x.get("master-interface")] == radios_before
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == p["id"])
    assert p["undeployed"] == [] and p["devices"][0]["status"] == "ok"
    st = (await client.get(f"/api/v1/devices/{dev['id']}/wlan", headers=h)).json()
    assert st["live"]["channels"]["wifi2"].startswith("5180") and len(st["live"]["clients"]) == 2
    assert st["profiles"][0]["status"] == "ok" and st["profiles"][0]["applied_version"] == st["profiles"][0]["version"]
    # Ländercode des Mandanten gilt, Profil überschreibt; nur 5 GHz → nur wifi2
    body = {**PROFILE, "passphrase": None, "country_code": "DE", "band": "5ghz", "channel_width": "auto"}
    p = (await client.put(f"/api/v1/wlan/profiles/{p['id']}", json=body, headers=h)).json()
    assert p["undeployed"] == ["ap1"]
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    (cfg,) = r.tables["/interface/wifi/configuration"]
    assert cfg["country"] == "Germany" and r.tables["/interface/wifi/channel"] == []
    assert [x["master-interface"] for x in r.tables["/interface/wifi"] if x.get("master-interface")] == ["wifi2"]
    assert r.tables["/interface/wifi/security"][0]["passphrase"] == "geheim-12345"  # PSK unverändert
    # Zuweisung entfernen → beim Ausrollen vollständig entfernt, Radios bleiben
    await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[], headers=h)
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == p["id"])
    assert p["pending_removal"] == [{"device_id": dev["id"], "device": "ap1"}]
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    assert r.tables["/interface/wifi/configuration"] == [] and r.tables["/interface/wifi/security"] == []
    assert r.tables["/interface/wifi"] == radios_before
    audit = (await client.get("/api/v1/audit?action=wlan.profile.create", headers=h)).json()
    assert audit and "passphrase" not in audit[0]["details"]


async def test_foreign_wifi_objects_untouched_and_schedule(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = _wifi_router(dev, "wifi")
    r._insert("/interface/wifi/configuration", {"name": "eigene-cfg", "ssid": "Eigenes"})
    r._insert("/interface/wifi", {"name": "wifi1-own", "master-interface": "wifi1", "configuration": "eigene-cfg"})
    r._insert("/system/scheduler", {"name": "eigener", "on-event": "/log info x", "interval": "1d"})
    body = {**PROFILE, "schedule": {"start": "07:00", "end": "22:00"}}
    p = (await client.post("/api/v1/wlan/profiles", json=body, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    sched = {x["name"]: x for x in r.tables["/system/scheduler"]}
    assert sched["sdwan-wifi-office-on"]["start-time"] == "07:00:00" and sched["sdwan-wifi-office-on"]["interval"] == "1d"
    assert 'comment="sdwan:wifi:office"' in sched["sdwan-wifi-office-off"]["on-event"] and "disable" in sched["sdwan-wifi-office-off"]["on-event"]
    await client.delete(f"/api/v1/wlan/profiles/{p['id']}", headers=h)
    assert {x["name"] for x in r.tables["/system/scheduler"]} == {"eigener"}
    assert [x["name"] for x in r.tables["/interface/wifi/configuration"]] == ["eigene-cfg"]
    assert any(x["name"] == "wifi1-own" for x in r.tables["/interface/wifi"])


async def test_wireless_driver_never_gets_wifi_commands(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = _wifi_router(dev, "wireless")
    calls = _log_calls(r)
    p = (await client.post("/api/v1/wlan/profiles", json=PROFILE, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    assert calls and all(c.endswith("/print") for c in calls if "/interface/wi" in c), calls
    assert not any(c.startswith("/interface/wireless/") and not c.endswith("/print") for c in calls)
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == p["id"])
    assert p["devices"][0]["status"] == "unsupported_driver" and "wireless" in p["devices"][0]["error"]
    st = (await client.get(f"/api/v1/devices/{dev['id']}/wlan", headers=h)).json()
    assert st["live"]["driver"] == "wireless" and st["live"]["clients"][0]["signal"] == "-61dBm"


async def test_capsman_provisioning_appended(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = _wifi_router(dev, "wifi")
    p = (await client.post("/api/v1/wlan/profiles", json=PROFILE, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[{"mode": "capsman", "device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == p["id"])
    assert p["devices"][0]["status"] == "error" and "CAPsMAN" in p["devices"][0]["error"]
    r.tables["/interface/wifi/capsman"][0]["enabled"] = "yes"
    r._insert("/interface/wifi/provisioning", {"action": "create-enabled", "master-configuration": "eigene"})
    guest = (await client.post("/api/v1/wlan/profiles", json={**PROFILE, "name": "Gäste", "slug": "guest", "ssid": "Gast", "passphrase": None,
                                                              "is_guest": True, "client_isolation": True, "band": "2ghz"}, headers=h)).json()
    for x in (p, guest):
        await client.put(f"/api/v1/wlan/profiles/{x['id']}/assignments", json=[{"mode": "capsman", "device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    prov = r.tables["/interface/wifi/provisioning"]
    assert prov[0]["master-configuration"] == "eigene"  # vorhandene Regel bleibt vorne
    by = {x["comment"]: x for x in prov[1:]}
    assert by["sdwan:wifi:prov:5ghz"]["master-configuration"] == "sdwan-wifi-office"
    two = by["sdwan:wifi:prov:2ghz"]
    assert {two["master-configuration"], two.get("slave-configurations")} == {"sdwan-wifi-office", "sdwan-wifi-guest"}
    assert not [x for x in r.tables["/interface/wifi"] if x.get("master-interface")]  # keine lokalen APs im CAPsMAN-Modus


async def test_guest_psk_rotation_credentials_and_rbac(client, msp, hub):
    t, h, dev = await _setup(client, msp)
    r = _wifi_router(dev, "wifi")
    guest = (await client.post("/api/v1/wlan/profiles", json={**PROFILE, "slug": "guest", "passphrase": None, "is_guest": True}, headers=h)).json()
    assert guest["has_passphrase"]  # automatisch erzeugt
    await client.put(f"/api/v1/wlan/profiles/{guest['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{guest['id']}/apply", headers=h)
    old = r.tables["/interface/wifi/security"][0]["passphrase"]
    res = (await client.post(f"/api/v1/wlan/profiles/{guest['id']}/rotate-psk", headers=h)).json()
    assert res["passphrase"] != old and r.tables["/interface/wifi/security"][0]["passphrase"] == res["passphrase"]
    cred = (await client.get(f"/api/v1/wlan/profiles/{guest['id']}/credentials", headers=h)).json()
    assert cred["passphrase"] == res["passphrase"] and cred["qr_svg"].startswith("<svg")
    assert (await client.get("/api/v1/audit?action=wlan.credentials.view", headers=h)).json()
    # Nur-Lesen: sieht Profile, aber keine Zugangsdaten; Techniker darf nicht anlegen
    ro = await make_tenant_admin(client, msp, t["id"], email="ro@acme.example.com", role="readonly")
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    assert (await client.get("/api/v1/wlan/profiles", headers=ro)).status_code == 200
    assert (await client.get(f"/api/v1/wlan/profiles/{guest['id']}/credentials", headers=ro)).status_code == 403
    assert (await client.post("/api/v1/wlan/profiles", json={**PROFILE, "slug": "x2"}, headers=tech)).status_code == 403
    # anderer Mandant sieht nichts
    t2 = await make_tenant(client, msp, slug="other")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="admin@other.example.com")
    assert (await client.get("/api/v1/wlan/profiles", headers=h2)).json() == []
    assert (await client.get(f"/api/v1/wlan/profiles/{guest['id']}/credentials", headers=h2)).status_code == 404


async def test_tenant_country_default(client, msp, hub):
    r = await client.post("/api/v1/tenants", json={"name": "Land", "slug": "land"}, headers=msp)
    assert r.json()["country_code"] == "AT"
    r = await client.patch(f"/api/v1/tenants/{r.json()['id']}", json={"country_code": "CH"}, headers=msp)
    assert r.json()["country_code"] == "CH"
