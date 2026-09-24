from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import update

from app.config import get_settings
from app.db import system_session
from app.models import RemoteSession
from app.remote_proxy import ProxyManager
from app.routeros.simulator import get_router
from app.services.remote import expire_sessions
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _echo_server():
    async def handle(r, w):
        while data := await r.read(1024):
            w.write(data)
            await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def test_remote_session_proxy_and_audit(client, msp, hub, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "remote_proxy_target_override", "127.0.0.1")
    monkeypatch.setattr(s, "remote_proxy_port_range", "41500-41509")
    monkeypatch.setattr(s, "remote_proxy_host", "proxy.test")
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"], role="technician", email="tech@acme.example.com")
    adm = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    srv, echo_port = await _echo_server()
    rt = get_router(dev["tunnel_ip"])
    winbox = next(x for x in rt.tables["/ip/service"] if x["name"] == "winbox")
    winbox.update({"port": str(echo_port), "disabled": "true", "address": "192.168.88.0/24"})

    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "winbox", "duration_minutes": 30, "reason": "Ticket #42"}, headers=h)
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["listen_port"] == 41500 and sess["connect"] == "proxy.test:41500"
    assert sess["allowed_cidr"] == "127.0.0.1/32"
    assert sess["password"] and sess["username"].startswith("sdwan-rs-")
    # Temp-User + Dienst freigeschaltet (Hub-Adresse ergänzt)
    assert any(u["name"] == sess["username"] and u["address"] == "10.100.0.1/32" for u in rt.tables["/user"])
    assert winbox["disabled"] == "no" and winbox["address"] == "192.168.88.0/24,10.100.0.1/32"

    mgr = ProxyManager(bind_host="127.0.0.1")
    await mgr.reconcile()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 41500)
        writer.write(b"hello winbox")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 2) == b"hello winbox"
        writer.close()
        await asyncio.sleep(0.2)

        # Zweite Session mit fremder erlaubter IP -> Verbindung wird abgewiesen
        r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "duration_minutes": 10, "allowed_cidr": "203.0.113.0/24"}, headers=h)
        denied = r.json()
        await mgr.reconcile()
        rd, wr = await asyncio.open_connection("127.0.0.1", denied["listen_port"])
        assert await asyncio.wait_for(rd.read(100), 2) == b""
        wr.close()
        await asyncio.sleep(0.1)

        audit = (await client.get("/api/v1/audit?action=remote.", headers=adm)).json()
        actions = [a["action"] for a in audit]
        assert {"remote.open", "remote.connect", "remote.disconnect", "remote.denied"} <= set(actions)
        disc = next(a for a in audit if a["action"] == "remote.disconnect")
        assert disc["details"]["bytes_in"] == 12 and disc["details"]["source"] == "127.0.0.1"
        assert disc["user_email"] is None and disc["details"]["user"] == "tech@acme.example.com"

        # Schließen: Temp-User weg, Listener gestoppt
        r = await client.post(f"/api/v1/remote-sessions/{sess['id']}/close", headers=h)
        assert r.json()["status"] == "closed" and r.json()["connections"] == 1
        assert not any(u["name"] == sess["username"] for u in rt.tables["/user"])
        await mgr.reconcile()
        assert sess["id"] not in {str(k) for k in mgr.listeners}

        # Ablauf durch Worker-Job
        async with system_session() as db:
            await db.execute(update(RemoteSession).values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
            await db.commit()
        await expire_sessions()
        lst = (await client.get(f"/api/v1/devices/{dev['id']}/remote-sessions", headers=h)).json()
        assert {x["status"] for x in lst} == {"closed", "expired"}
        await mgr.reconcile()
        assert mgr.listeners == {}
    finally:
        await mgr.close_all()
        srv.close()


async def test_remote_rbac_and_limits(client, msp, hub):
    t = await make_tenant(client, msp)
    ro = await make_tenant_admin(client, msp, t["id"], role="readonly", email="ro@acme.example.com")
    adm = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, adm)
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh"}, headers=ro)
    assert r.status_code == 403
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "duration_minutes": 9999}, headers=adm)
    assert r.status_code == 400
