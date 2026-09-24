from __future__ import annotations

from app.services import metrics
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def test_metrics_collected_and_queryable(client, msp, hub):
    metrics.reset_sink()
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    site = (await client.post("/api/v1/sites", json={"name": "HQ"}, headers=h)).json()
    dev = await make_paired_device(client, h, site_id=site["id"])
    await poll_all()
    await poll_all()  # zweiter Poll -> Raten berechenbar
    sink = metrics.get_sink()
    sys_points = [p for p in sink.points if p["measurement"] == "system"]
    assert len(sys_points) == 2
    assert sys_points[0]["tags"]["tenant"] == "Acme" and sys_points[0]["tags"]["site"] == "HQ"
    assert "cpu_load" in sys_points[0]["fields"] and "mgmt_rtt_ms" in sys_points[0]["fields"]
    iface = [p for p in sink.points if p["measurement"] == "interface" and p["tags"]["interface"] == "ether1"]
    assert "rx_bps" not in iface[0]["fields"] and iface[1]["fields"]["rx_bps"] > 0

    r = await client.get(f"/api/v1/devices/{dev['id']}/metrics?measurement=system&range=1h", headers=h)
    assert r.status_code == 200 and len(r.json()["points"]) == 2
    r = await client.get(f"/api/v1/devices/{dev['id']}/metrics?measurement=interface&range=15m", headers=h)
    assert any(p.get("interface") == "ether1" for p in r.json()["points"])
    assert (await client.get(f"/api/v1/devices/{dev['id']}/metrics?measurement=evil", headers=h)).status_code == 400

    # Fremder Tenant bekommt 404
    t2 = await make_tenant(client, msp, "other")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="o@other.example.com")
    assert (await client.get(f"/api/v1/devices/{dev['id']}/metrics", headers=h2)).status_code == 404


async def test_live_mode(client, msp, hub):
    metrics.reset_sink()
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    assert await metrics.live_device_ids() == set()
    r = await client.post(f"/api/v1/devices/{dev['id']}/metrics/live", headers=h)
    assert r.status_code == 200
    assert dev["id"] in await metrics.live_device_ids()
    await poll_all(only=await metrics.live_device_ids())
    assert any(p["tags"]["device_id"] == dev["id"] for p in metrics.get_sink().points)
