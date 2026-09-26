"""Phase 16 – Compliance (Regeltypen, Auswertung nach Backup, Bericht, Alarm) und Config-Suche (Maskierung)."""

from __future__ import annotations

import pytest

from app.routeros.simulator import get_router
from app.services.alerts import evaluate_all
from app.services.compliance import ComplianceError, check_regex, evaluate_rule, mask_secrets, validate_rules
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def test_mask_secrets():
    txt = ('/interface wireguard add name=wg1 private-key="abc+/=" listen-port=1\n'
           '/user add name=x password=geheim123 group=full\n'
           '/interface wireguard peers add public-key=PUB preshared-key=PSK\n'
           '/interface wifi security add passphrase="mit leer zeichen" name=sec')
    m = mask_secrets(txt)
    assert "abc+/=" not in m and "geheim123" not in m and "PSK" not in m and "mit leer zeichen" not in m
    assert "public-key=PUB" in m and "private-key=***" in m and "password=***" in m


def test_regex_guard_and_validation():
    with pytest.raises(ComplianceError):
        check_regex("(a+)+$")
    with pytest.raises(ComplianceError):
        check_regex("x" * 201)
    with pytest.raises(ComplianceError):
        validate_rules([{"type": "unbekannt"}])
    rules = validate_rules([{"type": "contains", "params": {"text": "/ip dns"}}, {"type": "regex", "params": {"pattern": "telnet"}}])
    assert [r["id"] for r in rules] == ["r1", "r2"] and rules[1]["params"]["expect"] == "match"


def test_evaluate_rule_types():
    live = {"services": [{"name": "www", "disabled": "true"}, {"name": "api", "disabled": "false", "address": "10.100.0.1/32"},
                         {"name": "ssh", "disabled": "false", "address": ""}],
            "users": [{"name": "admin"}], "resource": {"version": "7.15.3 (stable)"}, "ntp": {"enabled": "no"}, "update": {"channel": "testing"}}
    r = lambda t, **p: evaluate_rule({"type": t, "params": p}, "/ip dns set servers=9.9.9.9\n/ip service set telnet disabled=yes", live)  # noqa: E731
    assert r("contains", text="servers=9.9.9.9")[0] == "ok"
    assert r("not_contains", text="telnet")[0] == "fail"
    assert r("regex", pattern=r"servers=\S+", expect="match")[0] == "ok"
    assert r("service_disabled", service="www")[0] == "ok"
    assert r("no_user", name="admin")[0] == "fail"
    assert r("ntp_enabled")[0] == "fail"
    st, detail = r("service_restricted_to_tunnel", services=["api", "ssh"])
    assert st == "fail" and "ssh" in detail and "api" not in detail
    assert r("channel_in", channels=["stable", "long-term"]) == ("fail", "Kanal testing")
    assert r("min_version", version="7.12")[0] == "ok" and r("min_version", version="7.16")[0] == "fail"
    assert evaluate_rule({"type": "ntp_enabled", "params": {}}, None, None)[0] == "unknown"
    assert evaluate_rule({"type": "contains", "params": {"text": "x"}}, None, None)[0] == "unknown"


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="c1")
    await poll_all()
    return h, dev, t


async def test_baseline_after_backup_report_and_alert(client, msp, hub):
    h, dev, t = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    sets = (await client.get("/api/v1/compliance/rule-sets", headers=h)).json()
    base = next(s for s in sets if s["name"] == "MSP-Baseline")
    assert base["builtin"] and base["scope"] == "global" and len(base["rules"]) == 6
    r = await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    assert r.status_code == 201
    # Auswertung nach jedem Backup
    await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h)
    rep = (await client.get("/api/v1/compliance/report", headers=h)).json()
    (row,) = rep["rows"]
    assert row["device"] == "c1" and row["cells"]["www-off"]["status"] == "fail"  # Simulator: www aktiv
    assert row["cells"]["mgmt-tunnel"]["status"] == "fail"  # ssh ohne Adressbeschränkung
    assert row["cells"]["min-version"]["status"] == "ok"
    # korrigieren -> manuell erneut auswerten
    for s in rt.tables["/ip/service"]:
        if s["name"] == "www":
            s["disabled"] = "true"
        if s["name"] in ("api", "ssh"):
            s["address"] = "10.100.0.1/32"
    await client.post("/api/v1/compliance/evaluate", json={}, headers=h)
    row = (await client.get("/api/v1/compliance/report", headers=h)).json()["rows"][0]
    assert row["cells"]["www-off"]["status"] == "ok" and row["cells"]["mgmt-tunnel"]["status"] == "ok"
    csv = await client.get("/api/v1/compliance/report.csv", headers=h)
    assert csv.status_code == 200 and "c1;MSP-Baseline" in csv.text
    pdf = await client.get("/api/v1/compliance/report.pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    trend = (await client.get("/api/v1/compliance/trend", headers=h)).json()
    assert trend and trend[-1]["passed"] >= 4
    # Alarmtyp compliance_failed: nicht in den Standardregeln (standardmäßig aus), bei Bedarf anlegen
    defaults = (await client.post("/api/v1/alert-rules/defaults", headers=h)).json()
    assert not any(x["type"] == "compliance_failed" for x in defaults)
    r = await client.post("/api/v1/alert-rules", json={"name": "Compliance", "type": "compliance_failed", "severity": "info", "duration_s": 0}, headers=h)
    assert r.status_code == 201, r.text
    cf = r.json()
    next(x for x in rt.tables["/ip/service"] if x["name"] == "www")["disabled"] = "false"
    await client.post("/api/v1/compliance/evaluate", json={}, headers=h)
    await evaluate_all()
    alerts = [a for a in (await client.get("/api/v1/alerts", headers=h)).json() if a["rule_id"] == cf["id"]]
    assert alerts and "Dienst www (HTTP) deaktiviert" in alerts[0]["message"]


async def test_custom_rule_set_rbac_and_builtin_protection(client, msp, hub):
    h, dev, t = await _setup(client, msp)
    base = next(s for s in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if s["builtin"])
    assert (await client.patch(f"/api/v1/compliance/rule-sets/{base['id']}", json={"name": "x", "rules": []}, headers=h)).status_code == 403
    cp = (await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/copy", headers=h)).json()
    assert cp["scope"] == "tenant" and not cp["builtin"]
    r = await client.post("/api/v1/compliance/rule-sets", json={"name": "Eigen", "rules": [{"type": "regex", "params": {"pattern": "(a+)+"}}]}, headers=h)
    assert r.status_code == 422
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    assert (await client.post("/api/v1/compliance/rule-sets", json={"name": "T", "rules": []}, headers=tech)).status_code == 403


async def test_config_search_masks_secrets_and_scopes(client, msp, hub):
    h, dev, t = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt._insert("/user", {"name": "tech", "group": "full", "password": "SuperGeheim1", "comment": "Techniker-Zugang"})
    await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h)
    r = (await client.get("/api/v1/config-search", params={"q": "Techniker"}, headers=h)).json()
    assert r["hits"] and r["hits"][0]["device"] == "c1"
    text = str(r)
    assert "SuperGeheim1" not in text
    # Geheimnisse sind nie Treffer
    assert (await client.get("/api/v1/config-search", params={"q": "SuperGeheim1"}, headers=h)).json()["hits"] == []
    rx = (await client.get("/api/v1/config-search", params={"q": r"name=tech\b", "regex": "true"}, headers=h)).json()
    assert rx["hits"] and any(c["hit"] for m in rx["hits"][0]["matches"] for c in m["context"])
    assert (await client.get("/api/v1/config-search", params={"q": "(a+)+", "regex": "true"}, headers=h)).status_code == 422
    # anderer Mandant sieht nichts, mandantenübergreifend nur MSP
    t2 = await make_tenant(client, msp, slug="other")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="admin@other.example.com")
    assert (await client.get("/api/v1/config-search", params={"q": "Techniker"}, headers=h2)).json()["hits"] == []
    assert (await client.get("/api/v1/config-search", params={"q": "Techniker", "all_tenants": "true"}, headers=h2)).status_code == 403
    allr = (await client.get("/api/v1/config-search", params={"q": "Techniker", "all_tenants": "true"}, headers=msp)).json()
    assert allr["hits"]
