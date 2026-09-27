"""Nachtrag Phase 14 – Werks-Firewall (defconf): gesonderte Vorprüfung, Deaktivieren statt Überspringen, Wiederherstellen
nur der selbst deaktivierten Regeln, Zonen-Vorschlag aus den defconf-Interface-Lists."""

from __future__ import annotations

from app.routeros.simulator import get_router
from tests.test_phase14_fw_editor import _setup, _simple_policy

# Auszug der RouterOS-7-Werkskonfiguration (Kommentare wie ab Werk)
DEFCONF = [
    ("*D1", "input", "accept", {"connection-state": "established,related,untracked"}, "defconf: accept established,related,untracked"),
    ("*D2", "input", "drop", {"connection-state": "invalid"}, "defconf: drop invalid"),
    ("*D3", "input", "accept", {"protocol": "icmp"}, "defconf: accept ICMP"),
    ("*D4", "input", "drop", {"in-interface-list": "!LAN"}, "defconf: drop all not coming from LAN"),
    ("*D5", "forward", "fasttrack-connection", {"connection-state": "established,related"}, "defconf: fasttrack"),
    ("*D6", "forward", "drop", {"connection-state": "new", "connection-nat-state": "!dstnat", "in-interface-list": "WAN"},
     "defconf: drop all from WAN not DSTNATed"),
]


def _factory(rt) -> None:
    for rid, chain, action, extra, comment in DEFCONF:
        rt.tables["/ip/firewall/filter"].append({".id": rid, "chain": chain, "action": action, **extra, "comment": comment, "disabled": "false"})
    rt.tables["/interface/list"] += [{".id": "*LW", "name": "WAN", "comment": "defconf"}, {".id": "*LL", "name": "LAN", "comment": "defconf"}]
    rt.tables["/interface/list/member"] += [{".id": "*MW", "list": "WAN", "interface": "ether1", "comment": "defconf"},
                                            {".id": "*ML", "list": "LAN", "interface": "bridge", "comment": "defconf"}]


def _state(rt) -> dict[str, str]:
    return {r[".id"]: r.get("disabled", "false") for r in rt.tables["/ip/firewall/filter"] if str(r.get("comment", "")).startswith("defconf")}


async def _policy(client, h, dev, z):
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]}]}, headers=h)
    pol = await _simple_policy(client, h, {"rules": [{"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]})
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    return pol


async def test_defconf_reported_separately_and_disabled_on_deploy(client, msp, hub):
    h, dev, _cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    pol = await _policy(client, h, dev, z)
    chk = (await client.post(f"/api/v1/policies/{pol['id']}/deploy-check", json={}, headers=h)).json()
    (d,) = chk["devices"]
    assert d["unmanaged"] == [] and [x["comment"] for x in d["defconf"]] == [c for *_x, c in DEFCONF]
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)  # Standard: defconf deaktivieren
    assert r.status_code == 202 and r.json()["skipped"] == []
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "success"
    assert set(_state(rt).values()) == {"yes"} and len(_state(rt)) == len(DEFCONF)  # deaktiviert, nicht gelöscht
    st = (await client.get(f"/api/v1/devices/{dev['id']}/firewall/defconf", headers=h)).json()
    assert {x["rule_id"] for x in st["disabled"]} == {rid for rid, *_ in DEFCONF} and st["active"] == []
    # erneutes Deploy: keine doppelten Einträge
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 202
    assert len((await client.get(f"/api/v1/devices/{dev['id']}/firewall/defconf", headers=h)).json()["disabled"]) == len(DEFCONF)
    audit = (await client.get("/api/v1/audit?action=policy.deploy", headers=h)).json()
    assert [a for a in audit if a["action"] == "policy.deploy"][-1]["details"]["disable_defconf_devices"] == [dev["id"]]


async def test_option_off_skips_and_manual_rules_unchanged(client, msp, hub):
    h, dev, _cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    pol = await _policy(client, h, dev, z)
    before = [dict(x) for x in rt.tables["/ip/firewall/filter"]]
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"disable_defconf": False}, headers=h)
    assert r.status_code == 409 and "übersprungen" in r.text
    assert [dict(x) for x in rt.tables["/ip/firewall/filter"]] == before
    # zusätzlich eine manuelle Regel: Gerät wird trotz defconf-Option übersprungen, nichts verändert
    rt.tables["/ip/firewall/filter"].append({".id": "*M1", "chain": "input", "action": "accept", "dst-port": "8080", "protocol": "tcp", "comment": "manuell"})
    before = [dict(x) for x in rt.tables["/ip/firewall/filter"]]
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 409 and '"unmanaged"' in r.text and "1 manuelle Regeln" in r.text
    assert [dict(x) for x in rt.tables["/ip/firewall/filter"]] == before
    # bestätigt: manuelle Regel bleibt aktiv, defconf deaktiviert
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"confirm_devices": [dev["id"]]}, headers=h)
    assert r.status_code == 202
    assert next(x for x in rt.tables["/ip/firewall/filter"] if x[".id"] == "*M1").get("disabled", "false") == "false"
    assert set(_state(rt).values()) == {"yes"}


async def test_restore_on_unassign_only_own_rules(client, msp, hub):
    h, dev, _cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    next(x for x in rt.tables["/ip/firewall/filter"] if x[".id"] == "*D5")["disabled"] = "true"  # vom Kunden deaktiviert
    pol = await _policy(client, h, dev, z)
    await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    st = (await client.get(f"/api/v1/devices/{dev['id']}/firewall/defconf", headers=h)).json()
    assert "*D5" not in {x["rule_id"] for x in st["disabled"]}
    # Kunde löscht eine Regel zwischendurch, eine andere bekommt einen neuen Kommentar
    rt.tables["/ip/firewall/filter"] = [x for x in rt.tables["/ip/firewall/filter"] if x[".id"] != "*D3"]
    next(x for x in rt.tables["/ip/firewall/filter"] if x[".id"] == "*D6")["comment"] = "geändert"
    r = await client.delete(f"/api/v1/policies/{pol['id']}/assign/{dev['id']}", headers=h)
    assert r.status_code == 200
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    restored = dep["results"][dev["id"]]["defconf_restored"]
    assert sorted(restored["enabled"]) == ["*D1", "*D2", "*D4"] and sorted(restored["missing"]) == ["*D3", "*D6"]
    s = _state(rt)
    assert s["*D1"] == s["*D2"] == s["*D4"] == "no" and s["*D5"] == "true"  # vom Kunden deaktivierte Regel bleibt aus
    assert next(x for x in rt.tables["/ip/firewall/filter"] if x[".id"] == "*D6")["disabled"] == "yes"  # verändert → nicht angefasst
    assert (await client.get(f"/api/v1/devices/{dev['id']}/firewall/defconf", headers=h)).json()["disabled"] == []


async def test_restore_button_and_rbac(client, msp, hub):
    h, dev, _cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    pol = await _policy(client, h, dev, z)
    await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    r = await client.post(f"/api/v1/devices/{dev['id']}/firewall/defconf/restore", headers=h)
    assert r.status_code == 200 and len(r.json()["enabled"]) == len(DEFCONF)
    assert set(_state(rt).values()) == {"no"}
    assert (await client.get("/api/v1/audit?action=fw.defconf.restore", headers=h)).json()
    assert (await client.get(f"/api/v1/devices/{dev['id']}/firewall/defconf", headers=h)).json()["disabled"] == []


async def test_zone_suggestions_from_defconf_lists(client, msp, hub):
    h, dev, _cat, z = await _setup(client, msp)
    _factory(get_router(dev["tunnel_ip"]))
    res = (await client.get(f"/api/v1/devices/{dev['id']}/zones/suggestions", headers=h)).json()
    by = {s["list"]: s for s in res["suggestions"]}
    assert by["LAN"]["zone_id"] == z["lan"] and by["LAN"]["interfaces"] == ["bridge"] and by["LAN"]["applicable"] and by["LAN"]["defconf"]
    assert by["WAN"]["interfaces"] == ["ether1"] and by["WAN"]["applicable"] is False  # WAN-Zone folgt der WAN-Konfiguration
