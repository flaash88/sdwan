from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import update

from app.db import system_session
from app.models import Alert, Device, DeviceStatus, StatusEvent
from app.routeros.simulator import get_router
from app.services import mailer
from app.services.alerts import evaluate_all
from app.services.poller import poll_all
from app.services.sla import availability, previous_month
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

UTC = dt.UTC


def T(h: float) -> dt.datetime:
    return dt.datetime(2026, 1, 1, tzinfo=UTC) + dt.timedelta(hours=h)


def test_availability_math():
    # 10 h Zeitraum, 1 h offline in der Mitte
    a = availability([(T(4), "offline"), (T(5), "online")], T(0), T(10), "online")
    assert a["availability_pct"] == 90.0 and a["outage_count"] == 1 and a["downtime_s"] == 3600
    # andauernder Ausfall am Ende, unbekannter Beginn zählt nicht
    a = availability([(T(2), "online"), (T(8), "offline")], T(0), T(10), None)
    assert a["measured_s"] == 8 * 3600 and a["availability_pct"] == 75.0 and a["outages"][0]["end"] is None
    # keine Daten
    assert availability([], T(0), T(1), None)["availability_pct"] is None
    s, e = previous_month(dt.datetime(2026, 3, 15, tzinfo=UTC))
    assert (s, e) == (dt.datetime(2026, 2, 1, tzinfo=UTC), dt.datetime(2026, 3, 1, tzinfo=UTC))


async def test_alert_lifecycle_with_email(client, msp, hub):
    mailer.outbox.clear()
    t = (await client.post("/api/v1/tenants", json={"name": "Acme", "slug": "acme", "contact_email": "noc@acme.example.com"}, headers=msp)).json()
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": [
        {"name": "Glasfaser", "interface": "ether1", "gateway": "100.64.0.1", "check_target": "1.1.1.1"}]}, headers=h)
    r = await client.post("/api/v1/alert-rules/defaults", headers=h)
    assert r.status_code == 201 and len(r.json()) == 4
    rule = (await client.post("/api/v1/alert-rules", json={"name": "WAN sofort", "type": "wan_down", "severity": "critical", "duration_s": 0, "recipients": ["tech@acme.example.com"]}, headers=h)).json()
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []

    # WAN fällt aus -> sofortiger Alert + Mail
    get_router(dev["tunnel_ip"]).down_hosts.add("1.1.1.1")
    await poll_all()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert len(alerts) == 1 and alerts[0]["severity"] == "critical" and "Glasfaser" in alerts[0]["message"]
    assert alerts[0]["notified"] is True
    mail = mailer.outbox[-1]
    assert mail["To"] == "tech@acme.example.com" and "[CRITICAL]" in mail["Subject"]
    # Default-Regel (120 s Verzögerung) noch pending -> nicht sichtbar
    assert len(alerts) == 1
    r = await client.post(f"/api/v1/alerts/{alerts[0]['id']}/ack", headers=h)
    assert r.json()["acknowledged_by"] == "admin@acme.example.com"

    # Wiederherstellung -> resolved + Entwarnung
    get_router(dev["tunnel_ip"]).down_hosts.clear()
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    hist = (await client.get("/api/v1/alerts?state=resolved", headers=h)).json()
    assert len(hist) == 1 and hist[0]["resolved_at"]
    assert "[BEHOBEN]" in mailer.outbox[-1]["Subject"]
    # Pending-Alert der Default-Regel wurde verworfen
    async with system_session() as db:
        from sqlalchemy import select

        assert not (await db.execute(select(Alert).where(Alert.status == "pending"))).scalars().all()
    assert rule["id"]


async def test_device_offline_alert_after_duration(client, msp, hub):
    mailer.outbox.clear()
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    await client.post("/api/v1/alert-rules", json={"name": "offline", "type": "device_offline", "duration_s": 300, "recipients": ["a@b.example.com"]}, headers=h)
    async with system_session() as db:
        await db.execute(update(Device).values(status=DeviceStatus.offline, last_seen_at=dt.datetime.now(UTC) - dt.timedelta(minutes=2)))
        await db.commit()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []  # erst 2 min offline
    async with system_session() as db:
        await db.execute(update(Alert).values(started_at=dt.datetime.now(UTC) - dt.timedelta(minutes=10)))
        await db.commit()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert len(alerts) == 1 and alerts[0]["device"] == dev["name"]


async def test_sla_report_json_and_pdf(client, msp, hub):
    mailer.outbox.clear()
    t = (await client.post("/api/v1/tenants", json={"name": "Beta", "slug": "beta", "contact_email": "chef@beta.example.com"}, headers=msp)).json()
    h = await make_tenant_admin(client, msp, t["id"], email="a@beta.example.com")
    site = (await client.post("/api/v1/sites", json={"name": "HQ"}, headers=h)).json()
    dev = await make_paired_device(client, h, site_id=site["id"])
    base = dt.datetime.combine(dt.date.today() - dt.timedelta(days=3), dt.time(), UTC)
    async with system_session() as db:
        for hours, st in ((0, "online"), (24, "offline"), (30, "online")):
            db.add(StatusEvent(tenant_id=uuid.UUID(t["id"]), device_id=uuid.UUID(dev["id"]), subject="device", status=st, at=base + dt.timedelta(hours=hours)))
        await db.commit()
    start = base.date().isoformat()
    end = (base + dt.timedelta(days=1)).date().isoformat()  # 48 h inkl. Endtag
    rep = (await client.get(f"/api/v1/reports/sla?start={start}&end={end}", headers=h)).json()
    d = rep["devices"][0]
    assert d["site"] == "HQ" and d["outage_count"] == 1 and d["downtime_s"] == 6 * 3600
    assert d["availability_pct"] == round(100 * 42 / 48, 3)
    r = await client.get(f"/api/v1/reports/sla?start={start}&end={end}&format=pdf", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    # Gespeicherter Bericht + Versand an Kontakt
    r = await client.post(f"/api/v1/reports?start={start}&end={end}&send=true", headers=h)
    assert r.json()["sent_to"] == ["chef@beta.example.com"]
    att = list(mailer.outbox[-1].iter_attachments())
    assert att and att[0].get_filename().endswith(".pdf")
    lst = (await client.get("/api/v1/reports", headers=h)).json()
    assert len(lst["reports"]) == 1
    pdf = await client.get(f"/api/v1/reports/{lst['reports'][0]['id']}/pdf", headers=h)
    assert pdf.content.startswith(b"%PDF")


async def test_status_events_recorded_by_poller(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    await make_paired_device(client, h)
    await poll_all()
    async with system_session() as db:
        from sqlalchemy import select

        ev = (await db.execute(select(StatusEvent))).scalars().all()
        assert [e.status for e in ev] == ["online"]
