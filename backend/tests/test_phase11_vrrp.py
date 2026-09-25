"""Phase 11 – VRRP & Backup-Transparenz (Kassen-VLAN-Szenario mit zentraler FortiGate)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from app.db import system_session
from app.models import Alert, StatusEvent
from app.routeros.simulator import get_router
from app.services import mailer
from app.services.alerts import evaluate_all
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

# WAN1 = ether2 zur FortiGate (echte IP, nicht die VIP), WAN2 = 5G-Modem an ether8
WAN = [
    {"name": "Glasfaser-Core", "interface": "ether2", "gateway": "192.168.110.2", "priority": 1, "check_target": "1.1.1.1"},
    {"name": "5G", "interface": "ether8", "gateway": "192.168.8.1", "priority": 2, "check_target": "9.9.9.9"},
]
VRRP = {"name": "vrrp-kassen", "interface": "ether2", "vrid": 110, "priority": 100, "vip": "192.168.110.1",
        "local_address": "192.168.110.21/24", "linked_wan_slot": 1}


async def _setup(client, msp, with_wan=True):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="filiale-01")
    if with_wan:
        r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": WAN}, headers=h)
        assert r.json()["push"]["ok"], r.text
    return h, dev


def _vrrp_rows(rt):
    return [r for r in rt.tables["/interface/vrrp"] if str(r.get("comment", "")).startswith("sdwan:vrrp:")]


async def test_vrrp_push_and_idempotency(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt.tables["/interface/vrrp"].append({".id": "*M1", "name": "manual-vrrp", "interface": "ether3", "vrid": "5"})
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["push"]["ok"] is True
    inst = body["instances"][0]
    assert inst["vip"] == "192.168.110.1/32"  # Default-Präfix /32
    (v,) = _vrrp_rows(rt)
    assert (v["name"], v["interface"], v["vrid"], v["priority"], v["interval"], v["preemption-mode"], v["version"]) == \
        ("vrrp-kassen", "ether2", "110", "100", "1s", "yes", "3")
    assert 'disable [find where comment~"^sdwan:wan:default:1"]' in v["on-master"]
    assert 'reply-dst-address~("^" . $ip . ":")' in v["on-master"]  # Verbindungen des WAN-Slots werden geleert
    assert 'netwatch get [find where comment="sdwan:wan:check:1"] status] = "up"' in v["on-backup"]
    addrs = {a["comment"]: a for a in rt.tables["/ip/address"] if str(a.get("comment", "")).startswith("sdwan:vrrp:")}
    assert {(a["address"], a["interface"]) for a in addrs.values()} == {("192.168.110.21/24", "ether2"), ("192.168.110.1/32", "vrrp-kassen")}
    # manuelle Instanz bleibt unangetastet
    assert any(r[".id"] == "*M1" for r in rt.tables["/interface/vrrp"])
    # erneut anwenden -> keine Änderungen
    rep = (await client.post(f"/api/v1/devices/{dev['id']}/vrrp/apply", headers=h)).json()
    assert rep["stats"]["vrrp"] == {"added": 0, "updated": 0, "removed": 0}
    assert rep["stats"]["addresses"] == {"added": 0, "updated": 0, "removed": 0}
    # Instanz entfernen -> VIP, lokale Adresse und Interface weg, manuelle bleibt
    await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": []}, headers=h)
    assert _vrrp_rows(rt) == [] and not [a for a in rt.tables["/ip/address"] if str(a.get("comment", "")).startswith("sdwan:vrrp:")]
    assert any(r[".id"] == "*M1" for r in rt.tables["/interface/vrrp"])
    audit = (await client.get("/api/v1/audit?action=vrrp.", headers=h)).json()
    assert {a["action"] for a in audit} >= {"vrrp.update", "vrrp.apply"}


async def test_vrrp_validation(client, msp, hub):
    h, dev = await _setup(client, msp, with_wan=False)
    for bad, code in (
        ({**VRRP, "vrid": 0}, 422),
        ({**VRRP, "vrid": 256}, 422),
        ({**VRRP, "priority": 255}, 422),
        ({**VRRP, "vip": "192.168.110.1/24"}, 400),
        ({**VRRP, "vip": "10.0.0.1"}, 400),  # nicht im Netz der lokalen Adresse
        ({**VRRP, "local_address": "192.168.110.1/24"}, 400),  # gleich der VIP
        ({**VRRP, "interface": "ether2; /system reset"}, 400),
    ):
        r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [bad]}, headers=h)
        assert r.status_code == code, (bad, r.text)
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP, {**VRRP, "name": "b"}]}, headers=h)
    assert r.status_code == 400  # gleiche VRID auf gleichem Interface
    # ohne lokale Adresse ist jede /32-VIP gültig
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [{**VRRP, "local_address": None}]}, headers=h)
    assert r.status_code == 200
    # Read-Only darf nicht schreiben
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@x.example.com", role="readonly")
    assert (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": []}, headers=ro)).status_code == 403


async def test_vrrp_master_disables_linked_wan_and_state_is_tracked(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    await poll_all()
    st = (await client.get(f"/api/v1/devices/{dev['id']}/vrrp", headers=h)).json()["instances"][0]
    assert st["state"] == "backup"
    wan = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [lk["active"] for lk in wan] == [True, False]

    # Glasfaser fällt aus: FortiGate nicht mehr erreichbar -> MikroTik wird Master
    rt = get_router(dev["tunnel_ip"])
    rt.down_hosts.add("1.1.1.1")
    r = await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    assert r.status_code == 200, r.text
    # on-master hat die WAN1-Default-Route sofort deaktiviert (vor der Netwatch)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "yes"
    await poll_all()
    st = (await client.get(f"/api/v1/devices/{dev['id']}/vrrp", headers=h)).json()["instances"][0]
    assert st["state"] == "master"
    wan = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert [lk["active"] for lk in wan] == [False, True]  # 5G trägt jetzt

    # FortiGate zurück, VRRP wieder Backup: on-backup schaltet nur ein, weil Netwatch "up" meldet
    rt.down_hosts.clear()
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "no"
    await poll_all()
    async with system_session() as db:
        ev = (await db.execute(select(StatusEvent).where(StatusEvent.subject == f"vrrp:{inst['id']}").order_by(StatusEvent.at))).scalars().all()
        assert [e.status for e in ev] == ["backup", "master", "backup"]


async def test_vrrp_on_backup_keeps_routes_off_while_netwatch_down(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    rt = get_router(dev["tunnel_ip"])
    rt.down_hosts.add("1.1.1.1")
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
    # Netwatch noch down -> Route bleibt aus (Hysterese bleibt bei der Netwatch)
    assert next(x for x in rt.tables["/ip/route"] if x.get("comment") == "sdwan:wan:default:1")["disabled"] == "yes"


async def test_vrrp_via_zero_touch(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tpl = {"wan_interface": "ether8", "lan": {"enabled": False},
           "wan": {"mode": "failover", "links": WAN},
           "vrrp": [{k: v for k, v in VRRP.items() if k != "local_address"}]}
    r = await client.post("/api/v1/ztp/templates", json={"name": "Filiale Kassen", "content": tpl}, headers=h)
    assert r.status_code == 201, r.text
    bad = await client.post("/api/v1/ztp/stage", json={"template_id": r.json()["id"], "devices": [
        {"name": "fil-x", "serial": "HX1", "vrrp_local_address": "10.9.9.9/24"}]}, headers=h)
    assert bad.status_code == 422  # VIP nicht im Netz
    staged = (await client.post("/api/v1/ztp/stage", json={"template_id": r.json()["id"], "devices": [
        {"name": "fil-02", "serial": "HX2", "vrrp_local_address": "192.168.110.22/24"}]}, headers=h)).json()
    dev_id = staged[0]["device"]["id"]
    r = await client.post(f"/api/v1/devices/{dev_id}/simulate-pair", headers=h)
    assert r.status_code == 200, r.text
    await poll_all()
    dev = (await client.get(f"/api/v1/devices/{dev_id}", headers=h)).json()
    assert dev["ztp_state"] == "provisioned", dev["ztp_log"]
    inst = (await client.get(f"/api/v1/devices/{dev_id}/vrrp", headers=h)).json()["instances"][0]
    assert inst["local_address"] == "192.168.110.22/24" and inst["linked_wan_slot"] == 1
    assert _vrrp_rows(get_router(dev["tunnel_ip"]))


async def test_alerts_vrrp_master_and_backup_wan(client, msp, hub):
    mailer.outbox.clear()
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    rules = (await client.post("/api/v1/alert-rules/defaults", headers=h)).json()
    vr = next(r for r in rules if r["type"] == "vrrp_master")
    assert vr["severity"] == "warning" and vr["duration_s"] == 30
    await client.post("/api/v1/alert-rules", json={"name": "Backup-WAN", "type": "wan_backup_active", "severity": "info", "duration_s": 0,
                                                   "recipients": ["noc@x.example.com"]}, headers=h)
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []

    rt = get_router(dev["tunnel_ip"])
    rt.down_hosts.add("1.1.1.1")
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    await poll_all()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    by_subject = {a["subject"]: a for a in alerts}
    # Backup-WAN sofort (Regel duration 0), VRRP-Master erst nach 30 s
    wan5g = next(a for s_, a in by_subject.items() if s_.startswith("wanactive:"))
    assert wan5g["status"] == "firing" and "5G" in wan5g["message"]
    vr_alert = by_subject[f"vrrp:{inst['id']}"]
    assert vr_alert["status"] == "pending" and "Master" in vr_alert["message"]
    async with system_session() as db:
        await db.execute(update(Alert).where(Alert.subject == f"vrrp:{inst['id']}").values(started_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
        await db.commit()
    await evaluate_all()
    alerts = {a["subject"]: a for a in (await client.get("/api/v1/alerts", headers=h)).json()}
    assert alerts[f"vrrp:{inst['id']}"]["status"] == "firing"

    # FortiGate zurück -> beide Alarme behoben
    rt.down_hosts.clear()
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    resolved = (await client.get("/api/v1/alerts?state=resolved", headers=h)).json()
    assert len(resolved) == 2
    async with system_session() as db:
        ev = (await db.execute(select(StatusEvent).where(StatusEvent.subject.like("wanactive:%")).order_by(StatusEvent.at))).scalars().all()
        seq: dict[str, list[str]] = {}
        for e in ev:
            seq.setdefault(e.subject, []).append(e.status)
        assert sorted(seq.values()) == [["active", "inactive"], ["active", "inactive", "active"]]


async def test_no_backup_alert_in_loadbalance_mode(client, msp, hub):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "loadbalance_ecmp", "links": WAN}, headers=h)
    await client.post("/api/v1/alert-rules", json={"name": "Backup-WAN", "type": "wan_backup_active", "duration_s": 0}, headers=h)
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []


def test_volume_accounting_math():
    from app.models import WanLink
    from app.services.wan import account_volume

    lk = WanLink(vol_bytes=0)
    now = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.UTC)
    account_volume(lk, {"rx_bytes": 1000, "tx_bytes": 500}, now)
    assert lk.vol_bytes == 0 and lk.vol_month == "2026-09"  # erster Stand = Basis
    account_volume(lk, {"rx_bytes": 3000, "tx_bytes": 1500}, now)
    assert lk.vol_bytes == 3000
    account_volume(lk, {"rx_bytes": 200, "tx_bytes": 100}, now)  # Reboot: Zähler zurückgesetzt
    assert lk.vol_bytes == 3300
    account_volume(lk, {"rx_bytes": 1200, "tx_bytes": 100}, now + dt.timedelta(hours=2))  # Monatswechsel
    assert lk.vol_month == "2026-10" and lk.vol_bytes == 1000
    account_volume(lk, None, now + dt.timedelta(hours=3))  # Interface fehlt -> unverändert
    assert lk.vol_bytes == 1000


async def test_wan_volume_limit_and_alerts(client, msp, hub):
    from app.models import WanLink

    h, dev = await _setup(client, msp)
    links = [dict(WAN[0]), {**WAN[1], "monthly_limit_gb": 1}]
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": links}, headers=h)
    assert r.json()["links"][1]["monthly_limit_gb"] == 1
    await client.post("/api/v1/alert-rules", json={"name": "Volumen", "type": "wan_volume", "duration_s": 0}, headers=h)
    await poll_all()
    await poll_all()
    wan = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    assert wan[1]["vol_bytes"] > 0 and wan[1]["vol_month"] == dt.datetime.now(dt.UTC).strftime("%Y-%m")
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []  # weit unter 80 %

    async with system_session() as db:
        await db.execute(update(WanLink).where(WanLink.slot == 2).values(vol_bytes=int(0.85e9)))
        await db.commit()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert [a["subject"].split(":")[0] for a in alerts] == ["volume80"] and "85 %" in alerts[0]["message"]
    async with system_session() as db:
        await db.execute(update(WanLink).where(WanLink.slot == 2).values(vol_bytes=int(1.2e9)))
        await db.commit()
    await evaluate_all()
    assert sorted(a["subject"].split(":")[0] for a in (await client.get("/api/v1/alerts", headers=h)).json()) == ["volume100", "volume80"]
    # neuer Monat -> Zähler von vorn, Alarme behoben
    async with system_session() as db:
        await db.execute(update(WanLink).where(WanLink.slot == 2).values(vol_month="2000-01"))
        await db.commit()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []


def _pdf_text(pdf: bytes) -> str:
    import re
    import zlib

    import base64

    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        raw = m.group(1).strip()
        try:
            if raw.endswith(b"~>"):  # ReportLab: ASCII85 + Flate
                raw = base64.a85decode(raw[:-2].replace(b"\n", b""))
            out.append(zlib.decompress(raw).decode("latin-1"))
        except (zlib.error, ValueError):
            continue
    return "".join(out)


def test_time_in_state_math():
    from app.services.sla import intervals, union_stats

    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    H = lambda h: t0 + dt.timedelta(hours=h)  # noqa: E731
    # vor dem Zeitraum aktiv, 2 h – dann 3 h Pause – nochmals 1 h, am Ende noch aktiv
    sp = intervals([(H(2), "inactive"), (H(5), "active"), (H(6), "inactive"), (H(9), "active")], H(0), H(10), "active", {"active"})
    assert [(a.hour, b.hour) for a, b in sp] == [(0, 2), (5, 6), (9, 10)]
    assert union_stats(sp) == {"seconds": 4 * 3600, "count": 3}
    # überlappende Phasen zweier Instanzen zählen einmal
    assert union_stats([(H(1), H(3)), (H(2), H(4)), (H(6), H(7))]) == {"seconds": 4 * 3600, "count": 2}
    assert union_stats([]) == {"seconds": 0, "count": 0}


async def test_sla_report_backup_times(client, msp, hub):
    import uuid

    from app.models import VrrpInstance, WanLink

    h, dev = await _setup(client, msp)
    await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)
    base = dt.datetime.combine(dt.date.today() - dt.timedelta(days=3), dt.time(), dt.UTC)
    async with system_session() as db:
        links = {lk.slot: lk for lk in (await db.execute(select(WanLink).where(WanLink.device_id == uuid.UUID(dev["id"])))).scalars()}
        inst = (await db.execute(select(VrrpInstance))).scalar_one()
        tid, did = uuid.UUID(dev["tenant_id"]), uuid.UUID(dev["id"])
        for hours, subj, st in ((0, f"wanactive:{links[1].id}", "active"), (0, f"wanactive:{links[2].id}", "inactive"), (0, f"vrrp:{inst.id}", "backup"),
                                (10, f"vrrp:{inst.id}", "master"), (10, f"wanactive:{links[2].id}", "active"), (10, f"wanactive:{links[1].id}", "inactive"),
                                (12, f"vrrp:{inst.id}", "backup"), (12.5, f"wanactive:{links[2].id}", "inactive"), (12.5, f"wanactive:{links[1].id}", "active"),
                                (30, f"wanactive:{links[2].id}", "active"), (30.25, f"wanactive:{links[2].id}", "inactive")):
            db.add(StatusEvent(tenant_id=tid, device_id=did, subject=subj, status=st, at=base + dt.timedelta(hours=hours)))
        await db.commit()
    start, end = base.date().isoformat(), (base + dt.timedelta(days=1)).date().isoformat()
    d = (await client.get(f"/api/v1/reports/sla?start={start}&end={end}", headers=h)).json()["devices"][0]
    assert d["has_backup_wan"] and d["has_vrrp"]
    assert d["backup_wan_s"] == int(2.75 * 3600) and d["backup_wan_count"] == 2
    assert d["vrrp_master_s"] == 2 * 3600 and d["vrrp_master_count"] == 1
    pdf = (await client.get(f"/api/v1/reports/sla?start={start}&end={end}&format=pdf", headers=h)).content
    assert pdf.startswith(b"%PDF")
    r = (await client.post(f"/api/v1/reports?start={start}&end={end}", headers=h)).json()
    lst = (await client.get("/api/v1/reports", headers=h)).json()["reports"]
    assert lst and r
    async with system_session() as db:
        from app.models import SlaReport

        rep = (await db.execute(select(SlaReport))).scalar_one()
        assert rep.summary["devices"][0]["vrrp_master_s"] == 7200 and rep.summary["devices"][0]["backup_wan_s"] == 9900
        from app.services.sla import _backup_lines, build_report, render_pdf
        from app.models import Tenant

        full = await build_report(db, await db.get(Tenant, tid), base, base + dt.timedelta(days=2))
        text = _pdf_text(render_pdf(full))
        assert "Backup-Betrieb" in text and "2 h 0 min" in text and "2 h 45 min" in text
        assert "Backup-WAN 2 h 45 min (2x), VRRP-Master 2 h 0 min (1x)" in _backup_lines(full)  # Text der Monats-Mail


async def test_alert_webhook_teams_and_generic(client, msp, hub):
    import json

    import httpx

    from app.services import webhook

    received: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append((str(req.url), json.loads(req.content)))
        return httpx.Response(202)

    webhook.transport = httpx.MockTransport(handler)
    try:
        h, dev = await _setup(client, msp)
        inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
        # Validierung: nur https, keine internen Ziele
        for bad in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://10.1.2.3/x", "https://localhost/x", "https://u:p@hooks.example.com/x"):
            r = await client.post("/api/v1/alert-rules", json={"name": "x", "type": "vrrp_master", "webhook_url": bad}, headers=h)
            assert r.status_code == 422, bad
        url = "https://prod.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=GEHEIM"
        teams = (await client.post("/api/v1/alert-rules", json={"name": "VRRP Teams", "type": "vrrp_master", "duration_s": 0,
                                                                "webhook_url": url, "webhook_format": "teams"}, headers=h)).json()
        assert teams["webhook"] == "https://prod.westeurope.logic.azure.com/…" and "GEHEIM" not in json.dumps(teams)
        await client.post("/api/v1/alert-rules", json={"name": "Backup generisch", "type": "wan_backup_active", "duration_s": 0,
                                                       "webhook_url": "https://hooks.example.com/sdwan"}, headers=h)
        audit = (await client.get("/api/v1/audit?action=alert_rule.", headers=h)).json()
        assert audit and "GEHEIM" not in json.dumps(audit)
        # Update ohne webhook_url behält die URL
        r = await client.put(f"/api/v1/alert-rules/{teams['id']}", json={"name": "VRRP Teams", "type": "vrrp_master", "duration_s": 0,
                                                                          "webhook_format": "teams"}, headers=h)
        assert r.json()["webhook"]

        get_router(dev["tunnel_ip"]).down_hosts.add("1.1.1.1")
        await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
        await poll_all()
        await evaluate_all()
        by_url = {u.split("?")[0]: p for u, p in received}
        card = by_url["https://prod.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"]
        assert card["type"] == "message"
        att = card["attachments"][0]
        assert att["contentType"] == "application/vnd.microsoft.card.adaptive" and att["content"]["type"] == "AdaptiveCard"
        assert "Master" in json.dumps(att["content"]) and att["content"]["actions"][0]["type"] == "Action.OpenUrl"
        gen = by_url["https://hooks.example.com/sdwan"]
        assert gen["event"] == "alert.firing" and gen["type"] == "wan_backup_active" and "5G" in gen["text"] and gen["status"] == "firing"
        alerts = (await client.get("/api/v1/alerts", headers=h)).json()
        assert all(a["notified"] for a in alerts)  # ohne E-Mail-Empfänger, aber per Webhook benachrichtigt

        # Entwarnung
        received.clear()
        get_router(dev["tunnel_ip"]).down_hosts.clear()
        await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=false", headers=h)
        await poll_all()
        await evaluate_all()
        assert {p.get("event") for _, p in received} >= {"alert.resolved"}
        # Test-Knopf und Entfernen
        t = (await client.post(f"/api/v1/alert-rules/{teams['id']}/test", headers=h)).json()
        assert t["webhook"] is True
        r = await client.put(f"/api/v1/alert-rules/{teams['id']}", json={"name": "VRRP Teams", "type": "vrrp_master", "webhook_url": ""}, headers=h)
        assert r.json()["webhook"] is None
    finally:
        webhook.transport = None


async def test_dashboard_fleet_state(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    await poll_all()
    st = (await client.get("/api/v1/dashboard/fleet-state", headers=h)).json()
    d = st["devices"][dev["id"]]
    assert d["active_wan"]["slot"] == 1 and d["active_wan"]["backup"] is False and d["vrrp_role"] == "backup"
    assert d["on_backup"] is False and st["sites_on_backup"] == 0
    get_router(dev["tunnel_ip"]).down_hosts.add("1.1.1.1")
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    await poll_all()
    st = (await client.get("/api/v1/dashboard/fleet-state", headers=h)).json()
    d = st["devices"][dev["id"]]
    assert d["active_wan"]["name"] == "5G" and d["active_wan"]["backup"] is True and d["vrrp_role"] == "master"
    assert d["on_backup"] is True and d["backup_since"] and st["sites_on_backup"] == 1


async def test_vrrp_put_without_id_updates_same_name(client, msp, hub):
    h, dev = await _setup(client, msp)
    a = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    r = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [{**VRRP, "priority": 90}]}, headers=h)
    assert r.status_code == 200, r.text
    (b,) = r.json()["instances"]
    assert b["id"] == a["id"] and b["priority"] == 90


async def test_device_events_and_managed_routes(client, msp, hub):
    h, dev = await _setup(client, msp)
    inst = (await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [VRRP]}, headers=h)).json()["instances"][0]
    await poll_all()
    r = (await client.get(f"/api/v1/devices/{dev['id']}/wan/routes", headers=h)).json()
    assert r["live"] is True
    defaults = [x for x in r["routes"] if x["kind"] == "default" and x["routing_table"] == "main"]
    assert [(x["slot"], x["distance"], x["active"]) for x in defaults] == [(1, 1, True), (2, 2, False)]
    assert all(x["comment"].startswith("sdwan:wan:") for x in r["routes"])
    get_router(dev["tunnel_ip"]).down_hosts.add("1.1.1.1")
    await client.post(f"/api/v1/devices/{dev['id']}/vrrp/{inst['id']}/simulate?master=true", headers=h)
    await poll_all()
    ev = (await client.get(f"/api/v1/devices/{dev['id']}/events?prefix=vrrp", headers=h)).json()
    assert [e["status"] for e in ev["events"]] == ["master", "backup"]  # neueste zuerst
    assert ev["events"][0]["label"] == "vrrp-kassen"
    allev = (await client.get(f"/api/v1/devices/{dev['id']}/events", headers=h)).json()
    kinds = {e["kind"] for e in allev["events"]}
    assert {"device", "wan", "wanactive", "vrrp"} <= kinds
    assert any(e["label"].startswith("WAN2 5G") for e in allev["events"] if e["kind"] == "wanactive")
    assert (await client.get(f"/api/v1/devices/{dev['id']}/events?prefix=bogus", headers=h)).status_code == 422
