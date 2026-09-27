"""Offboarding: Router bereinigen (feste Reihenfolge) oder nur aus der Plattform entfernen; Archiv, Rechte, Abbruch."""

from __future__ import annotations

from app.config import get_settings
from app.routeros.simulator import _seed_wlan, get_router
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin
from tests.test_phase14_defconf import DEFCONF, _factory, _state

WAN = [{"name": "Glasfaser", "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"},
       {"name": "LTE", "interface": "ether8", "gateway": "198.51.100.1", "priority": 2, "check_target": "9.9.9.9"}]
VRRP = {"name": "vrrp-lan", "interface": "ether2", "vrid": 10, "priority": 100, "vip": "192.0.2.254", "local_address": "192.0.2.21/24"}


async def _managed_device(client, msp, monkeypatch):
    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41800-41819")
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="off1")
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    rt.wlan_driver = "wifi"
    _seed_wlan(rt)
    # WAN, VRRP, Zonen + einfache Policy mit Default-Drop (defconf wird deaktiviert), Syslog, WLAN, Hotspot
    assert (await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": WAN}, headers=h)).json()["push"]["ok"]
    assert (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).status_code == 200
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]},
                                                                           {"interface": "bridge", "zone_id": z["lan"]}]}, headers=h)
    pol = (await client.post("/api/v1/policies", json={"name": "FW", "mode": "simple", "spec": {"rules": [
        {"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]}}, headers=h)).json()
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    assert (await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)).status_code == 202
    assert set(_state(rt).values()) == {"yes"}
    await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": True, "topics": ["system"]}, headers=h)
    prof = (await client.post("/api/v1/wlan/profiles", json={"name": "Büro", "slug": "office", "ssid": "Beispiel", "security": "wpa2-psk",
                                                             "passphrase": "geheim-12345"}, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{prof['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{prof['id']}/apply", headers=h)
    portal = next(p for p in (await client.get("/api/v1/hotspot/portals", headers=h)).json() if p["name"] == "Gastronomie")
    inst = (await client.post("/api/v1/hotspot/instances", json={"name": "G", "slug": "g", "device_id": dev["id"], "portal_id": portal["id"],
                                                                 "interface": "bridge", "walled_garden": ["example.com"]}, headers=h)).json()
    assert (await client.post(f"/api/v1/hotspot/instances/{inst['id']}/apply", headers=h)).status_code == 200
    # Fernzugriff ändert www (vorher deaktiviert) – Sitzung bleibt offen
    www = next(s for s in rt.tables["/ip/service"] if s["name"] == "www")
    www["disabled"] = "true"
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "webfig", "duration_minutes": 15,
                                                                                 "allowed_cidr": "198.51.100.7/32"}, headers=h)
    assert r.status_code == 201, r.text
    assert www["disabled"] == "no"
    return t, h, dev, rt


def _leftovers(rt) -> list[tuple[str, str]]:
    out = []
    for path, rows in rt.tables.items():
        for r in rows:
            if str(r.get("comment", "")).startswith("sdwan:"):
                out.append((path, str(r.get("comment"))))
            # /interface: Simulator-Spiegel der Interfaces (RouterOS entfernt sie mit dem eigentlichen Interface)
            elif path != "/interface" and str(r.get("name", "")).startswith("sdwan-"):
                out.append((path, str(r.get("name"))))
    return out


async def test_clean_offboarding_removes_everything_and_keeps_defconf(client, msp, hub, monkeypatch):
    _t, h, dev, rt = await _managed_device(client, msp, monkeypatch)
    assert len(_leftovers(rt)) > 20
    pre = (await client.get(f"/api/v1/devices/{dev['id']}/offboarding/preview", headers=h)).json()
    assert pre["online"] and pre["defconf_disabled"] == len(DEFCONF) and pre["counts"]["Firewall-Filter"] > 0
    assert pre["final"]["WAN-Routen"] == 2 and not any("Default-Route" in w for w in pre["warnings"])  # eigene Default-Route vorhanden
    own = [x for x in rt.tables["/ip/route"] if x.get("dst-address") == "0.0.0.0/0" and not str(x.get("comment", "")).startswith("sdwan:")]
    rt.tables["/ip/route"] = [x for x in rt.tables["/ip/route"] if x not in own]
    pre2 = (await client.get(f"/api/v1/devices/{dev['id']}/offboarding/preview", headers=h)).json()
    assert any("Default-Route" in w for w in pre2["warnings"])
    rt.tables["/ip/route"] += own
    assert (await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "falsch"}, headers=h)).status_code == 422
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"mode": "clean", "confirm_name": "off1"}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    assert [s["step"] for s in res["steps"]] == [1, 2, 3, 4, 5, 6] and all(s["ok"] for s in res["steps"])
    assert len(res["steps"][1]["detail"]["enabled"]) == len(DEFCONF)
    assert res["steps"][3]["detail"] == [{"service": "www", "disabled": "true", "address": ""}]
    # Vor dem Scheduler: Zugang (Tunnel, API-Benutzer, WAN) steht noch, defconf ist aktiv
    assert set(_state(rt).values()) == {"no"}
    assert any(u.get("comment") == "sdwan:mgmt" for u in rt.tables["/user"])
    assert next(s for s in rt.tables["/ip/service"] if s["name"] == "www")["disabled"] == "true"
    assert not [u for u in rt.tables["/user"] if str(u.get("comment", "")).startswith("sdwan:remote")]
    assert not [g for g in rt.tables["/user/group"] if g.get("name") == "sdwan-remote"]
    # Schritt 6 läuft auf dem Router
    rt.fire_scheduler("sdwan-offboard")
    assert _leftovers(rt) == []
    assert set(_state(rt).values()) == {"no"} and len(_state(rt)) == len(DEFCONF)
    assert not [g for g in rt.tables["/user/group"] if g.get("name") == "sdwan-api"]
    assert [x["name"] for x in rt.tables["/interface/wifi"] if not x.get("master-interface")] == ["wifi1", "wifi2"]  # Radios bleiben
    # Gerät weg, Archiv mit Backup beim Mandanten
    assert (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).status_code == 404
    (arch,) = (await client.get("/api/v1/offboarding-archives", headers=h)).json()
    assert arch["device_name"] == "off1" and arch["mode"] == "clean" and arch["has_backup"]
    assert arch["steps"][0]["label"].startswith("Backup")
    dl = await client.get(f"/api/v1/offboarding-archives/{arch['id']}/download", headers=h)
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    audit = (await client.get("/api/v1/audit?action=device.offboard", headers=h)).json()
    assert audit and audit[0]["details"]["mode"] == "clean"


async def test_failure_before_step6_aborts_and_keeps_device(client, msp, hub, monkeypatch):
    _t, h, dev, rt = await _managed_device(client, msp, monkeypatch)
    rt.fail_next.add("/ip/firewall/filter/remove")
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "off1"}, headers=h)
    assert r.status_code == 409 and "abgebrochen" in r.text
    steps = r.json()["detail"]["steps"]
    assert [s["step"] for s in steps] == [1, 2, 3] and steps[-1]["ok"] is False
    assert (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).status_code == 200
    assert set(_state(rt).values()) == {"no"}  # defconf wurde zuerst wieder aktiviert
    assert not [s for s in rt.tables["/system/scheduler"] if s.get("name") == "sdwan-offboard"]  # Zugang bleibt
    assert any(u.get("comment") == "sdwan:mgmt" for u in rt.tables["/user"])
    assert (await client.get("/api/v1/offboarding-archives", headers=h)).json() == []


async def test_platform_only_leaves_router_unchanged(client, msp, hub, monkeypatch):
    _t, h, dev, rt = await _managed_device(client, msp, monkeypatch)
    await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h)
    before = {p: [dict(r) for r in rows] for p, rows in rt.tables.items() if p != "/interface"}
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"mode": "platform_only", "confirm_name": "off1"}, headers=h)
    assert r.status_code == 200, r.text
    assert {p: [dict(r) for r in rows] for p, rows in rt.tables.items() if p != "/interface"} == before
    (arch,) = (await client.get("/api/v1/offboarding-archives", headers=h)).json()
    assert arch["mode"] == "platform_only" and arch["has_backup"]


async def test_rights_and_offline(client, msp, hub, monkeypatch):
    t, h, dev, rt = await _managed_device(client, msp, monkeypatch)
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "off1"}, headers=tech)).status_code == 403
    rt.offline = True
    from app.services.poller import poll_all

    await poll_all()
    await poll_all()
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"mode": "clean", "confirm_name": "off1"}, headers=h)
    assert r.status_code == 409 and "erreichbar" in r.text
    assert not (await client.get(f"/api/v1/devices/{dev['id']}/offboarding/preview", headers=h)).json()["online"]
