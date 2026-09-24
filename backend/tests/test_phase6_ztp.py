from __future__ import annotations

from app.routeros.simulator import get_router
from app.services.poller import poll_all
from app.services.wireguard import generate_keypair
from tests.conftest import make_tenant, make_tenant_admin

TEMPLATE = {
    "identity_pattern": "{tenant}-{site}-{name}",
    "timezone": "Europe/Vienna",
    "ntp_servers": ["pool.ntp.org"],
    "dns_servers": ["9.9.9.9", "1.1.1.1"],
    "wan_interface": "ether1",
    "lan": {"enabled": True, "bridge_ports": ["ether2", "ether3"], "dhcp": True},
    "wan": {"mode": "failover", "links": [
        {"name": "Primär", "interface": "ether1", "gateway": "100.64.0.1", "priority": 1, "check_target": "1.1.1.1"},
    ]},
}


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    site = (await client.post("/api/v1/sites", json={"name": "Filiale", "lan_subnets": ["192.168.50.0/24"]}, headers=h)).json()
    pol = (await client.post("/api/v1/policies", json={"name": "Base", "content": {"filter": [{"chain": "input", "action": "drop", "protocol": "tcp", "dst-port": "23"}]}}, headers=h)).json()
    tpl = (await client.post("/api/v1/ztp/templates", json={"name": "Filiale Standard", "content": {**TEMPLATE, "policy_ids": [pol["id"]]}}, headers=h))
    assert tpl.status_code == 201, tpl.text
    return h, site, tpl.json(), pol


async def test_stage_and_zero_touch_flow(client, msp, hub):
    h, site, tpl, _pol = await _setup(client, msp)
    r = await client.post("/api/v1/ztp/stage", json={"template_id": tpl["id"], "site_id": site["id"], "devices": [
        {"name": "fil01", "serial": "HGF0123ABC"}, {"name": "fil02", "serial": "HGF0456DEF"}]}, headers=h)
    assert r.status_code == 201, r.text
    staged = r.json()
    assert len(staged) == 2 and staged[0]["device"]["ztp_state"] == "staged"
    boot = staged[0]["bootstrap_script"]
    assert "/system scheduler add name=sdwan-ztp start-time=startup interval=1m" in boot
    assert f"/api/v1/onboard/{staged[0]['token']}.rsc" in boot
    assert '/ip dhcp-client add interface="ether1"' in boot

    # Router bootet beim Kunden: falsche Seriennummer wird abgelehnt
    _, pub = generate_keypair()
    r = await client.post("/api/v1/pair", json={"token": staged[0]["token"], "public_key": pub, "serial": "OTHER999"})
    assert "Seriennummer passt nicht" in r.text
    r = await client.post("/api/v1/pair", json={"token": staged[0]["token"], "public_key": pub, "serial": "hgf0123abc", "routeros_version": "7.16"})
    script = r.text
    assert ":error" not in script
    assert '/system identity set name="acme-Filiale-fil01"' in script
    assert "/ip address add address=192.168.50.1/24 interface=bridge" in script
    assert "ranges=192.168.50.11-192.168.50.254" in script
    assert 'servers="9.9.9.9,1.1.1.1"' in script
    assert "interface=\"ether2\"" in script

    dev_id = staged[0]["device"]["id"]
    dev = (await client.get(f"/api/v1/devices/{dev_id}", headers=h)).json()
    assert dev["ztp_state"] == "paired"

    # Erster Online-Poll -> WAN + Policies werden automatisch gepusht
    await poll_all()
    dev = (await client.get(f"/api/v1/devices/{dev_id}", headers=h)).json()
    assert dev["ztp_state"] == "provisioned", dev["ztp_log"]
    rt = get_router(dev["tunnel_ip"])
    assert any(r.get("comment") == "sdwan:wan:default:1" for r in rt.tables["/ip/route"])
    assert any(str(r.get("comment", "")).startswith("sdwan:fw:") for r in rt.tables["/ip/firewall/filter"])
    wan = (await client.get(f"/api/v1/devices/{dev_id}/wan", headers=h)).json()
    assert wan["links"][0]["name"] == "Primär"
    pols = (await client.get(f"/api/v1/devices/{dev_id}/policies", headers=h)).json()
    assert pols[0]["status"] == "deployed"

    listing = (await client.get("/api/v1/ztp/devices", headers=h)).json()
    assert {d["ztp_state"] for d in listing} == {"provisioned", "staged"}


async def test_stage_duplicate_serial_and_regenerate(client, msp, hub):
    h, site, tpl, _ = await _setup(client, msp)
    r = await client.post("/api/v1/ztp/stage", json={"template_id": tpl["id"], "devices": [{"name": "a", "serial": "SER1"}]}, headers=h)
    assert r.status_code == 201
    r2 = await client.post("/api/v1/ztp/stage", json={"devices": [{"name": "b", "serial": "ser1"}]}, headers=h)
    assert r2.status_code == 409
    dev_id = r.json()[0]["device"]["id"]
    old_token = r.json()[0]["token"]
    r = await client.post(f"/api/v1/devices/{dev_id}/ztp/bootstrap", headers=h)
    assert r.status_code == 200 and "sdwan-ztp" in r.text
    assert old_token not in r.text
    assert ":error" in (await client.get(f"/api/v1/onboard/{old_token}.rsc")).text


async def test_template_validation(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    for bad in ({"dns_servers": ['1.1.1.1"; /system reset']}, {"lan": {"bridge_ports": ["ether1"]}},
                {"timezone": "Europe/Vienna; x"}, {"wan": {"links": [{"name": "x"}]}}):
        r = await client.post("/api/v1/ztp/templates", json={"name": "bad", "content": bad}, headers=h)
        assert r.status_code == 422, bad
