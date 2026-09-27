"""Phase 18 – Wartungsfenster (Alarmunterdrückung, Firmware nur im Fenster), Speedtest je WAN, zentrales Syslog."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import update

from app.config import get_settings
from app.db import system_session
from app.models import MaintenanceWindow, SyslogMessage
from app.routeros.simulator import get_router
from app.services.alerts import evaluate_all
from app.services.firmware import firmware_tick
from app.services.maintenance import is_active
from app.services.poller import poll_all
from app.services.syslog import DeviceMap, parse, purge_old, store
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def _w(**kw):
    return MaintenanceWindow(tenant_id=None, name="w", enabled=True, suppress_alerts=True, firmware_allowed=True, **kw)


def test_window_math_timezone_and_midnight():
    # Samstag 22:00 Wien, 4 h -> bis Sonntag 02:00
    w = _w(kind="weekly", weekdays=[5], start_time="22:00", duration_min=240)
    tz = "Europe/Vienna"
    sat_2230 = dt.datetime(2026, 9, 26, 20, 30, tzinfo=dt.UTC)  # 22:30 MESZ
    sun_0130 = dt.datetime(2026, 9, 26, 23, 30, tzinfo=dt.UTC)  # 01:30 MESZ
    sun_0230 = dt.datetime(2026, 9, 27, 0, 30, tzinfo=dt.UTC)  # 02:30 MESZ
    assert is_active(w, sat_2230, tz) and is_active(w, sun_0130, tz) and not is_active(w, sun_0230, tz)
    once = _w(kind="once", start_at=dt.datetime(2026, 10, 1, 8, tzinfo=dt.UTC), duration_min=30)
    assert is_active(once, dt.datetime(2026, 10, 1, 8, 10, tzinfo=dt.UTC), tz)
    assert not is_active(once, dt.datetime(2026, 10, 1, 8, 31, tzinfo=dt.UTC), tz)


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="op1")
    return h, dev


async def test_alert_suppressed_during_window_then_fires(client, msp, hub):
    h, dev = await _setup(client, msp)
    await client.post("/api/v1/alert-rules", json={"name": "off", "type": "device_offline", "severity": "critical", "duration_s": 0}, headers=h)
    now = dt.datetime.now(dt.UTC)
    r = await client.post("/api/v1/maintenance-windows", json={"name": "Umbau", "device_id": dev["id"], "kind": "once",
                                                                "start_at": (now - dt.timedelta(minutes=5)).isoformat(), "duration_min": 60}, headers=h)
    assert r.status_code == 201 and r.json()["active"]
    get_router(dev["tunnel_ip"]).offline = True
    await poll_all()
    await poll_all()
    await evaluate_all()
    (a,) = (await client.get("/api/v1/alerts", headers=h)).json()
    assert a["status"] == "pending" and a["suppressed_reason"] == "maintenance:Umbau"
    assert (await client.get(f"/api/v1/devices/{dev['id']}/maintenance", headers=h)).json() == {"active": True, "window": "Umbau"}
    # Fenster vorbei -> normale Alarmierung
    async with system_session() as db:
        await db.execute(update(MaintenanceWindow).values(start_at=now - dt.timedelta(hours=3)))
        await db.commit()
    await evaluate_all()
    (a,) = (await client.get("/api/v1/alerts", headers=h)).json()
    assert a["status"] == "firing" and a["suppressed_reason"] is None
    bad = await client.post("/api/v1/maintenance-windows", json={"name": "x", "kind": "weekly", "weekdays": [9], "start_time": "25:00"}, headers=h)
    assert bad.status_code == 422


async def test_firmware_only_in_window(client, msp, hub):
    h, dev = await _setup(client, msp)
    await poll_all()
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [dev["id"]], "batch_size": 1, "batch_interval_s": 0, "only_in_window": True}, headers=h)
    job = r.json()
    await firmware_tick()
    j = (await client.get(f"/api/v1/firmware/jobs/{job['id']}", headers=h)).json()
    assert j["items"][0]["status"] == "queued" and j["items"][0]["error"] == "wartet auf Wartungsfenster"
    now = dt.datetime.now(dt.UTC)
    await client.post("/api/v1/maintenance-windows", json={"name": "Nacht", "kind": "once", "start_at": (now - dt.timedelta(minutes=1)).isoformat(),
                                                           "duration_min": 120, "suppress_alerts": False}, headers=h)
    await firmware_tick()
    j = (await client.get(f"/api/v1/firmware/jobs/{job['id']}", headers=h)).json()
    assert j["items"][0]["status"] != "queued"


async def test_speedtest_per_wan_route_and_volume_warning(client, msp, hub, monkeypatch):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": [
        {"name": "Glasfaser", "interface": "ether1", "gateway": "192.0.2.1", "check_target": "1.1.1.1"},
        {"name": "5G", "interface": "ether8", "gateway": "198.51.100.1", "check_target": "9.9.9.9", "priority": 2, "monthly_limit_gb": 50}]}, headers=h)
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    fiber, lte = links[0]["id"], links[1]["id"]
    # ohne Server deaktiviert
    assert (await client.post(f"/api/v1/devices/{dev['id']}/wan/{fiber}/speedtest", json={}, headers=h)).status_code == 409
    monkeypatch.setattr(get_settings(), "speedtest_server", "203.0.113.50")
    r = await client.post(f"/api/v1/devices/{dev['id']}/wan/{fiber}/speedtest", json={}, headers=h)
    assert r.status_code == 202, r.text
    res = (await client.get(f"/api/v1/devices/{dev['id']}/speedtests", headers=h)).json()["results"]
    assert len(res) == 1 and res[0]["status"] == "ok" and res[0]["down_mbps"] == 87.3 and res[0]["up_mbps"] == 21.4
    call = rt.btest_log[-1]
    assert call["address"] == "203.0.113.50" and call["direction"] == "transmit"
    assert not [x for x in rt.tables["/ip/route"] if str(x.get("comment", "")).startswith("sdwan:speedtest")]  # Route wieder entfernt
    # WAN mit Volumenlimit: Warnung und Bestätigung
    est = (await client.get(f"/api/v1/devices/{dev['id']}/wan/{lte}/speedtest/estimate", headers=h)).json()
    assert est["volume_warning"] and "50 GB" in est["volume_warning"]
    r = await client.post(f"/api/v1/devices/{dev['id']}/wan/{lte}/speedtest", json={}, headers=h)
    assert r.status_code == 409 and "Volumenlimit" in r.text
    assert (await client.post(f"/api/v1/devices/{dev['id']}/wan/{lte}/speedtest", json={"confirm_volume": True}, headers=h)).status_code == 202
    assert (await client.put(f"/api/v1/devices/{dev['id']}/wan/{fiber}/speedtest/schedule", json={"weekly": True}, headers=h)).json()["weekly"]


def test_syslog_parse():
    p = parse(b"<30>Sep 26 12:00:01 fil-01 system,info,account user admin logged in from 10.100.0.1 via ssh")
    assert p["severity"] == 6 and p["facility"] == 3 and p["topics"] == "system,info,account"
    assert p["message"].startswith("user admin logged in")
    p = parse(b"<28>firewall,info input: in:ether1 out:(unknown 0)")
    assert p["topics"] == "firewall,info" and p["severity"] == 4
    assert parse(b"kaputte zeile ohne alles")["message"]


async def test_syslog_config_store_query_and_retention(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    r = await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": True, "topics": ["critical", "error", "warning", "firewall"]}, headers=h)
    assert r.status_code == 200 and r.json()["last_error"] is None
    (act,) = [a for a in rt.tables["/system/logging/action"] if a["name"] == "sdwansyslog"]
    assert act["target"] == "remote" and act["remote"] == "10.100.0.1" and act["src-address"] == dev["tunnel_ip"]
    assert sorted(x["topics"] for x in rt.tables["/system/logging"] if x.get("action") == "sdwansyslog") == ["critical", "error", "firewall", "warning"]
    assert len([x for x in rt.tables["/system/logging"] if x.get("action") == "memory"]) == 4  # Standardregeln unberührt
    # Empfang
    async with system_session() as db:
        dmap = DeviceMap()
        await dmap.refresh(db)
        now = dt.datetime.now(dt.UTC)
        n = await store(db, dmap, [(dev["tunnel_ip"], b"<28>firewall,info drop input: src-address=203.0.113.9", now),
                                   ("10.100.99.99", b"<30>fremd", now)])
        await db.commit()
    assert n == 1
    logs = (await client.get(f"/api/v1/devices/{dev['id']}/logs", params={"q": "203.0.113.9"}, headers=h)).json()["messages"]
    assert logs[0]["topics"] == "firewall,info"
    around = (await client.get(f"/api/v1/devices/{dev['id']}/logs", params={"around": now.isoformat()}, headers=h)).json()
    assert len(around["messages"]) == 1
    # Aufbewahrung
    await client.put("/api/v1/syslog/retention", json={"days": 1}, headers=h)
    async with system_session() as db:
        await db.execute(update(SyslogMessage).values(received_at=now - dt.timedelta(days=2)))
        await db.commit()
    assert await purge_old() == 1
    # Abschalten entfernt nur die eigenen Einträge
    await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": False}, headers=h)
    assert not [a for a in rt.tables["/system/logging/action"] if a["name"] == "sdwansyslog"]
    assert not [x for x in rt.tables["/system/logging"] if x.get("action") == "sdwansyslog"]
    assert len(rt.tables["/system/logging/action"]) == 4


async def test_syslog_action_name_rule_and_legacy_migration(client, msp, hub):
    """Hardware-Fund: RouterOS lehnt Aktionsnamen mit '-' ab. Simulator bildet die Regel nach; alte Aktion wird migriert."""
    from app.routeros import RouterOSError

    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    try:  # Fehler reproduziert (früherer Name)
        rt.call("/system/logging/action/add", {"name": "sdwan-syslog", "target": "remote", "remote": "10.100.0.1"})
        raise AssertionError("Simulator hätte ablehnen müssen")
    except RouterOSError as exc:
        assert "action name can contain only letters and numbers" in str(exc)
    # alter Zustand (z. B. aus einer früheren Version): Aktion mit altem Namen + Regel; dazu eine fremde gleichnamige Aktion?
    rt._insert("/system/logging/action", {"name": "sdwan-syslog", "target": "remote", "remote": "10.100.0.1", "remote-port": "514"})
    rt._insert("/system/logging", {"topics": "error", "action": "sdwan-syslog"})
    rt._insert("/system/logging/action", {"name": "kundensyslog", "target": "remote", "remote": "192.0.2.50"})
    r = await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": True, "topics": ["error"]}, headers=h)
    assert r.status_code == 200 and r.json()["last_error"] is None, r.text
    names = [a["name"] for a in rt.tables["/system/logging/action"]]
    assert "sdwan-syslog" not in names and "sdwansyslog" in names and "kundensyslog" in names
    assert not [x for x in rt.tables["/system/logging"] if x.get("action") == "sdwan-syslog"]
    (act,) = [a for a in rt.tables["/system/logging/action"] if a["name"] == "sdwansyslog"]
    assert act.get("comment") == "sdwan:syslog"  # Simulator akzeptiert comment (ANNAHME Labor)
    # gleichnamige Aktion, die NICHT von der Plattform stammt (anderes Ziel), bleibt unangetastet
    await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": False}, headers=h)
    rt._insert("/system/logging/action", {"name": "sdwan-syslog", "target": "remote", "remote": "192.0.2.99"})
    await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": True, "topics": ["error"]}, headers=h)
    assert any(a["name"] == "sdwan-syslog" and a["remote"] == "192.0.2.99" for a in rt.tables["/system/logging/action"])


async def test_syslog_action_without_comment_field(client, msp, hub, monkeypatch):
    """ANNAHME: kennt RouterOS kein comment an Logging-Aktionen, wird ohne angelegt."""
    from app.routeros import RouterOSError

    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    orig = rt.call

    def call(cmd, params):
        if cmd == "/system/logging/action/add" and "comment" in params:
            raise RouterOSError("failure: unknown parameter comment")
        return orig(cmd, params)

    rt.call = call
    r = await client.put(f"/api/v1/devices/{dev['id']}/syslog", json={"enabled": True, "topics": ["error"]}, headers=h)
    assert r.json()["last_error"] is None
    (act,) = [a for a in rt.tables["/system/logging/action"] if a["name"] == "sdwansyslog"]
    assert "comment" not in act


def test_routeros_safe_name():
    from app.routeros.naming import is_valid, routeros_safe_name

    assert routeros_safe_name("sdwansyslog", "logging_action") == "sdwansyslog"
    assert routeros_safe_name("sdwan-syslog", "logging_action") == "sdwansyslog"
    assert routeros_safe_name("sdwan-wifi-büro gast", "generic") == "sdwan-wifi-buero-gast"
    for valid in ("sdwan-zone-lan", "sdwan-r-ab12cd-src", "sdwan-hs-lobby", "sdwan-wan1", "sdwan-local-access"):
        assert routeros_safe_name(valid) == valid and is_valid(valid)  # bestehende Namen bleiben unverändert
