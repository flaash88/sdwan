"""Phase 12 – Branding (MikroTik-Fleet-Management) und HTML-Mail-Layout."""

from __future__ import annotations

import datetime as dt
from urllib.parse import unquote

import pytest
from sqlalchemy import update

from app.config import get_settings
from app.db import system_session
from app.models import Alert, Device, DeviceStatus
from app.routeros.simulator import get_router
from app.services import mailer
from app.services.alerts import TYPES, evaluate_all
from app.services.mail_render import fmt_duration, fmt_local, render_alert, sample_context
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant_admin


def _parts(msg):
    """-> (text, html, attachments) einer EmailMessage."""
    body_text = msg.get_body(("plain",))
    body_html = msg.get_body(("html",))
    return (body_text.get_content() if body_text else None, body_html.get_content() if body_html else None, list(msg.iter_attachments()))


@pytest.fixture
def branding():
    s = get_settings()
    old = (s.product_name, s.product_short, s.mail_footer_text, s.mail_logo_url, s.mail_subject_emoji, s.mail_accent_color)
    yield s
    s.product_name, s.product_short, s.mail_footer_text, s.mail_logo_url, s.mail_subject_emoji, s.mail_accent_color = old


def test_duration_and_timezone_formatting():
    assert [fmt_duration(x) for x in (0, 59, 60, 24 * 60 + 30, 3600, 3600 + 5 * 60, 86400, 3 * 86400 + 4 * 3600)] == \
        ["0 s", "59 s", "1 min", "24 min", "1 h", "1 h 5 min", "1 d", "3 d 4 h"]
    summer = dt.datetime(2026, 9, 25, 9, 39, tzinfo=dt.UTC)
    winter = dt.datetime(2026, 1, 15, 9, 39, tzinfo=dt.UTC)
    assert fmt_local(summer, "Europe/Vienna") == "25.09.2026, 11:39 Uhr"  # MESZ = UTC+2
    assert fmt_local(winter, "Europe/Vienna") == "15.01.2026, 10:39 Uhr"  # MEZ = UTC+1
    assert fmt_local(summer, "America/New_York") == "25.09.2026, 05:39 Uhr"
    assert fmt_local(summer.replace(tzinfo=None), "Europe/Vienna") == "25.09.2026, 11:39 Uhr"  # naive = UTC
    assert fmt_local(summer, "Kein/Ort") == "25.09.2026, 11:39 Uhr"  # ungültig -> Default
    assert fmt_local(None, "Europe/Vienna") == "–"


@pytest.mark.parametrize("typ", list(TYPES))
@pytest.mark.parametrize("resolved", [False, True])
def test_render_every_alert_type(typ, resolved, branding):
    subject, text, html = render_alert(sample_context(typ, resolved))
    assert subject.startswith("[MFM] ")
    if resolved:
        assert "✅ BEHOBEN | Gutshof – routerboard: " in subject and subject.endswith("(Dauer 24 min)")
        assert "BEHOBEN" in html and "#16a34a" in html and "war 24 min aktiv" in html and "Empfohlene" not in html
    else:
        assert " | Gutshof – routerboard: " in subject and "AUSGELÖST" in html and "Empfohlene nächste Schritte" in html
    assert TYPES[typ] in html  # Alarmtyp im Klartext im Balken
    assert "Standort Gutshof – routerboard" in html
    for label in ("Mandant", "Standort", "Gerät", "Regel", "Schweregrad", "Beginn", "Dauer"):
        assert label in html
    assert ("Ende" in html) == resolved
    assert "L009UiGS-RM · RouterOS 7.19.4" in html
    assert "Gerät öffnen" in html and "Alarm quittieren" in html and "/alerts" in html
    # Outlook-tauglich: Tabellen, Inline-CSS, 600 px, kein Script/externe Fonts/Style-Block
    assert 'width="600"' in html and "<script" not in html.lower() and "<style" not in html.lower() and "fonts.googleapis" not in html
    assert "MikroTik-Fleet-Management" in html
    # Klartext-Fallback mit denselben Kerninformationen
    assert "Gutshof" in text and "routerboard" in text and "Alarm quittieren" in text and "<" not in text.replace("<-", "")


def test_type_specific_blocks():
    html = render_alert(sample_context("vrrp_master", False))[2]
    assert "192.168.110.1/32" in html and "110" in html and "Master seit" in html and "WAN1 Glasfaser-Core" in html
    assert "Glasfaser-Strecke und FortiGate prüfen" in html
    html = render_alert(sample_context("wan_down", False))[2]
    assert "WAN-Links des Geräts" in html and "ether8" in html and "Verlust" in html
    html = render_alert(sample_context("cpu_high", False))[2]
    assert "97 %" in html and "Schwelle" in html
    html = render_alert(sample_context("device_offline", False))[2]
    assert "Zuletzt gesehen" in html and "Management-RTT" in html and "routerboard ist offline" in html
    assert render_alert(sample_context("device_offline", False))[0].endswith("routerboard: offline")
    html = render_alert(sample_context("mesh_down", False))[2]
    assert "Gegenstelle" in html and "zentrale-01" in html


def test_subject_severity_emoji_and_branding(branding):
    assert "🟠 WARNUNG |" in render_alert(sample_context("wan_down", False))[0]
    assert "🔴 KRITISCH |" in render_alert(sample_context("device_offline", False))[0]
    branding.mail_subject_emoji = False
    branding.product_short, branding.product_name = "NW", "Netzwarte Fleet"
    branding.mail_footer_text, branding.mail_logo_url = "Netzwarte GmbH · Support +43 1 234", "https://cdn.example.com/logo.png"
    subject, text, html = render_alert(sample_context("wan_down", False))
    assert subject.startswith("[NW] WARNUNG | Gutshof") and "🟠" not in subject
    assert "Netzwarte Fleet" in html and "Netzwarte GmbH · Support +43 1 234" in html and 'src="https://cdn.example.com/logo.png"' in html
    assert "Netzwarte GmbH" in text
    branding.mail_logo_url = ""
    assert "<img" not in render_alert(sample_context("wan_down", False))[2]


def test_html_escaping():
    ctx = sample_context("wan_down", False)
    ctx["tenant"] = "<b>Böse</b>"
    html = render_alert(ctx)[2]
    assert "<b>Böse</b>" not in html and "&lt;b&gt;" in html


async def test_meta_exposes_branding(client, branding):
    branding.product_name, branding.product_short = "Netzwarte Fleet", "NW"
    r = (await client.get("/api/v1/meta")).json()
    assert r["product_name"] == "Netzwarte Fleet" and r["product_short"] == "NW"


async def test_real_alert_mail_multipart(client, msp, hub):
    mailer.outbox.clear()
    t = (await client.post("/api/v1/tenants", json={"name": "Gut", "slug": "gut", "contact_email": "noc@gut.example.com", "timezone": "Europe/Vienna"},
                           headers=msp)).json()
    assert t["timezone"] == "Europe/Vienna"
    assert (await client.post("/api/v1/tenants", json={"name": "X", "slug": "x-y", "timezone": "Mars/Base"}, headers=msp)).status_code == 422
    h = await make_tenant_admin(client, msp, t["id"], email="a@gut.example.com")
    site = (await client.post("/api/v1/sites", json={"name": "Gutshof"}, headers=h)).json()
    dev = await make_paired_device(client, h, site_id=site["id"], name="routerboard")
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"links": [
        {"name": "Glasfaser", "interface": "ether1", "gateway": "100.64.0.1", "check_target": "1.1.1.1"}]}, headers=h)
    await client.post("/api/v1/alert-rules", json={"name": "WAN sofort", "type": "wan_down", "severity": "critical", "duration_s": 0}, headers=h)
    await poll_all()
    get_router(dev["tunnel_ip"]).down_hosts.add("1.1.1.1")
    await poll_all()
    await evaluate_all()
    msg = mailer.outbox[-1]
    assert msg["Subject"] == "[MFM] 🔴 KRITISCH | Gutshof – routerboard: WAN Glasfaser ausgefallen"
    assert msg.get_content_type() == "multipart/alternative"
    text, html, att = _parts(msg)
    assert text and html and not att
    assert "Standort Gutshof – routerboard: WAN Glasfaser ausgefallen" in html
    assert "WAN1 Glasfaser" in html and "down" in html and "Uhr" in html and "RouterOS" in html
    # Beginn in Wiener Zeit
    async with system_session() as db:
        from sqlalchemy import select

        a = (await db.execute(select(Alert))).scalar_one()
        assert fmt_local(a.started_at, "Europe/Vienna") in html

    # Zeitzone ändern -> andere Uhrzeit in der Entwarnung
    await client.patch(f"/api/v1/tenants/{t['id']}", json={"timezone": "America/New_York"}, headers=msp)
    get_router(dev["tunnel_ip"]).down_hosts.clear()
    await poll_all()
    await evaluate_all()
    msg = mailer.outbox[-1]
    assert msg["Subject"].startswith("[MFM] ✅ BEHOBEN | Gutshof – routerboard: WAN Glasfaser ausgefallen (Dauer ")
    text, html, _ = _parts(msg)
    async with system_session() as db:
        a = (await db.execute(select(Alert))).scalar_one()
        assert fmt_local(a.resolved_at, "America/New_York") in html and "Ende" in html


async def test_offline_mail_context(client, msp, hub):
    mailer.outbox.clear()
    t = (await client.post("/api/v1/tenants", json={"name": "Off", "slug": "off", "contact_email": "noc@off.example.com"}, headers=msp)).json()
    h = await make_tenant_admin(client, msp, t["id"], email="a@off.example.com")
    await make_paired_device(client, h)
    await client.post("/api/v1/alert-rules", json={"name": "offline", "type": "device_offline", "severity": "critical", "duration_s": 0}, headers=h)
    async with system_session() as db:
        await db.execute(update(Device).values(status=DeviceStatus.offline, last_seen_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
                                               facts={"mgmt_rtt_ms": 23.4}))
        await db.commit()
    await evaluate_all()
    msg = mailer.outbox[-1]
    assert msg["Subject"] == "[MFM] 🔴 KRITISCH | rtr1: offline"
    _, html, _ = _parts(msg)
    assert "Zuletzt gesehen" in html and "23 ms" in html and "Stromversorgung" in html


async def test_test_mail_and_sla_mail_layout(client, msp, hub):
    mailer.outbox.clear()
    t = (await client.post("/api/v1/tenants", json={"name": "Beta", "slug": "beta", "contact_email": "chef@beta.example.com"}, headers=msp)).json()
    h = await make_tenant_admin(client, msp, t["id"], email="a@beta.example.com")
    await make_paired_device(client, h)
    rule = (await client.post("/api/v1/alert-rules", json={"name": "Offline", "type": "device_offline"}, headers=h)).json()
    await client.post(f"/api/v1/alert-rules/{rule['id']}/test", headers=h)
    msg = mailer.outbox[-1]
    assert msg["Subject"] == "[MFM] 🧪 TEST | Offline"
    text, html, _ = _parts(msg)
    assert "Test-Benachrichtigung" in text and "TEST" in html and "MikroTik-Fleet-Management" in html

    today = dt.date.today()
    r = await client.post(f"/api/v1/reports?start={(today - dt.timedelta(days=2)).isoformat()}&end={today.isoformat()}&send=true", headers=h)
    assert r.status_code == 201, r.text
    msg = mailer.outbox[-1]
    assert msg["Subject"].startswith("[MFM] SLA-Bericht Beta")
    text, html, att = _parts(msg)
    assert "Verfügbarkeit je Gerät" in html and "rtr1" in html and "rtr1" in text
    assert att and att[0].get_filename().endswith(".pdf")
    from tests.test_phase11_vrrp import _pdf_text

    assert "MikroTik-Fleet-Management" in _pdf_text(att[0].get_content())


async def test_mail_preview_endpoint(client, msp, hub):
    r = await client.get("/api/v1/alerts/mail-preview?type=vrrp_master&state=firing", headers=msp)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "vrrp-kassen" in r.text and "AUSGELÖST" in r.text
    assert unquote(r.headers["x-mail-subject"]).startswith("[MFM] 🟠 WARNUNG |")
    r = await client.get("/api/v1/alerts/mail-preview?type=vrrp_master&state=resolved&format=text", headers=msp)
    assert r.text.startswith("Betreff: [MFM] ✅ BEHOBEN") and "BEHOBEN" in r.text
    assert (await client.get("/api/v1/alerts/mail-preview?type=nope", headers=msp)).status_code == 400
    from tests.conftest import make_tenant

    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    assert (await client.get("/api/v1/alerts/mail-preview?type=vrrp_master", headers=h)).status_code == 403
