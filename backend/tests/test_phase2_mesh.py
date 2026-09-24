from __future__ import annotations

from app.routeros.simulator import get_router
from app.services.mesh import auto_apply_all
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp, topology="hub_spoke"):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    await client.put("/api/v1/mesh/settings", json={"topology": topology}, headers=h)
    sites = {}
    for name, lan, hub in (("HQ", "192.168.10.0/24", True), ("Filiale A", "192.168.20.0/24", False), ("Filiale B", "192.168.30.0/24", False)):
        r = await client.post("/api/v1/sites", json={"name": name, "lan_subnets": [lan], "is_mesh_hub": hub}, headers=h)
        sites[name] = r.json()
    devs = {}
    for name, site in sites.items():
        devs[name] = await make_paired_device(client, h, site_id=site["id"], name=f"rtr-{site['name'][:3].lower()}{len(devs)}")
    # HQ hat eine öffentliche Adresse
    await client.patch(f"/api/v1/devices/{devs['HQ']['id']}", json={"mesh_endpoint": "203.0.113.10"}, headers=h)
    return t, h, devs


def _peers(dev):
    return [p for p in get_router(dev["tunnel_ip"]).tables["/interface/wireguard/peers"] if p.get("comment", "").startswith("sdwan:mesh:")]


async def test_hub_and_spoke_push(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    r = await client.post("/api/v1/mesh/apply", headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["links"] == 2
    assert all(d["ok"] for d in rep["devices"].values())

    hq, fa = devs["HQ"], devs["Filiale A"]
    hq_peers, fa_peers = _peers(hq), _peers(fa)
    assert len(hq_peers) == 2 and len(fa_peers) == 1
    # Spoke -> Hub: Endpoint gesetzt, Keepalive, Transfernetz + LANs aller anderen Standorte
    p = fa_peers[0]
    assert p["endpoint-address"] == "203.0.113.10"
    assert p["persistent-keepalive"] == "25s"
    allowed = p["allowed-address"].split(",")
    assert "10.200.0.0/24" in allowed and "192.168.10.0/24" in allowed and "192.168.30.0/24" in allowed
    assert "192.168.20.0/24" not in allowed
    assert p["preshared-key"]
    # Hub -> Spoke: keine Endpoint-Adresse (Spoke hinter NAT), nur Spoke-Netze
    hp = next(x for x in hq_peers if x["comment"] == f"sdwan:mesh:{fa['id']}")
    assert "endpoint-address" not in hp
    assert "192.168.20.0/24" in hp["allowed-address"]
    # PSK identisch auf beiden Seiten
    assert hp["preshared-key"] == p["preshared-key"]
    # Routen auf dem Spoke für die entfernten LANs
    routes = [r["dst-address"] for r in get_router(fa["tunnel_ip"]).tables["/ip/route"] if r.get("comment", "").startswith("sdwan:mesh:")]
    assert sorted(routes) == ["192.168.10.0/24", "192.168.30.0/24"]
    # Firewall-Regeln stehen vor unmanaged Regeln
    fw = get_router(hq["tunnel_ip"]).tables["/ip/firewall/filter"]
    assert [r["comment"] for r in fw][:3] == ["sdwan:mesh:wg", "sdwan:mesh:fwd-in", "sdwan:mesh:fwd-out"]

    # Idempotent: erneutes Anwenden ändert nichts
    rep2 = (await client.post("/api/v1/mesh/apply", headers=h)).json()
    for d in rep2["devices"].values():
        assert d["stats"]["peers"] == {"added": 0, "updated": 0, "removed": 0}

    # Status nach Poll
    await poll_all()
    mesh = (await client.get("/api/v1/mesh", headers=h)).json()
    assert len(mesh["links"]) == 2
    assert all(lk["status"] == "up" for lk in mesh["links"])
    assert sum(1 for n in mesh["nodes"] if n["is_hub"]) == 1


async def test_full_mesh_and_switch_back(client, msp, hub):
    _t, h, devs = await _setup(client, msp, topology="full_mesh")
    rep = (await client.post("/api/v1/mesh/apply", headers=h)).json()
    assert rep["links"] == 3
    # Filiale A <-> B: keine Seite erreichbar -> Warnung
    assert any("kein erreichbarer Endpoint" in w for w in rep["warnings"])
    assert len(_peers(devs["Filiale A"])) == 2
    # Zurück auf Hub-and-Spoke: A<->B-Peer wird entfernt
    await client.put("/api/v1/mesh/settings", json={"topology": "hub_spoke"}, headers=h)
    await client.post("/api/v1/mesh/apply", headers=h)
    assert len(_peers(devs["Filiale A"])) == 1
    # Mesh ausschalten -> alles entfernt
    await client.put("/api/v1/mesh/settings", json={"topology": "none"}, headers=h)
    await client.post("/api/v1/mesh/apply", headers=h)
    r = get_router(devs["HQ"]["tunnel_ip"])
    assert not _peers(devs["HQ"])
    assert not [w for w in r.tables["/interface/wireguard"] if w["name"] == "sdwan-mesh"]
    assert not [f for f in r.tables["/ip/firewall/filter"] if f.get("comment", "").startswith("sdwan:mesh")]


async def test_hub_spoke_requires_hub(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    for n in ("A", "B"):
        s = (await client.post("/api/v1/sites", json={"name": n}, headers=h)).json()
        await make_paired_device(client, h, site_id=s["id"], name=f"r{n}")
    r = await client.post("/api/v1/mesh/apply", headers=h)
    assert r.status_code == 409


async def test_auto_apply(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    await auto_apply_all()
    assert len(_peers(devs["HQ"])) == 2
    # Zweiter Lauf ohne Änderung -> kein erneuter Push (Fingerprint)
    get_router(devs["HQ"]["tunnel_ip"]).fail_next.add("/interface/wireguard/peers/print")
    await auto_apply_all()
    assert "/interface/wireguard/peers/print" in get_router(devs["HQ"]["tunnel_ip"]).fail_next


async def test_mesh_failure_reported(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    get_router(devs["Filiale B"]["tunnel_ip"]).fail_next.add("/interface/wireguard/print")
    rep = (await client.post("/api/v1/mesh/apply", headers=h)).json()
    errs = [d for d in rep["devices"].values() if not d["ok"]]
    assert len(errs) == 1 and "simulated failure" in errs[0]["error"]
    audit = (await client.get("/api/v1/audit?action=mesh.apply", headers=h)).json()
    assert audit[0]["success"] is False
