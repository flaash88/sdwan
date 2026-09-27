"""Audit-Behebung AP1 (Router-Injection und Transport) – Tests für Funde ohne bisherigen Reproduktionstest und
zusätzliche Absicherung der Behebungen (siehe docs/AUDIT-2026-09.md, docs/PLAN-AUDIT-FIX.md)."""

from __future__ import annotations

import re

import pytest

from app.config import get_settings
from app.routeros.simulator import get_router
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

VALID_WG = "A" * 43 + "="


async def _admin(client, msp, slug="acme"):
    t = await make_tenant(client, msp, slug)
    return t, await make_tenant_admin(client, msp, t["id"], email=f"admin@{slug}.example.com")


# ----------------------------------------------------------------------------- AUDIT-001/002: Namen und Escaping
@pytest.mark.parametrize("bad", ["a\nb", 'a"b', "a$b", "a[b]", "a{b}", "a;b", "a\\b", "", "x" * 201])
async def test_001_device_name_rejected(client, msp, bad):
    _t, h = await _admin(client, msp)
    assert (await client.post("/api/v1/devices", json={"name": bad}, headers=h)).status_code == 422


async def test_001_device_name_allowed_characters_and_rename(client, msp, hub):
    _t, h = await _admin(client, msp)
    r = await client.post("/api/v1/devices", json={"name": "Filiale Nord (Büro) #2 – rtr.1/a"}, headers=h)
    assert r.status_code == 201, r.text
    dev = await make_paired_device(client, h)
    assert (await client.patch(f"/api/v1/devices/{dev['id']}", json={"name": "neu\n/system reset"}, headers=h)).status_code == 422
    assert (await client.patch(f"/api/v1/devices/{dev['id']}", json={"name": "Neuer Name"}, headers=h)).status_code == 200


def test_002_routeros_str_escapes_and_rejects_control_chars():
    from app.routeros.naming import routeros_str, script_comment, validate_label

    assert routeros_str('a"b$c\\d') == '"a\\"b\\$c\\\\d"'
    with pytest.raises(ValueError):
        routeros_str("a\nb")
    assert "\n" not in script_comment("a\nb") and script_comment("Büro") == "B-ro"
    for bad in ("x\n", 'x"', "x$", "x;"):
        with pytest.raises(ValueError):
            validate_label(bad)


async def test_002_wan_name_rejected_and_ztp_template_wan_name_rejected(client, msp, hub):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": [
        {"name": 'x"y', "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"}]}, headers=h)
    assert r.status_code == 422
    r = await client.post("/api/v1/ztp/templates", json={"name": "t", "content": {"wan": {"mode": "failover", "links": [
        {"name": "a;b", "interface": "ether1", "gateway": "dhcp", "priority": 1, "check_target": "1.1.1.1"}]}}}, headers=h)
    assert r.status_code == 422


async def test_002_netwatch_script_quotes_existing_names():
    """Bestehende Namen (vor der Validierung angelegt) werden im Script maskiert statt ausgeführt."""
    from app.models import WanLink
    from app.services.wan import _scripts

    down, up = _scripts(WanLink(slot=1, name='x" ; /user add name=pwn; :log info "', interface="ether1"), "failover", 30, False)
    assert '\\"' in down and "/user add name=pwn" in down  # nur als Text im String
    assert re.search(r':log warning "[^"]*\\" ; /user add name=pwn; :log info \\"[^"]*"', down)


# ----------------------------------------------------------------------------- AUDIT-026: Validierungs-Regex ohne \n-Lücke
def test_026_validators_reject_trailing_newline():
    from app.api.v1 import firewall, wan
    from app.schemas import SLUG_RE
    from app.services import hotspot, maintenance, onboarding, policy, vrrp, wireguard, wlan, ztp, ztp_import

    cases = [(onboarding._SAFE, "abc"), (ztp._IFACE, "ether1"), (ztp._HOST, "pool.ntp.org"), (ztp._TZ, "Europe/Vienna"),
             (wan._NAME, "ether1"), (vrrp.NAME_RE, "vrrp1"), (wlan._SLUG, "buero"), (wlan._HHMM, "08:00"), (hotspot._SLUG, "gast"),
             (hotspot._HOST, "a.example.com"), (maintenance._HHMM, "08:00"), (ztp_import.SERIAL_RE, "HGK0001"), (policy._NAME, "x"),
             (firewall._IFACE, "ether2"), (SLUG_RE, "acme"), (wireguard._WG_KEY_RE, "A" * 43 + "=")]
    for rx, good in cases:
        assert rx.match(good), (rx.pattern, good)
        assert not rx.match(good + "\n"), rx.pattern


def test_026_quote_helper_and_pydantic_patterns():
    from pydantic import ValidationError

    from app.api.v1.ztp import StageDevice
    from app.services.onboarding import _q

    with pytest.raises(ValueError):
        _q("ok\n")
    with pytest.raises(ValidationError):
        StageDevice(name="r", serial="HGK0001\n")


# ----------------------------------------------------------------------------- AUDIT-027: identity_pattern
def test_027_identity_pattern_only_known_placeholders():
    from app.models import Device, Site, Tenant
    from app.services.ztp import TemplateError, render_identity, validate_template

    for bad in ("{name.__class__}", "{name!r}", "{0}", "{foo}", "{name:>99}"):
        with pytest.raises(TemplateError):
            validate_template({"identity_pattern": bad})
    ok = validate_template({"identity_pattern": "{tenant}-{site}-{name}-{serial}"})["identity_pattern"]
    dev = Device(name="fil01", serial="S1")
    assert render_identity(ok, dev, Tenant(slug="acme"), Site(name="Nord")) == "acme-Nord-fil01-S1"
    assert render_identity("{unbekannt}-{name}", dev, None, None) == "unbekannt--fil01"  # alte Vorlage: kein Fehler, kein format()


# ----------------------------------------------------------------------------- AUDIT-051: Script-Bibliothek und Seriennummer
def test_051_script_values_quoted_outside_strings():
    from app.services.scripts import render

    ctx = {"device.name": "a b=c disabled=yes"}
    assert render("/system identity set name={{ device.name }}", ctx) == '/system identity set name="a b=c disabled=yes"'
    assert render(':log info "Router {{ device.name }} ok"', ctx) == ':log info "Router a b=c disabled=yes ok"'
    assert render(':put "x\\"" ; :put {{device.name}}', {"device.name": "v"}) == ':put "x\\"" ; :put "v"'


async def test_051_serial_validated_and_bootstrap_comment_safe(client, msp, hub):
    from app.models import Device
    from app.services.ztp import bootstrap_script

    _t, h = await _admin(client, msp)
    assert (await client.post("/api/v1/devices", json={"name": "r", "serial": "SN1\n/user add"}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/devices", json={"name": "r2", "serial": "HGK-0001"}, headers=h)).status_code == 201
    text = bootstrap_script("tok", Device(name="r", serial="X\n/user add name=pwn"), None)
    assert not any(line.startswith("/user add") for line in text.splitlines())


# ----------------------------------------------------------------------------- AUDIT-005: Zertifikatsprüfung
def test_005_trust_store_before_fetch_and_no_fallback():
    from app.models import Device
    from app.routeros.schema import CERT_ERROR, TRUST_ANCHORS_CMD, TRUST_ANCHORS_MIN_VERSION
    from app.services.onboarding import onboarding_command, onboarding_script
    from app.services.ztp import bootstrap_script

    texts = {"command": onboarding_command("tok"), "script": onboarding_script("tok", "r"),
             "bootstrap": bootstrap_script("tok", Device(name="r", serial="SN1"), None)}
    for name, t in texts.items():
        assert "check-certificate=no" not in t, name
        assert TRUST_ANCHORS_CMD in t, name
        assert t.index(TRUST_ANCHORS_CMD) < t.index("/tool fetch"), name  # erst Speicher, dann Download
        assert f':error "{CERT_ERROR}' in t, name
        assert all("check-certificate=yes" in line for line in t.splitlines() if "/tool fetch" in line), name
    assert TRUST_ANCHORS_MIN_VERSION in CERT_ERROR
    # Onboarding-Script: Speicher wird VOR jeder Änderung aktiviert (ältere Version bricht ab, ohne etwas anzulegen)
    s = texts["script"]
    assert s.index(TRUST_ANCHORS_CMD) < s.index("/interface wireguard add")


async def test_005_pairing_info_and_meta_carry_min_version(client, msp):
    from app.routeros.schema import TRUST_ANCHORS_MIN_VERSION

    _t, h = await _admin(client, msp)
    r = (await client.post("/api/v1/devices", json={"name": "r"}, headers=h)).json()
    assert r["pairing"]["min_routeros"] == TRUST_ANCHORS_MIN_VERSION
    assert (await client.get("/api/v1/meta")).json()["onboarding_min_routeros"] == TRUST_ANCHORS_MIN_VERSION


async def test_005_selftest_trust_store_row(client, msp, hub):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    t = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    row = next(c for c in t["checks"] if c["key"] == "trust_store")
    assert row["status"] == "ok"
    from app.services.selftest import extra_checks

    warn = next(c for c in extra_checks({"certificate_settings": []}) if c["key"] == "trust_store")
    assert warn["status"] == "warn" and "RouterOS >=" in warn["notes"][0]


# ----------------------------------------------------------------------------- AUDIT-007/053: Pairing
async def test_007_wrong_serial_rejected_and_token_single_use(client, msp, hub):
    _t, h = await _admin(client, msp)
    token = (await client.post("/api/v1/ztp/stage", json={"devices": [{"name": "z1", "serial": "HGK0002"}]}, headers=h)).json()[0]["token"]
    assert (await client.post("/api/v1/pair", json={"token": token, "public_key": VALID_WG, "serial": "FALSCH"})).text.startswith(":error")
    ok = await client.post("/api/v1/pair", json={"token": token, "public_key": VALID_WG, "serial": "hgk0002"})
    assert not ok.text.startswith(":error"), ok.text
    again = await client.post("/api/v1/pair", json={"token": token, "public_key": "B" * 43 + "=", "serial": "HGK0002"})
    assert again.text.startswith(":error")


async def test_053_ztp_token_ttl_configurable(client, msp, monkeypatch):
    import datetime as dt

    _t, h = await _admin(client, msp)
    monkeypatch.setattr(get_settings(), "ztp_token_ttl_days", 30)
    r = (await client.post("/api/v1/ztp/stage", json={"devices": [{"name": "z1", "serial": "HGK0003"}]}, headers=h)).json()[0]
    exp = dt.datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
    days = (exp - dt.datetime.now(dt.UTC)).days
    assert 28 <= days <= 30


# ----------------------------------------------------------------------------- AUDIT-028: ftp für Hotspot-Upload
def test_028_ftp_in_api_policies_and_subsets():
    from app.routeros.schema import API_POLICIES, LOCAL_POLICIES_FULL, REMOTE_POLICIES, local_policies_for

    assert "ftp" in API_POLICIES
    assert set(REMOTE_POLICIES) <= set(API_POLICIES)
    assert set(local_policies_for(",".join(API_POLICIES))) <= set(API_POLICIES)
    assert "ftp" in local_policies_for(",".join(API_POLICIES)) and "ftp" in LOCAL_POLICIES_FULL


def test_028_pair_script_grants_ftp():
    from app.models import Device
    from app.services.onboarding import pair_response_script

    s = pair_response_script(Device(name="r", tunnel_ip="10.100.0.9"), "A" * 43 + "=", "pw12345678")
    assert re.search(r"/user group add name=\"sdwan-api\" policy=[a-z,]*\bftp\b", s)


def _old_group(dev):
    """Gerät mit sdwan-api-Gruppe ohne ftp (Onboarding vor AUDIT-028)."""
    from app.routeros.schema import API_POLICIES

    rt = get_router(dev["tunnel_ip"])
    g = next(x for x in rt.tables["/user/group"] if x.get("name") == "sdwan-api")
    g["policy"] = ",".join(p for p in API_POLICIES if p != "ftp")
    user = next(u for u in rt.tables["/user"] if u.get("name") == get_settings().routeros_api_user)
    user["group"] = "sdwan-api"
    return rt


async def test_028_selftest_warns_only_with_hotspot_and_sync_gives_fix(client, msp, hub, monkeypatch):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    _old_group(dev)
    t = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    assert not any(c["key"] == "rights_hotspot" for c in t["checks"])  # ohne Hotspot kein Hinweis
    from app.services import selftest

    async def uses(_dev):
        return True

    monkeypatch.setattr(selftest, "_uses_hotspot", uses)
    t = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    row = next(c for c in t["checks"] if c["key"] == "rights_hotspot")
    assert row["status"] == "warn" and "ftp" in row["fix"]
    # Abgleich: der API-Benutzer darf sich selbst keine Rechte geben (Simulator wie Annahme) → Einzeiler statt Fehler
    r = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert r["status"] == "readback_mismatch" and "ftp" in r["fix"] and '/user group set [find name="sdwan-api"]' in r["fix"]


async def test_028_sync_adds_ftp_when_router_allows(client, msp, hub, monkeypatch):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    rt = _old_group(dev)
    monkeypatch.setattr(get_settings(), "simulator_enforce_group_rights", False)
    r = (await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)).json()
    assert r["status"] == "unchanged"
    assert "ftp" in next(x for x in rt.tables["/user/group"] if x.get("name") == "sdwan-api")["policy"]


async def test_028_baseline_checks_ftp_service(client, msp):
    _t, h = await _admin(client, msp)
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    rule = next(r for r in base["rules"] if r["id"] == "ftp-off")
    assert rule["type"] == "service_disabled" and rule["params"] == {"service": "ftp"}
