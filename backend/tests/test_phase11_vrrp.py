"""Phase 11 – VRRP & Backup-Transparenz (Kassen-VLAN-Szenario mit zentraler FortiGate)."""

from __future__ import annotations

from sqlalchemy import select

from app.db import system_session
from app.models import StatusEvent
from app.routeros.simulator import get_router
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

# WAN1 = ether2 zur FortiGate (echte IP, nicht die VIP), WAN2 = 5G-Modem an ether8
WAN = [
    {"name": "Glasfaser-Core", "interface": "ether2", "gateway": "192.168.110.2", "priority": 1, "check_target": "1.1.1.1"},
    {"name": "5G", "interface": "ether8", "gateway": "192.168.8.1", "priority": 2, "check_target": "9.9.9.9"},
]
VRRP = {"name": "vrrp-kassen", "interface": "ether2", "vrid": 110, "priority": 100, "vip": "192.168.110.1",
        "local_address": "192.168.110.21/24", "linked_wan_slot": 1}


async def _setup(client, msp, with_wan=True):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="filiale-01")
    if with_wan:
        r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": WAN}, headers=h)
        assert r.json()["push"]["ok"], r.text
    return h, dev


def _vrrp_rows(rt):
    return [r for r in rt.tables["/interface/vrrp"] if str(r.get("comment", "")).startswith("sdwan:vrrp:")]


async def test_vrrp_push_and_idempotency(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt.tables["/interface/vrrp"].append({".id": "*M1", "name": "manual-vrrp", "interface": "ether3", "vrid": "5"})
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["push"]["ok"] is True
    inst = body["instances"][0]
    assert inst["vip"] == "192.168.110.1/32"  # Default-Präfix /32
    (v,) = _vrrp_rows(rt)
    assert (v["name"], v["interface"], v["vrid"], v["priority"], v["interval"], v["preemption-mode"], v["version"]) == \
        ("vrrp-kassen", "ether2", "110", "100", "1s", "yes", "3")
    assert 'disable [find where comment~"^sdwan:wan:default:1"]' in v["on-master"]
    assert 'reply-dst-address~("^" . $ip . ":")' in v["on-master"]  # Verbindungen des WAN-Slots werden geleert
    assert 'netwatch get [find where comment="sdwan:wan:check:1"] status] = "up"' in v["on-backup"]
    addrs = {a["comment"]: a for a in rt.tables["/ip/address"] if str(a.get("comment", "")).startswith("sdwan:vrrp:")}
    assert {(a["address"], a["interface"]) for a in addrs.values()} == {("192.168.110.21/24", "ether2"), ("192.168.110.1/32", "vrrp-kassen")}
    # manuelle Instanz bleibt unangetastet
    assert any(r[".id"] == "*M1" for r in rt.tables["/interface/vrrp"])
    # erneut anwenden -> keine Änderungen
    rep = (await client.post(f"/api/v1/devices/{dev['id']}/vrrp/apply", headers=h)).json()
    assert rep["stats"]["vrrp"] == {"added": 0, "updated": 0, "removed": 0}
    assert rep["stats"]["addresses"] == {"added": 0, "updated": 0, "removed": 0}
    # Instanz entfernen -> VIP, lokale Adresse und Interface weg, manuelle bleibt
    await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": []}, headers=h)
    assert _vrrp_rows(rt) == [] and not [a for a in rt.tables["/ip/address"] if str(a.get("comment", "")).startswith("sdwan:vrrp:")]
    assert any(r[".id"] == "*M1" for r in rt.tables["/interface/vrrp"])
    audit = (await client.get("/api/v1/audit?action=vrrp.", headers=h)).json()
    assert {a["action"] for a in audit} >= {"vrrp.update", "vrrp.apply"}


async def test_vrrp_validation(client, msp, hub):
    h, dev = await _setup(client, msp, with_wan=False)
    for bad, code in (
        ({**VRRP, "vrid": 0}, 422),
        ({**VRRP, "vrid": 256}, 422),
        ({**VRRP, "priority": 255}, 422),
        ({**VRRP, "vip": "192.168.110.1/24"}, 400),
        ({**VRRP, "vip": "10.0.0.1"}, 400),  # nicht im Netz der lokalen Adresse
        ({**VRRP, "local_address": "192.168.110.1/24"}, 400),  # gleich der VIP
        ({**VRRP, "interface": "ether2; /system reset"}, 400),
    ):
        r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [bad]}, headers=h)
        assert r.status_code == code, (bad, r.text)
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP, {**VRRP, "name": "b"}]}, headers=h)
    assert r.status_code == 400  # gleiche VRID auf gleichem Interface
    # ohne lokale Adresse ist jede /32-VIP gültig
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [{**VRRP, "local_address": None}]}, headers=h)
    assert r.status_code == 200
    # Read-Only darf nicht schreiben
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@x.example.com", role="readonly")
    assert (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": []}, headers=ro)).status_code == 403


async def test_vrrp_master_disables_linked_wan_and_state_is_tracked(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    await poll_all()
    st = (await client.get(f"/api/v1/devices/{dev['id']}/vrrp", headers=h)).json()["instances"][0]
    assert st["state"] == "backup"
    wan = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [lk["active"] for lk in wan] == [True, False]

    # Glasfaser fällt aus: FortiGate nicht mehr erreichbar -> MikroTik wird Master
    rt = get_router(dev["tunnel_ip"])
    rt.down_hosts.add("1.1.1.1")
    r = await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    assert r.status_code == 200, r.text
    # on-master hat die WAN1-Default-Route sofort deaktiviert (vor der Netwatch)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "yes"
    await poll_all()
    st = (await client.get(f"/api/v1/devices/{dev['id']}/vrrp", headers=h)).json()["instances"][0]
    assert st["state"] == "master"
    wan = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [lk["active"] for lk in wan] == [False, True]  # 5G trägt jetzt

    # FortiGate zurück, VRRP wieder Backup: on-backup schaltet nur ein, weil Netwatch "up" meldet
    rt.down_hosts.clear()
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "no"
    await poll_all()
    async with system_session() as db:
        ev = (await db.execute(select(StatusEvent).where(StatusEvent.subject == f"vrrp:{inst['id']}").order_by(StatusEvent.at))).scalars().all()
        assert [e.status for e in ev] == ["backup", "master", "backup"]


async def test_vrrp_on_backup_keeps_routes_off_while_netwatch_down(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    rt = get_router(dev["tunnel_ip"])
    rt.down_hosts.add("1.1.1.1")
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
    # Netwatch noch down -> Route bleibt aus (Hysterese bleibt bei der Netwatch)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "yes"


async def test_vrrp_via_zero_touch(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tpl = {"wan_interface": "ether8", "lan": {"enabled": False},
           "wan": {"mode": "failover", "links": WAN},
           "vrrp": [{k: v for k, v in VRRP.items() if k != "local_address"}]}
    r = await client.post("/api/v1/ztp/templates", json={"name": "Filiale Kassen", "content": tpl}, headers=h)
    assert r.status_code == 201, r.text
    bad = await client.post("/api/v1/ztp/stage", json={"template_id": r.json()["id"], "devices": [
        {"name": "fil-x", "serial": "HX1", "vrrp_local_address": "10.9.9.9/24"}]}, headers=h)
    assert bad.status_code == 422  # VIP nicht im Netz
    staged = (await client.post("/api/v1/ztp/stage", json={"template_id": r.json()["id"], "devices": [
        {"name": "fil-02", "serial": "HX2", "vrrp_local_address": "192.168.110.22/24"}]}, headers=h)).json()
    dev_id = staged[0]["device"]["id"]
    r = await client.post(f"/api/v1/devices/{dev_id}/simulate-pair", headers=h)
    assert r.status_code == 200, r.text
    await poll_all()
    dev = (await client.get(f"/api/v1/devices/{dev_id}", headers=h)).json()
    assert dev["ztp_state"] == "provisioned", dev["ztp_log"]
    inst = (await client.get(f"/api/v1/devices/{dev_id}/vrrp", headers=h)).json()["instances"][0]
    assert inst["local_address"] == "192.168.110.22/24" and inst["linked_wan_slot"] == 1
    assert _vrrp_rows(get_router(dev["tunnel_ip"]))
