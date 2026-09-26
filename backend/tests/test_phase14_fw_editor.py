"""Phase 14 – Firewall-Editor: Compiler, Lint, Objekte/Zonen/Bausteine, Deploy-Vorprüfung, Treffer, Umwandlung."""

from __future__ import annotations

import pytest

from app.routeros.simulator import get_router
from app.services.fw_compile import Catalog, SpecError, compile_spec, expert_to_simple, rule_key_from_comment
from app.services.fw_lint import DeviceCtx, lint_spec
from app.services.policy import validate_content
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

# Beispiel-Katalog (Beispieladressen aus Dokumentationsnetzen, keine echten Kundennetze)
CAT = Catalog(
    objects={
        "o1": {"id": "o1", "tenant_id": None, "name": "Server", "slug": "server", "kind": "host", "values": ["192.0.2.10"], "members": []},
        "o2": {"id": "o2", "tenant_id": None, "name": "Netz B", "slug": "netz-b", "kind": "network", "values": ["198.51.100.0/24"], "members": []},
        "o3": {"id": "o3", "tenant_id": None, "name": "Gruppe", "slug": "gruppe", "kind": "group", "values": [], "members": ["o1", "o2"]},
        "o4": {"id": "o4", "tenant_id": None, "name": "Leer", "slug": "leer", "kind": "group", "values": [], "members": []},
    },
    services={
        "s1": {"id": "s1", "tenant_id": None, "name": "HTTPS", "slug": "https", "entries": [{"protocol": "tcp", "ports": "443"}], "members": []},
        "s2": {"id": "s2", "tenant_id": None, "name": "DNS", "slug": "dns", "entries": [{"protocol": "udp", "ports": "53"}, {"protocol": "tcp", "ports": "53"}], "members": []},
        "s3": {"id": "s3", "tenant_id": None, "name": "HTTP", "slug": "http", "entries": [{"protocol": "tcp", "ports": "80"}], "members": []},
    },
    zones={
        "zl": {"id": "zl", "tenant_id": None, "name": "LAN", "slug": "lan", "source": "manual", "management": False},
        "zw": {"id": "zw", "tenant_id": None, "name": "WAN", "slug": "wan", "source": "wan", "management": False},
        "zm": {"id": "zm", "tenant_id": None, "name": "Management", "slug": "management", "source": "manual", "management": True},
    },
)


def _rule(**kw):
    return {"id": kw.pop("id", "aaaa1111"), "enabled": True, "action": "accept", **kw}


def test_compile_basic_structure():
    spec = {"rules": [
        _rule(id="r0000001", src_zone="zl", dst_zone="zw", services=["s1", "s3"], comment="Web"),
        _rule(id="r0000002", src_zone="zl", dst_zone="router", services=["s2"]),
        _rule(id="r0000003", src_zone="zl", dst=["o1", "o2"], action="drop", log=True),
    ], "nat": [{"type": "masquerade", "zone": "zw"}]}
    c = validate_content(compile_spec(spec, CAT))
    f = c["filter"]
    # Plattform-Zugänge zuerst, Default-Drop zuletzt
    assert f[0]["comment"] == "base:platform-hub" and f[0]["in-interface"] == "sdwan-mgmt" and f[0]["src-address"] == "10.100.0.1"
    assert [r["comment"] for r in f[-2:]] == ["base:default-drop-input", "base:default-drop-forward"]
    web = [r for r in f if r["comment"].startswith("r:r0000001")]
    assert len(web) == 1 and web[0]["dst-port"] == "443,80" and web[0]["protocol"] == "tcp"
    assert web[0]["in-interface-list"] == "sdwan-zone-lan" and web[0]["out-interface-list"] == "sdwan-wan"  # WAN-Zone = bestehende Liste
    dns = [r for r in f if r["comment"].startswith("r:r0000002")]
    assert {r["protocol"] for r in dns} == {"udp", "tcp"} and all(r["chain"] == "input" for r in dns)
    drop = next(r for r in f if r["comment"].startswith("r:r0000003"))
    assert drop["dst-address-list"] == "sdwan-r-r0000003-dst" and drop["log"] == "yes"
    assert {a["address"] for a in c["address_lists"] if a["list"] == "sdwan-r-r0000003-dst"} == {"192.0.2.10", "198.51.100.0/24"}
    assert c["nat"][0] == {"chain": "srcnat", "action": "masquerade", "out-interface-list": "sdwan-wan", "comment": c["nat"][0]["comment"]}
    # Management-Zugriff nur aus Management-Zone
    mg = [r for r in f if r["comment"].startswith("base:mgmt")]
    assert mg[0]["in-interface-list"] == "sdwan-zone-management" and mg[-1]["action"] == "drop"


def test_compile_options_and_errors():
    c = compile_spec({"options": {"baseline": False, "default_drop": False}, "rules": [_rule(src_zone="zl")]}, CAT)
    assert [r["comment"] for r in c["filter"]] == ["r:aaaa1111"]
    with pytest.raises(SpecError):
        compile_spec({"rules": [_rule(src_zone="nope")]}, CAT)
    with pytest.raises(SpecError):
        compile_spec({"rules": [_rule(action="allow")]}, CAT)
    with pytest.raises(SpecError):
        compile_spec({"nat": [{"type": "portforward", "ext_port": "8443", "host": "o2"}]}, CAT)  # Netz statt Host
    pf = compile_spec({"nat": [{"type": "portforward", "in_zone": "zw", "protocol": "tcp", "ext_port": "8443", "host": "o1", "int_port": "443"}]}, CAT)
    assert pf["nat"][0]["to-addresses"] == "192.0.2.10" and pf["nat"][0]["to-ports"] == "443"


def test_expert_to_simple_keeps_rules_unchanged():
    content = {"address_lists": [{"list": "x", "address": "192.0.2.1"}], "filter": [{"chain": "forward", "action": "drop"}], "nat": []}
    assert compile_spec(expert_to_simple(content), CAT) == content


def test_rule_key_from_comment():
    assert rule_key_from_comment("sdwan:fw:0123abcd:f7 r:ab12cd34 Web") == "0123abcd:ab12cd34"
    assert rule_key_from_comment("sdwan:fw:0123abcd:f0 base:platform-hub") == "0123abcd:base:platform-hub"
    assert rule_key_from_comment("sdwan:fw:0123abcd:f0 Kommentar") is None


def test_lint():
    spec = {"rules": [
        _rule(id="a0000001", src_zone="zl"),
        _rule(id="a0000002", src_zone="zl", dst_zone="zw", services=["s1"]),  # verdeckt durch a1
        _rule(id="a0000003"),  # any -> any
        _rule(id="a0000004", dst=["o4"], action="drop"),
    ], "nat": [{"type": "portforward", "in_zone": "zw", "ext_port": "8443", "host": "o1"}]}
    codes = {i["code"] for i in lint_spec(spec, CAT)}
    assert {"shadowed", "any_any", "empty_object", "no_router_access"} <= codes
    dup = {"rules": [_rule(id="b0000001", src_zone="zl", dst_zone="zw"), _rule(id="b0000002", src_zone="zl", dst_zone="zw")]}
    assert "duplicate" in {i["code"] for i in lint_spec(dup, CAT)}
    pf = {"rules": [], "nat": [{"type": "portforward", "in_zone": "zw", "ext_port": "8443", "host": "o1"}]}
    assert "portforward_no_rule" in {i["code"] for i in lint_spec(pf, CAT)}
    use = {"rules": [_rule(src_zone="zl", dst_zone="zw")]}
    issues = lint_spec(use, CAT, [DeviceCtx(name="r1", zone_ids=set(), has_wan=False, later_policies=1)])
    by = {i["code"]: i for i in issues}
    assert by["mgmt_zone_missing"]["level"] == "error" and "nur noch über den Tunnel" in by["mgmt_zone_missing"]["message"]
    assert by["zone_no_wan"]["level"] == "error" and "zone_empty" in by and "drop_not_last" in by
    ok = lint_spec(use, CAT, [DeviceCtx(name="r1", zone_ids={"zl", "zm"}, has_wan=True)])
    assert not [i for i in ok if i["level"] == "error"]


# ----------------------------------------------------------------------------- API / Simulator
async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="fw1")
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    return h, dev, cat, z


async def _simple_policy(client, h, spec, name="Editor"):
    r = await client.post("/api/v1/policies", json={"name": name, "mode": "simple", "spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def test_seeds_visible_and_builtin_protected(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    assert {"lan", "wan", "management", "vpn", "guest"} <= set(z)
    assert {s["slug"] for s in cat["services"]} >= {"http", "https", "dns", "ntp", "ssh", "rdp", "smb", "icmp", "winbox", "sip"}
    assert len(cat["blocks"]) >= 5 and all(b["builtin"] and b["scope"] == "global" for b in cat["blocks"])
    https = next(s for s in cat["services"] if s["slug"] == "https")
    r = await client.patch(f"/api/v1/fw/services/{https['id']}", json={"name": "X", "entries": [{"protocol": "tcp", "ports": "1"}]}, headers=h)
    assert r.status_code == 403
    cp = (await client.post(f"/api/v1/fw/services/{https['id']}/copy", headers=h)).json()
    assert cp["scope"] == "tenant" and not cp["builtin"]
    # Bausteine nur für Admins
    tech = await make_tenant_admin(client, msp, dev["tenant_id"], email="tech@acme.example.com", role="technician")
    assert (await client.post("/api/v1/fw/blocks", json={"name": "B"}, headers=tech)).status_code == 403
    assert (await client.post("/api/v1/fw/objects", json={"name": "Drucker", "kind": "host", "values": ["192.0.2.50"]}, headers=tech)).status_code == 201


async def test_editor_deploy_zones_and_block(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    # WAN-Konfiguration (Zone WAN nutzt sdwan-wan)
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": [
        {"name": "Internet", "interface": "ether1", "gateway": "dhcp", "check_target": "1.1.1.1"}]}, headers=h)
    r = await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [
        {"interface": "bridge", "zone_id": z["lan"]}, {"interface": "ether3", "zone_id": z["management"]}]}, headers=h)
    assert r.status_code == 200 and r.json()["apply"]["ok"], r.text
    lists = {x["name"] for x in rt.tables["/interface/list"]}
    assert {"sdwan-zone-lan", "sdwan-zone-management"} <= lists
    assert {(m["list"], m["interface"]) for m in rt.tables["/interface/list/member"] if str(m.get("comment", "")).startswith("sdwan:zone:")} == \
        {("sdwan-zone-lan", "bridge"), ("sdwan-zone-management", "ether3")}
    block = next(b for b in cat["blocks"] if b["name"] == "Standard-Härtung")
    ex = (await client.post(f"/api/v1/fw/blocks/{block['id']}/expand", json={"params": {}}, headers=h)).json()
    assert ex["rules"][0]["src_zone"] == z["lan"] and ex["rules"][0]["dst_zone"] == "router"
    pol = await _simple_policy(client, h, {"rules": ex["rules"], "nat": ex["nat"]})
    assert pol["mode"] == "simple" and pol["content"]["filter"][0]["comment"] == "base:platform-hub"
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    listing = (await client.get("/api/v1/policies", headers=h)).json()
    assert next(p for p in listing if p["id"] == pol["id"])["undeployed"][0]["name"] == "fw1"  # noch nicht ausgerollt
    pv = (await client.post(f"/api/v1/policies/{pol['id']}/preview", json={}, headers=h)).json()
    assert pv["commands"][0] == "/ip firewall address-list" or "/ip firewall filter" in pv["commands"]
    assert "oben" in pv["placement"] and not [i for i in pv["lint"] if i["level"] == "error"]
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 202, r.text
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "success", dep
    managed = [x for x in rt.tables["/ip/firewall/filter"] if str(x.get("comment", "")).startswith("sdwan:fw:")]
    assert managed[0]["comment"].endswith("base:platform-hub") and managed[-1]["comment"].endswith("base:default-drop-forward")
    assert any(x.get("action") == "masquerade" and x.get("out-interface-list") == "sdwan-wan" for x in rt.tables["/ip/firewall/nat"])
    listing = (await client.get("/api/v1/policies", headers=h)).json()
    assert next(p for p in listing if p["id"] == pol["id"])["undeployed"] == []
    # Änderung -> Diff gegen ausgerollte Version, „nicht ausgerollt“
    spec = pol["spec"]
    spec["rules"].append({"src_zone": z["lan"], "dst_zone": z["wan"], "action": "drop", "comment": "neu"})
    upd = (await client.patch(f"/api/v1/policies/{pol['id']}", json={"spec": spec}, headers=h)).json()
    assert upd["version"] == 2
    pv = (await client.post(f"/api/v1/policies/{pol['id']}/preview", json={}, headers=h)).json()
    assert pv["deployed_version"] == 1 and pv["diff"]["added"] >= 1
    assert (await client.get(f"/api/v1/policies/{pol['id']}", headers=h)).json()["undeployed"][0]["deployed_version"] == 1


async def test_default_drop_skips_device_with_manual_rules(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]}]}, headers=h)
    rt.tables["/ip/firewall/filter"].append({".id": "*M1", "chain": "input", "action": "accept", "protocol": "tcp", "dst-port": "8080", "comment": "manuell"})
    before = [dict(x) for x in rt.tables["/ip/firewall/filter"]]
    pol = await _simple_policy(client, h, {"rules": [{"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]})
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    chk = (await client.post(f"/api/v1/policies/{pol['id']}/deploy-check", json={}, headers=h)).json()
    (d,) = chk["devices"]
    assert chk["default_drop"] and d["unmanaged"][0]["comment"] == "manuell"
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 409 and "übersprungen" in r.text
    assert [dict(x) for x in rt.tables["/ip/firewall/filter"]] == before  # nichts verändert
    # mit ausdrücklicher Bestätigung
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"confirm_devices": [dev["id"]]}, headers=h)
    assert r.status_code == 202
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "success"
    manual = next(x for x in rt.tables["/ip/firewall/filter"] if x.get(".id") == "*M1")
    assert manual["comment"] == "manuell"
    idx = [i for i, x in enumerate(rt.tables["/ip/firewall/filter"]) if str(x.get("comment", "")).startswith("sdwan:fw:")]
    assert max(idx) < rt.tables["/ip/firewall/filter"].index(manual)  # verwaltete Regeln stehen oben


async def test_mgmt_zone_missing_requires_confirmation(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    pol = await _simple_policy(client, h, {"options": {"default_drop": False}, "rules": []})
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 409 and "mgmt_zone_missing" in r.text
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"confirm_lint": True}, headers=h)
    assert r.status_code == 202


async def test_object_change_recompiles_and_delete_protection(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    obj = (await client.post("/api/v1/fw/objects", json={"name": "Kassenserver", "kind": "host", "values": ["192.0.2.20"]}, headers=h)).json()
    grp = (await client.post("/api/v1/fw/objects", json={"name": "Ziele", "kind": "group", "members": [obj["id"]]}, headers=h)).json()
    pol = await _simple_policy(client, h, {"options": {"default_drop": False}, "rules": [{"src_zone": z["lan"], "dst": [grp["id"]], "action": "accept"}]})
    r = await client.patch(f"/api/v1/fw/objects/{obj['id']}", json={"name": "Kassenserver", "kind": "host", "values": ["192.0.2.21"]}, headers=h)
    assert r.json()["recompiled_policies"] == ["Editor"]
    p = (await client.get(f"/api/v1/policies/{pol['id']}", headers=h)).json()
    assert p["version"] == 2 and {"list": "sdwan-obj-ziele", "address": "192.0.2.21"} in p["content"]["address_lists"]
    assert (await client.delete(f"/api/v1/fw/objects/{grp['id']}", headers=h)).status_code == 409
    assert (await client.delete(f"/api/v1/fw/objects/{obj['id']}", headers=h)).status_code == 409  # Mitglied einer Gruppe
    bad = await client.post("/api/v1/fw/objects", json={"name": "X", "kind": "network", "values": ["10.0.0.0/33"]}, headers=h)
    assert bad.status_code == 422


async def test_convert_modes(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    content = {"address_lists": [], "filter": [{"chain": "forward", "action": "drop", "comment": "alt"}], "nat": []}
    p = (await client.post("/api/v1/policies", json={"name": "Alt", "content": content}, headers=h)).json()
    assert p["mode"] == "expert"  # Standard bleibt Expertenmodus
    r = await client.post(f"/api/v1/policies/{p['id']}/convert", json={"to": "simple"}, headers=h)
    assert r.status_code == 409 and "Rohregeln" in r.text
    s = (await client.post(f"/api/v1/policies/{p['id']}/convert", json={"to": "simple", "confirm": True}, headers=h)).json()
    assert s["mode"] == "simple" and s["content"] == p["content"] and s["spec"]["raw"]["filter"][0]["comment"] == "alt"
    e = (await client.post(f"/api/v1/policies/{p['id']}/convert", json={"to": "expert"}, headers=h)).json()
    assert e["mode"] == "expert" and e["content"] == p["content"]
    assert (await client.patch(f"/api/v1/policies/{p['id']}", json={"spec": {}}, headers=h)).status_code == 422


async def test_hit_counters_and_reset(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    pol = await _simple_policy(client, h, {"options": {"default_drop": False, "baseline": False},
                                           "rules": [{"id": "hit00001", "src_zone": z["lan"], "action": "accept"}]})
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"confirm_lint": True}, headers=h)
    row = next(x for x in rt.tables["/ip/firewall/filter"] if "r:hit00001" in str(x.get("comment", "")))
    row.update(packets="42", bytes="4200")
    await poll_all()
    hits = (await client.get(f"/api/v1/policies/{pol['id']}/hits", headers=h)).json()["rules"]
    assert hits["hit00001"]["packets"] == 42 and hits["hit00001"]["last_hit_at"]
    r = await client.post(f"/api/v1/devices/{dev['id']}/firewall/reset-counters", headers=h)
    assert r.json()["reset"] >= 1 and row["packets"] == "0"
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@acme.example.com", role="readonly")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/firewall/reset-counters", headers=ro)).status_code == 403


async def test_global_policy_cannot_use_tenant_objects(client, msp, hub):
    h, dev, cat, z = await _setup(client, msp)
    obj = (await client.post("/api/v1/fw/objects", json={"name": "Mandant", "kind": "host", "values": ["192.0.2.30"]}, headers=h)).json()
    r = await client.post("/api/v1/policies", json={"name": "Global", "mode": "simple",
                                                    "spec": {"rules": [{"dst": [obj["id"]], "src_zone": z["lan"], "action": "accept"}]}}, headers=msp)
    assert r.status_code == 422 and "globalen Policies" in r.text


async def test_ztp_template_zones_validation(client, msp, hub):
    from app.services.ztp import TemplateError, validate_template

    assert validate_template({"zones": {"lan": ["bridge"], "management": ["ether3"]}})["zones"] == {"lan": ["bridge"], "management": ["ether3"]}
    with pytest.raises(TemplateError):
        validate_template({"zones": {"lan": ["ether2"], "guest": ["ether2"]}})
