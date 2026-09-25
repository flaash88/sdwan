"""Fernzugriff: Zustand von /ip service (www/winbox/ssh) nach der letzten Sitzung wiederherstellen."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import update

from app.config import get_settings
from app.db import system_session
from app.models import RemoteSession
from app.routeros.simulator import get_router
from app.services.remote import expire_sessions
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp, monkeypatch):
    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41700-41719")
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    return h, dev, get_router(dev["tunnel_ip"])


def _svc(rt, name):
    return next(s for s in rt.tables["/ip/service"] if s["name"] == name)


async def _open(client, h, dev, protocol="webfig"):
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions",
                          json={"protocol": protocol, "duration_minutes": 15, "allowed_cidr": "198.51.100.7/32"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _close(client, h, sess):
    r = await client.post(f"/api/v1/remote-sessions/{sess['id']}/close", headers=h)
    assert r.status_code == 200, r.text


async def _expire_all():
    async with system_session() as db:
        await db.execute(update(RemoteSession).where(RemoteSession.status == "active")
                         .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
        await db.commit()
    await expire_sessions()


async def test_www_restored_on_close(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    www = _svc(rt, "www")
    www.update({"disabled": "true", "address": "192.168.88.0/24"})
    s = await _open(client, h, dev)
    assert www["disabled"] == "no" and www["address"] == "192.168.88.0/24,10.100.0.1/32"
    await _close(client, h, s)
    assert www["disabled"] == "true" and www["address"] == "192.168.88.0/24"  # exakt der Zustand vorher


async def test_www_restored_on_expiry_after_platform_restart(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    www = _svc(rt, "www")
    www.update({"disabled": "true", "address": ""})
    await _open(client, h, dev)
    # Plattform-Neustart: kein Zustand im Speicher, nur die Datenbank -> Worker-Job räumt auf
    await _expire_all()
    assert www["disabled"] == "true" and www["address"] == ""


async def test_parallel_sessions_restore_after_last(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    www = _svc(rt, "www")
    www.update({"disabled": "true", "address": ""})
    a = await _open(client, h, dev)
    b = await _open(client, h, dev)  # sieht www bereits aktiv, übernimmt aber den Ursprungszustand von a
    await _close(client, h, a)
    assert www["disabled"] == "no"  # b läuft noch
    await _close(client, h, b)
    assert www["disabled"] == "true"
    # umgekehrte Reihenfolge + Ablauf beider gleichzeitig
    await _open(client, h, dev)
    await _open(client, h, dev)
    assert www["disabled"] == "no"
    await _expire_all()
    assert www["disabled"] == "true"


async def test_already_active_service_stays_active(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    www = _svc(rt, "www")
    www.update({"disabled": "false", "address": ""})
    s = await _open(client, h, dev)
    await _close(client, h, s)
    assert www["disabled"] == "false" and www["address"] == ""


async def test_winbox_and_ssh_restored_too(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    winbox, ssh = _svc(rt, "winbox"), _svc(rt, "ssh")
    winbox.update({"disabled": "true", "address": ""})
    ssh.update({"disabled": "false", "address": "192.168.88.0/24"})
    w = await _open(client, h, dev, "winbox")
    s = await _open(client, h, dev, "ssh")
    assert winbox["disabled"] == "no" and ssh["address"] == "192.168.88.0/24,10.100.0.1/32"
    await _close(client, h, w)
    await _close(client, h, s)
    assert winbox["disabled"] == "true" and ssh["address"] == "192.168.88.0/24"


async def test_failed_session_and_offline_router_restore_later(client, msp, hub, monkeypatch):
    h, dev, rt = await _setup(client, msp, monkeypatch)
    www = _svc(rt, "www")
    www.update({"disabled": "true", "address": ""})
    # Sitzung scheitert nach dem Einschalten (Gruppe abgelehnt) -> Worker stellt zurück
    rt.fail_next.add("/user/group/add")
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions",
                          json={"protocol": "webfig", "duration_minutes": 15, "allowed_cidr": "198.51.100.7/32"}, headers=h)
    assert r.status_code == 502
    assert www["disabled"] == "no"
    await expire_sessions()
    assert www["disabled"] == "true"
    # Router beim Schließen offline -> später erneut versucht
    s = await _open(client, h, dev)
    rt.offline = True
    await _close(client, h, s)
    assert www["disabled"] == "no"
    rt.offline = False
    await expire_sessions()
    assert www["disabled"] == "true"
