from __future__ import annotations

from app.routeros.simulator import get_router
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

LINKS = [
    {"name": "Glasfaser", "interface": "ether1", "gateway": "100.64.0.1", "priority": 1, "weight": 2, "check_target": "1.1.1.1"},
    {"name": "LTE", "interface": "lte1", "gateway": "lte1", "priority": 2, "weight": 1, "check_target": "9.9.9.9", "check_type": "ping", "latency_threshold_ms": 200},
]


async def _dev(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, await make_paired_device(client, h)


def _managed(r, path):
    return [x for x in r.tables[path] if str(x.get("comment", "")).startswith("sdwan:wan:")]


async def test_failover_config(client, msp, hub):
    h, dev = await _dev(client, msp)
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "recovery_delay_s": 60, "links": LINKS}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["push"]["ok"] is True
    assert [lk["slot"] for lk in body["links"]] == [1, 2]
    rt = get_router(dev["tunnel_ip"])
    routes = {x["comment"]: x for x in _managed(rt, "/ip/route")}
    assert routes["sdwan:wan:check:1"]["dst-address"] == "1.1.1.1/32"
    assert routes["sdwan:wan:check:1"]["gateway"] == "100.64.0.1"
    assert routes["sdwan:wan:default:1"]["distance"] == "1"
    assert routes["sdwan:wan:default:2"]["distance"] == "2"
    assert routes["sdwan:wan:default:2"]["gateway"] == "lte1"
    nw = {x["comment"]: x for x in _managed(rt, "/tool/netwatch")}
    assert nw["sdwan:wan:check:2"]["thr-avg"] == "200ms"
    assert 'disable [find where comment~"^sdwan:wan:default:1"]' in nw["sdwan:wan:check:1"]["down-script"]
    assert ":delay 60s" in nw["sdwan:wan:check:1"]["up-script"]
    assert not _managed(rt, "/ip/firewall/mangle")  # kein PCC im Failover
    assert [x["comment"] for x in _managed(rt, "/ip/firewall/nat")] == ["sdwan:wan:nat"]

    await poll_all()
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [(lk["status"], lk["active"]) for lk in links] == [("up", True), ("up", False)]

    # Ausfall WAN1 simulieren -> WAN2 übernimmt
    r = await client.post(f"/api/v1/devices/{dev['id']}/wan/1/simulate-outage", headers=h)
    assert r.status_code == 200
    await poll_all()
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [(lk["status"], lk["active"]) for lk in links] == [("down", False), ("up", True)]
    await client.post(f"/api/v1/devices/{dev['id']}/wan/1/simulate-outage?down=false", headers=h)
    await poll_all()
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert links[0]["active"] is True


async def test_pcc_loadbalance_and_removal(client, msp, hub):
    h, dev = await _dev(client, msp)
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "loadbalance_pcc", "links": LINKS}, headers=h)
    assert r.json()["push"]["ok"]
    rt = get_router(dev["tunnel_ip"])
    mangle = _managed(rt, "/ip/firewall/mangle")
    pcc = [m for m in mangle if "per-connection-classifier" in m]
    # Gewichtung 2:1 -> 3 PCC-Slots, zwei davon auf WAN1
    assert [m["per-connection-classifier"] for m in pcc] == ["both-addresses-and-ports:3/0", "both-addresses-and-ports:3/1", "both-addresses-and-ports:3/2"]
    assert [m["new-connection-mark"] for m in pcc] == ["sdwan-wan1", "sdwan-wan1", "sdwan-wan2"]
    tables = [t["name"] for t in _managed(rt, "/routing/table")]
    assert tables == ["sdwan-wan1", "sdwan-wan2"]
    t1 = sorted((x["distance"], x["gateway"]) for x in _managed(rt, "/ip/route") if x.get("routing-table") == "sdwan-wan1")
    assert t1 == [("1", "100.64.0.1"), ("2", "lte1")]
    # Mangle steht vor unmanaged Regeln (keine vorhanden) und Address-List privat
    assert len(_managed(rt, "/ip/firewall/address-list")) == 4

    # Zurück auf ECMP: Mangle/Tabellen weg, gleiche Distanz
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "loadbalance_ecmp", "links": LINKS}, headers=h)
    assert r.json()["push"]["ok"]
    assert not _managed(rt, "/ip/firewall/mangle") and not _managed(rt, "/routing/table")
    d = {x["comment"]: x["distance"] for x in _managed(rt, "/ip/route") if "default" in x["comment"]}
    assert d == {"sdwan:wan:default:1": "1", "sdwan:wan:default:2": "1"}

    # Alle Links entfernen -> WAN-Konfiguration komplett entfernt
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": []}, headers=h)
    assert r.json()["push"]["ok"]
    for path in ("/ip/route", "/tool/netwatch", "/ip/firewall/nat", "/interface/list", "/interface/list/member"):
        assert not _managed(rt, path), path


async def test_wan_validation(client, msp, hub):
    h, dev = await _dev(client, msp)
    dup = [LINKS[0], {**LINKS[1], "check_target": "1.1.1.1"}]
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": dup}, headers=h)
    assert r.status_code == 400
    too_many = [{**LINKS[0], "interface": f"ether{i}", "check_target": f"1.1.1.{i}"} for i in range(1, 6)]
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": too_many}, headers=h)
    assert r.status_code == 400
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": [{**LINKS[0], "gateway": "1.2.3.4; /system reset"}]}, headers=h)
    assert r.status_code == 422


async def test_dhcp_gateway_resolution(client, msp, hub):
    h, dev = await _dev(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt.tables["/ip/dhcp-client"].append({".id": "*D1", "interface": "ether1", "gateway": "192.0.2.1", "status": "bound"})
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": [{**LINKS[0], "gateway": "dhcp"}]}, headers=h)
    assert r.json()["push"]["ok"], r.text
    assert r.json()["links"][0]["resolved_gateway"] == "192.0.2.1"
    # Gateway ändert sich -> Poll erkennt es und pusht neu
    rt.tables["/ip/dhcp-client"][0]["gateway"] = "192.0.2.254"
    await poll_all()
    gw = [x["gateway"] for x in _managed(rt, "/ip/route") if x["comment"] == "sdwan:wan:default:1"]
    assert gw == ["192.0.2.254"]


async def test_wan_link_test(client, msp, hub):
    h, dev = await _dev(client, msp)
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": LINKS[:1]}, headers=h)
    r = await client.post(f"/api/v1/devices/{dev['id']}/wan/1/test", headers=h)
    assert r.status_code == 200 and r.json()["avg_ms"] > 0


async def test_failover_flushes_connections_but_keeps_tunnels(client, msp, hub):
    """Bugfix: auch im Failover-Modus werden die Verbindungen des ausgefallenen WANs entfernt."""
    h, dev = await _dev(client, msp)
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "flush_connections": True, "links": LINKS}, headers=h)
    rt = get_router(dev["tunnel_ip"])
    down = {x["comment"]: x for x in _managed(rt, "/tool/netwatch")}["sdwan:wan:check:1"]["down-script"]
    assert down.index("/ip route disable") < down.index("/ip firewall connection remove")
    assert '/ip address find where interface="ether1"' in down
    assert 'reply-dst-address~("^" . $ip . ":")' in down
    # Management-Tunnel (51820) und Mesh (13232) bleiben bestehen
    assert '!(protocol="udp" and dst-address~":51820")' in down and '!(protocol="udp" and dst-address~":13232")' in down
    assert "connection-mark" not in down
    # ohne flush_connections kein Entfernen
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "flush_connections": False, "links": LINKS}, headers=h)
    down = {x["comment"]: x for x in _managed(rt, "/tool/netwatch")}["sdwan:wan:check:1"]["down-script"]
    assert "connection remove" not in down
    # PCC nutzt weiterhin die Connection-Mark
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "loadbalance_pcc", "links": LINKS}, headers=h)
    down = {x["comment"]: x for x in _managed(rt, "/tool/netwatch")}["sdwan:wan:check:1"]["down-script"]
    assert 'connection-mark="sdwan-wan1"' in down
