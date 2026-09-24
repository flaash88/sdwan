from __future__ import annotations

import re

from sqlalchemy import select

from app.db import tenant_session
from app.models import Site
from app.services.poller import poll_all
from app.services.wireguard import generate_keypair
from tests.conftest import login, make_paired_device, make_tenant, make_tenant_admin


async def test_login_and_me(client, msp):
    r = await client.get("/api/v1/auth/me", headers=msp)
    assert r.status_code == 200
    assert r.json()["user"]["is_superuser"] is True
    r = await client.post("/api/v1/auth/login", json={"email": "msp@test.example.com", "password": "wrong"})
    assert r.status_code == 401


async def test_rbac_readonly_cannot_write(client, msp):
    t = await make_tenant(client, msp)
    ro = await make_tenant_admin(client, msp, t["id"], email="ro@acme.example.com", role="readonly")
    assert (await client.get("/api/v1/sites", headers=ro)).status_code == 200
    r = await client.post("/api/v1/sites", json={"name": "HQ"}, headers=ro)
    assert r.status_code == 403
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    assert (await client.post("/api/v1/sites", json={"name": "HQ"}, headers=tech)).status_code == 201
    # Techniker darf keine Benutzer verwalten
    assert (await client.get("/api/v1/users", headers=tech)).status_code == 403
    # Tenant-Admin darf keine Tenants anlegen
    adm = await make_tenant_admin(client, msp, t["id"])
    assert (await client.post("/api/v1/tenants", json={"name": "X", "slug": "xx"}, headers=adm)).status_code == 403


async def test_tenant_isolation(client, msp):
    a = await make_tenant(client, msp, "alpha")
    b = await make_tenant(client, msp, "beta")
    ha = await make_tenant_admin(client, msp, a["id"], email="a@a.example.com")
    hb = await make_tenant_admin(client, msp, b["id"], email="b@b.example.com")
    site_a = (await client.post("/api/v1/sites", json={"name": "A-HQ"}, headers=ha)).json()
    await client.post("/api/v1/sites", json={"name": "B-HQ"}, headers=hb)

    # B sieht A's Site nicht – weder in Liste noch direkt
    names = [s["name"] for s in (await client.get("/api/v1/sites", headers=hb)).json()]
    assert names == ["B-HQ"]
    assert (await client.get(f"/api/v1/sites/{site_a['id']}", headers=hb)).status_code == 404
    assert (await client.patch(f"/api/v1/sites/{site_a['id']}", json={"name": "pwn"}, headers=hb)).status_code == 404
    # Gerät für A kann nicht an Site von A durch B gebunden werden
    r = await client.post("/api/v1/devices", json={"name": "x", "site_id": site_a["id"]}, headers=hb)
    assert r.status_code == 404
    # MSP sieht alles, mit X-Tenant-ID nur den Tenant
    assert len((await client.get("/api/v1/sites", headers=msp)).json()) == 2
    scoped = (await client.get("/api/v1/sites", headers={**msp, "X-Tenant-ID": a["id"]})).json()
    assert [s["name"] for s in scoped] == ["A-HQ"]

    # DB-Ebene: Scoped Session filtert automatisch
    import uuid

    async with tenant_session(uuid.UUID(b["id"])) as db:
        rows = (await db.execute(select(Site))).scalars().all()
        assert [s.name for s in rows] == ["B-HQ"]


async def test_cross_tenant_write_blocked_at_db_level():
    import uuid

    import pytest

    from app.db import TenantIsolationError, system_session
    from app.models import Tenant

    async with system_session() as db:
        t1, t2 = Tenant(name="T1", slug="t1"), Tenant(name="T2", slug="t2")
        db.add_all([t1, t2])
        await db.commit()
        t1_id, t2_id = t1.id, t2.id
    async with tenant_session(t1_id) as db:
        db.add(Site(tenant_id=t2_id, name="evil"))
        with pytest.raises(TenantIsolationError):
            await db.flush()
    async with tenant_session(t1_id) as db:
        s = Site(name="auto")  # tenant_id wird aus dem Kontext gesetzt
        db.add(s)
        await db.flush()
        assert s.tenant_id == t1_id
    assert uuid


async def test_pairing_flow_via_script(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    site = (await client.post("/api/v1/sites", json={"name": "HQ", "lan_subnets": ["192.168.10.0/24"]}, headers=h)).json()
    r = await client.post("/api/v1/devices", json={"name": "hq-rtr", "site_id": site["id"]}, headers=h)
    assert r.status_code == 201
    body = r.json()
    token = body["pairing"]["token"]
    assert body["pairing"]["command"].startswith('/tool fetch url="https://cloud.test/api/v1/onboard/')
    assert body["device"]["tunnel_ip"] == "10.100.0.2"

    script = (await client.get(f"/api/v1/onboard/{token}.rsc")).text
    assert "/interface wireguard add name=$iface" in script
    assert token in script

    _, pub = generate_keypair()
    r = await client.post("/api/v1/pair", json={"token": token, "public_key": pub, "serial": "HF1234", "routeros_version": "7.15.3 (stable)", "model": "RB5009"})
    assert r.status_code == 200
    resp = r.text
    assert ":error" not in resp
    assert "address=10.100.0.2/16" in resp
    assert f'public-key="{hub}"' in resp
    assert "allowed-address=10.100.0.1/32" in resp
    assert re.search(r'password="[A-Za-z0-9]{24}"', resp)

    # Token ist Single-Use
    r = await client.post("/api/v1/pair", json={"token": token, "public_key": pub})
    assert ":error" in r.text

    dev = (await client.get(f"/api/v1/devices/{body['device']['id']}", headers=h)).json()
    assert dev["pairing_status"] == "paired"
    assert dev["serial"] == "HF1234"

    peers = (await client.get("/api/v1/internal/hub/peers", headers={"X-Hub-Token": "hubtoken"})).json()
    assert peers == [{"public_key": pub, "allowed_ips": "10.100.0.2/32", "device_id": dev["id"]}]
    assert (await client.get("/api/v1/internal/hub/peers", headers={"X-Hub-Token": "nope"})).status_code == 401

    # Audit-Log enthält Pairing
    audit = (await client.get("/api/v1/audit", headers=h)).json()
    assert any(a["action"] == "device.paired" for a in audit)


async def test_pairing_rejects_bad_key_and_without_hub(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    token = (await client.post("/api/v1/devices", json={"name": "r"}, headers=h)).json()["pairing"]["token"]
    r = await client.post("/api/v1/pair", json={"token": token, "public_key": "invalid"})
    assert "Ungültiger WireGuard-Public-Key" in r.text
    _, pub = generate_keypair()
    r = await client.post("/api/v1/pair", json={"token": token, "public_key": pub})
    assert "Hub noch nicht registriert" in r.text


async def test_poll_marks_online_and_revoke(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    assert dev["status"] == "unknown"
    await poll_all()
    dev = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert dev["status"] == "online"
    assert dev["routeros_version"].startswith("7.")
    summary = (await client.get("/api/v1/dashboard/summary", headers=h)).json()
    assert summary["devices_online"] == 1

    r = await client.post(f"/api/v1/devices/{dev['id']}/revoke", headers=h)
    assert r.json()["pairing_status"] == "revoked"
    peers = (await client.get("/api/v1/internal/hub/peers", headers={"X-Hub-Token": "hubtoken"})).json()
    assert peers == []


async def test_hub_stats_update_handshake(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    r = await client.post("/api/v1/internal/hub/stats", headers={"X-Hub-Token": "hubtoken"},
                          json=[{"public_key": dev["wg_public_key"], "endpoint": "1.2.3.4:5555", "latest_handshake": 1700000000, "rx_bytes": 1, "tx_bytes": 2}])
    assert r.status_code == 200
    dev = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert dev["last_handshake_at"].startswith("2023-11-14")


async def test_routeros_refuses_non_tunnel_address():
    import pytest

    from app.routeros.client import RouterOSError, open_connection

    with pytest.raises(RouterOSError):
        await open_connection("8.8.8.8", "u", "p")
