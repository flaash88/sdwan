from __future__ import annotations

from app.routeros.simulator import get_router
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

POLICY = {
    "address_lists": [{"list": "blocked", "address": "203.0.113.0/24"}, {"list": "mgmt", "address": "198.51.100.7"}],
    "filter": [
        {"chain": "input", "action": "accept", "src-address-list": "mgmt", "protocol": "tcp", "dst-port": "22,8291", "comment": "Admin-Zugriff"},
        {"chain": "forward", "action": "drop", "src-address-list": "blocked"},
    ],
    "nat": [{"chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "8443", "to-addresses": "192.168.10.5", "to-ports": "443", "in-interface-list": "sdwan-wan"}],
}


def fw(dev, path="/ip/firewall/filter"):
    return [r for r in get_router(dev["tunnel_ip"]).tables[path] if str(r.get("comment", "")).startswith("sdwan:fw:")]


async def _setup(client, msp, n=2):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    devs = [await make_paired_device(client, h, name=f"r{i}") for i in range(n)]
    return t, h, devs


async def test_policy_push_to_multiple_devices(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    # unmanaged Regel vorhanden -> Policy-Regeln müssen davor landen
    get_router(devs[0]["tunnel_ip"]).tables["/ip/firewall/filter"].append({".id": "*FF", "chain": "input", "action": "drop", "comment": "defconf: drop all"})
    r = await client.post("/api/v1/policies", json={"name": "Baseline", "content": POLICY}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    assert len(r.json()["assigned"]) == 2
    r = await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    assert r.status_code == 202
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "success", dep
    for d in devs:
        rules = fw(d)
        assert [x["action"] for x in rules] == ["accept", "drop"]
        assert rules[0]["comment"].endswith("Admin-Zugriff")
        assert len(fw(d, "/ip/firewall/address-list")) == 2
        assert fw(d, "/ip/firewall/nat")[0]["to-addresses"] == "192.168.10.5"
    table = get_router(devs[0]["tunnel_ip"]).tables["/ip/firewall/filter"]
    assert table[-1]["comment"] == "defconf: drop all"
    pol = (await client.get(f"/api/v1/policies/{pid}", headers=h)).json()
    assert all(a["status"] == "deployed" and a["deployed_version"] == 1 for a in pol["assignments"])


async def test_versioning_and_rollback(client, msp, hub):
    _t, h, devs = await _setup(client, msp, n=1)
    pid = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()["id"]
    await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [devs[0]["id"]]}, headers=h)
    await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    v2 = {**POLICY, "filter": POLICY["filter"][:1]}
    r = await client.patch(f"/api/v1/policies/{pid}", json={"content": v2, "note": "drop entfernt"}, headers=h)
    assert r.json()["version"] == 2
    await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    assert len(fw(devs[0])) == 1
    vers = (await client.get(f"/api/v1/policies/{pid}/versions", headers=h)).json()
    assert [v["version"] for v in vers] == [2, 1]
    r = await client.post(f"/api/v1/policies/{pid}/rollback", json={"version": 1}, headers=h)
    assert r.json()["version"] == 3
    assert len(fw(devs[0])) == 2


async def test_push_failure_rolls_back_device(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    pid = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()["id"]
    await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    before = [dict(x) for x in fw(devs[1])]
    # v2 schlägt auf Gerät 1 mitten im Filter-Push fehl (alte Regeln schon entfernt) -> Rollback
    v2 = {**POLICY, "filter": [*POLICY["filter"], {"chain": "forward", "action": "reject"}]}
    await client.patch(f"/api/v1/policies/{pid}", json={"content": v2}, headers=h)
    get_router(devs[1]["tunnel_ip"]).fail_next.add("/ip/firewall/filter/add")
    r = await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "partial"
    res = dep["results"][devs[1]["id"]]
    assert res["ok"] is False and res["rolled_back"] is True
    assert [x["action"] for x in fw(devs[1])] == [x["action"] for x in before]
    assert len(fw(devs[0])) == 3


async def test_atomic_deploy_rolls_back_all(client, msp, hub):
    _t, h, devs = await _setup(client, msp)
    pid = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()["id"]
    await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    get_router(devs[1]["tunnel_ip"]).fail_next.add("/ip/firewall/filter/add")
    r = await client.post(f"/api/v1/policies/{pid}/deploy", json={"atomic": True}, headers=h)
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "rolled_back"
    assert fw(devs[0]) == [] and fw(devs[1]) == []


async def test_validation_rejects_bad_rules(client, msp, hub):
    _t, h, _ = await _setup(client, msp, n=0)
    for bad in (
        {"filter": [{"chain": "input", "action": "drop", "comment": 'x"; /system reset'}]},
        {"filter": [{"chain": "prerouting", "action": "drop"}]},
        {"filter": [{"chain": "input", "action": "drop", "script": "evil"}]},
        {"nat": [{"chain": "dstnat", "action": "dst-nat"}]},
        {"address_lists": [{"list": "x y", "address": "1.2.3.4"}]},
    ):
        r = await client.post("/api/v1/policies", json={"name": "bad", "content": bad}, headers=h)
        assert r.status_code == 422, bad


async def test_global_policy_scope(client, msp, hub):
    t, h, devs = await _setup(client, msp, n=1)
    # MSP ohne Tenant-Auswahl -> globale Policy
    r = await client.post("/api/v1/policies", json={"name": "MSP-Baseline", "content": POLICY}, headers=msp)
    gid = r.json()["id"]
    assert r.json()["scope"] == "global"
    # Tenant sieht sie, darf sie zuweisen und deployen, aber nicht ändern
    assert any(p["id"] == gid for p in (await client.get("/api/v1/policies", headers=h)).json())
    assert (await client.patch(f"/api/v1/policies/{gid}", json={"name": "x"}, headers=h)).status_code == 403
    await client.post(f"/api/v1/policies/{gid}/assign", json={"device_ids": [devs[0]["id"]]}, headers=h)
    r = await client.post(f"/api/v1/policies/{gid}/deploy", json={}, headers=h)
    assert r.status_code == 202
    assert len(fw(devs[0])) == 2
    # Tenant-Policy anderer Mandanten unsichtbar
    t2 = await make_tenant(client, msp, "zeta")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="z@zeta.example.com")
    own = (await client.post("/api/v1/policies", json={"name": "Z", "content": {}}, headers=h2)).json()["id"]
    assert (await client.get(f"/api/v1/policies/{own}", headers=h)).status_code == 404
    # Entzug -> Regeln werden entfernt
    r = await client.delete(f"/api/v1/policies/{gid}/assign/{devs[0]['id']}", headers=h)
    assert r.status_code == 200
    assert fw(devs[0]) == []
