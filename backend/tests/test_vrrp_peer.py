"""VRRP-Gegenstelle: Validierung, Ping im Poll (zeitlich begrenzt), „Peer prüfen“."""

from __future__ import annotations

import time

from app.routeros.simulator import get_router
from app.services.poller import poll_all
from app.services.vrrp import PEER_PING_LIMIT_S
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

BASE = {"name": "vrrp-kassen", "interface": "ether2", "vrid": 110, "priority": 100, "vip": "192.168.110.1",
        "local_address": "192.168.110.21/24"}
PEER = {**BASE, "peer_address": "192.168.110.2", "peer_description": "FortiGate Zentrale (port5)"}


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="filiale-01")
    return h, dev


async def _put(client, h, dev, inst):
    return await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [inst]}, headers=h)


async def _inst(client, h, dev):
    return (await client.get(f"/api/v1/devices/{dev['id']}/vrrp", headers=h)).json()["instances"][0]


async def test_peer_validation(client, msp, hub):
    h, dev = await _setup(client, msp)
    for bad, msg in [
        ({**PEER, "peer_address": "10.0.0.2"}, "nicht im Netz"),
        ({**PEER, "local_address": None}, "lokale Adresse"),
        ({**PEER, "peer_address": "192.168.110.1"}, "unterscheiden"),
        ({**PEER, "peer_address": "192.168.110.21"}, "unterscheiden"),
        ({**PEER, "peer_address": "192.168.110.2/24"}, "Ungültige"),
    ]:
        r = await _put(client, h, dev, bad)
        assert r.status_code == 400 and msg in r.text, (bad, r.text)
    r = await _put(client, h, dev, PEER)
    assert r.status_code == 200, r.text
    i = r.json()["instances"][0]
    assert (i["peer_address"], i["peer_description"], i["peer_reachable"]) == ("192.168.110.2", "FortiGate Zentrale (port5)", None)
    assert "priority_peer" not in i  # Priorität der Gegenstelle wird bewusst nicht geführt
    # Gegenstelle wird nicht auf den Router geschrieben
    rt = get_router(dev["tunnel_ip"])
    assert not any(str(row.get("address", "")).split("/")[0] == "192.168.110.2" for row in rt.tables["/ip/address"])
    assert not any("192.168.110.2 " in f"{v} " for row in rt.tables["/interface/vrrp"] for v in row.values())
    # ohne Gegenstelle weiterhin gültig
    assert (await _put(client, h, dev, BASE)).status_code == 200


async def test_poll_pings_peer_with_src_and_timeout(client, msp, hub):
    h, dev = await _setup(client, msp)
    await _put(client, h, dev, PEER)
    rt = get_router(dev["tunnel_ip"])
    await poll_all()
    p = rt.ping_log[-1]
    assert p["address"] == "192.168.110.2" and p["src-address"] == "192.168.110.21"
    assert (p["count"], p["timeout"]) == (3, "500ms")
    i = await _inst(client, h, dev)
    assert i["peer_reachable"] is True and i["peer_rtt_ms"] > 0 and i["peer_checked_at"]
    rt.down_hosts.add("192.168.110.2")
    await poll_all()
    i = await _inst(client, h, dev)
    assert i["peer_reachable"] is False and i["peer_rtt_ms"] is None


async def test_hanging_peer_ping_does_not_block_poll(client, msp, hub):
    h, dev = await _setup(client, msp)
    await _put(client, h, dev, PEER)
    rt = get_router(dev["tunnel_ip"])
    await poll_all()
    rt.hang_hosts.add("192.168.110.2")
    rt.set_vrrp_master("vrrp-kassen", True)
    t0 = time.monotonic()
    await poll_all()
    assert time.monotonic() - t0 < PEER_PING_LIMIT_S + 1.5  # je Instanz max. ~2 s
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert d["status"] == "online"  # Poll selbst erfolgreich
    i = await _inst(client, h, dev)
    assert i["peer_reachable"] is False
    assert i["state"] == "master"  # restliche Poll-Ergebnisse wurden übernommen


async def test_peer_check_endpoint_and_rbac(client, msp, hub):
    h, dev = await _setup(client, msp)
    r = await _put(client, h, dev, PEER)
    iid = r.json()["instances"][0]["id"]
    r = await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{iid}/ping", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["peer_reachable"] is True and r.json()["ping"]["received"] == 3
    get_router(dev["tunnel_ip"]).down_hosts.add("192.168.110.2")
    r = await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{iid}/ping", headers=h)
    assert r.json()["peer_reachable"] is False
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@acme.example.com", role="readonly")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{iid}/ping", headers=ro)).status_code == 403
    # ohne Gegenstelle: 409; geänderte Gegenstelle verwirft altes Ergebnis
    r = await _put(client, h, dev, {**PEER, "peer_address": "192.168.110.3"})
    assert r.json()["instances"][0]["peer_reachable"] is None
    await _put(client, h, dev, BASE)
    assert (await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{iid}/ping", headers=h)).status_code == 409


def test_ztp_template_accepts_peer_fields():
    import pytest

    from app.services.ztp import TemplateError, validate_template

    out = validate_template({"vrrp": [PEER]})
    assert out["vrrp"][0]["peer_address"] == "192.168.110.2" and out["vrrp"][0]["peer_description"].startswith("FortiGate")
    with pytest.raises(TemplateError):
        validate_template({"vrrp": [{**PEER, "peer_address": "10.1.1.1"}]})
