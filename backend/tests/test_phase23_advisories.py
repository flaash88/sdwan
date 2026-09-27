"""Phase 23 – Sicherheitsmeldungen: Versionen, Betroffenheit je Funktion, Alarm, Blockade, Compliance, Rechte."""

from __future__ import annotations

import pytest

from app.models import SecurityAdvisory
from app.routeros.simulator import _seed_wlan, get_router
from app.services import advisories as adv
from app.services.alerts import evaluate_all
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def _a(**kw) -> SecurityAdvisory:
    base = {"cve": "X-1", "title": "t", "function": "general", "severity": "high", "affected_from": "7.10", "affected_to": None,
            "fixed_in": "7.16", "enabled": True}
    return SecurityAdvisory(**{**base, **kw})


def test_versions_and_ranges():
    assert adv.parse_version("7.15.3") == (7, 15, 3, 2, 0)
    assert adv.parse_version("7.16rc2") < adv.parse_version("7.16") < adv.parse_version("7.16.1")
    assert adv.parse_version("7.16beta1") < adv.parse_version("7.16rc1")
    assert adv.parse_version("7.15.3 (stable)") == (7, 15, 3, 2, 0) and adv.parse_version("kaputt") is None
    a = _a()
    assert adv.version_affected(a, "7.10") and adv.version_affected(a, "7.15.3") and adv.version_affected(a, "7.16rc1")
    assert not adv.version_affected(a, "7.16") and not adv.version_affected(a, "7.9.2") and not adv.version_affected(a, None)
    b = _a(fixed_in=None, affected_to="7.14.2")
    assert adv.version_affected(b, "7.14.2") and not adv.version_affected(b, "7.14.3")
    with pytest.raises(adv.AdvisoryError):
        adv.validate({"function": "general", "severity": "high", "affected_from": "7.10"})  # weder bis noch behoben
    with pytest.raises(adv.AdvisoryError):
        adv.validate({"function": "general", "severity": "high", "affected_from": "7.10", "fixed_in": "7.9"})
    with pytest.raises(adv.AdvisoryError):
        adv.validate({"function": "toaster", "severity": "high", "affected_from": "7.10", "fixed_in": "7.11"})


ADV = {"cve": "CVE-TEST-0001", "title": "Test", "function": "general", "severity": "high", "affected_from": "7.10", "fixed_in": "7.16"}


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="r1")  # Simulator: RouterOS 7.15.3
    return t, h, dev


async def test_functions_fleet_alert_and_compliance(client, msp, hub):
    t, h, dev = await _setup(client, msp)
    # Beispiel aus dem Seed ist deaktiviert und schreibgeschützt
    lst = (await client.get("/api/v1/advisories", headers=h)).json()
    (ex,) = lst["advisories"]
    assert ex["builtin"] and not ex["enabled"] and ex["cve"].startswith("BEISPIEL")
    assert (await client.put(f"/api/v1/advisories/{ex['id']}", json={**ADV}, headers=msp)).status_code == 403
    assert (await client.post("/api/v1/advisories", json=ADV, headers=h)).status_code == 403  # nur MSP
    for body in (ADV, {**ADV, "cve": "CVE-TEST-HS", "function": "hotspot"}, {**ADV, "cve": "CVE-TEST-WB", "function": "winbox", "severity": "medium"},
                 {**ADV, "cve": "CVE-TEST-OLD", "affected_from": "6.40", "fixed_in": "6.49"}):
        assert (await client.post("/api/v1/advisories", json=body, headers=msp)).status_code == 201
    # ohne Dienste-Info: winbox „möglicherweise“, hotspot nicht aktiv → nicht betroffen
    d = (await client.get(f"/api/v1/devices/{dev['id']}/advisories", headers=h)).json()
    st = {a["cve"]: a["status"] for a in d["advisories"]}
    assert st == {"CVE-TEST-0001": "affected", "CVE-TEST-WB": "possible"}
    await poll_all()  # liest /ip/service → winbox aktiv
    fleet = (await client.get("/api/v1/advisories/fleet", headers=h)).json()
    assert {a["cve"]: a["status"] for a in fleet[dev["id"]]} == {"CVE-TEST-0001": "affected", "CVE-TEST-WB": "affected"}
    counts = {a["cve"]: a["affected"] for a in (await client.get("/api/v1/advisories", headers=h)).json()["advisories"]}
    assert counts["CVE-TEST-0001"] == 1 and counts["CVE-TEST-OLD"] == 0
    # Alarm (nur „betroffen“, ab Schweregrad high)
    r = await client.post("/api/v1/alert-rules", json={"name": "adv", "type": "security_advisory", "severity": "critical", "duration_s": 0}, headers=h)
    assert r.status_code in (200, 201), r.text
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert [a["subject"] for a in alerts if a["status"] == "firing"] == ["advisory:CVE-TEST-0001"]
    # Compliance-Baseline: Regel „keine bekannten Sicherheitsmeldungen“
    base = next(s for s in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if s["name"] == "MSP-Baseline")
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev["id"]]}, headers=h)
    rep = (await client.get(f"/api/v1/devices/{dev['id']}/compliance", headers=h)).json()
    rows = {r["rule_id"]: r for res in rep for r in res["results"]}
    assert rows["no-advisory"]["status"] == "fail" and "CVE-TEST-0001" in rows["no-advisory"]["detail"]


async def test_blocks_hotspot_and_wlan_activation(client, msp, hub):
    t, h, dev = await _setup(client, msp)
    hs = await client.post("/api/v1/advisories", json={**ADV, "cve": "CVE-HS", "function": "hotspot", "severity": "critical"}, headers=msp)
    await client.post("/api/v1/advisories", json={**ADV, "cve": "CVE-WLAN", "function": "wlan"}, headers=msp)
    await client.post("/api/v1/advisories", json={**ADV, "cve": "CVE-LOW", "function": "hotspot", "severity": "low"}, headers=msp)
    portal = next(p for p in (await client.get("/api/v1/hotspot/portals", headers=h)).json() if p["name"] == "Hotel")
    body = {"name": "Lobby", "slug": "lobby", "device_id": dev["id"], "portal_id": portal["id"], "interface": "bridge"}
    r = await client.post("/api/v1/hotspot/instances", json=body, headers=h)
    assert r.status_code == 409 and "CVE-HS" in r.text and "Firmware" in r.text and "7.16" in r.text
    # WLAN: Gerät wird nicht ausgerollt, Status „blocked“
    rt = get_router(dev["tunnel_ip"])
    rt.wlan_driver = "wifi"
    _seed_wlan(rt)
    p = (await client.post("/api/v1/wlan/profiles", json={"name": "W", "slug": "w", "ssid": "Beispiel", "security": "wpa2-psk",
                                                           "passphrase": "geheim-12345"}, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{p['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{p['id']}/apply", headers=h)
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == p["id"])
    assert p["devices"][0]["status"] == "blocked" and "CVE-WLAN" in p["devices"][0]["error"]
    assert rt.tables["/interface/wifi/configuration"] == []
    # Meldung deaktivieren → Aktivierung möglich
    await client.put(f"/api/v1/advisories/{hs.json()['id']}", json={**ADV, "cve": "CVE-HS", "function": "hotspot", "severity": "critical", "enabled": False},
                     headers=msp)
    assert (await client.post("/api/v1/hotspot/instances", json=body, headers=h)).status_code == 201
