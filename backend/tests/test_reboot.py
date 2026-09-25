"""Neustart über die Plattform + Unterdrückung des Offline-Alarms für 5 Minuten."""

from __future__ import annotations

import datetime as dt
import uuid

from app.db import system_session
from app.models import Device
from app.routeros.simulator import get_router
from app.services.alerts import evaluate_all
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="lab-l009")
    await client.post("/api/v1/alert-rules", json={"name": "offline", "type": "device_offline", "severity": "critical", "duration_s": 0}, headers=h)
    return h, dev


async def _shift_reboot(dev_id: str, **delta):
    async with system_session() as db:
        d = await db.get(Device, uuid.UUID(dev_id))
        info = dict(d.facts["reboot"])
        for k, v in delta.items():
            info[k] = (dt.datetime.fromisoformat(info[k]) + v).isoformat()
        d.facts = {**d.facts, "reboot": info}
        await db.commit()


async def test_reboot_sets_marker_audit_and_clears_after_return(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    boot_before = rt.boot
    r = await client.post(f"/api/v1/devices/{dev['id']}/reboot", headers=h)
    assert r.status_code == 200, r.text
    assert rt.boot > boot_before  # /system/reboot ausgeführt
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert d["facts"]["reboot"]["by"] == "admin@acme.example.com"
    audit = (await client.get("/api/v1/audit?action=device.reboot", headers=h)).json()
    assert audit and audit[0]["success"] is True
    # Router antwortet wieder mit kleiner Uptime -> Markierung weg
    await _shift_reboot(dev["id"], at=dt.timedelta(seconds=-30))
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert "reboot" not in d["facts"] and d["status"] == "online"


async def test_offline_alarm_suppressed_during_reboot(client, msp, hub):
    h, dev = await _setup(client, msp)
    await client.post(f"/api/v1/devices/{dev['id']}/reboot", headers=h)
    get_router(dev["tunnel_ip"]).offline = True
    await poll_all()
    await poll_all()
    assert (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()["status"] == "offline"
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []  # unterdrückt


async def test_device_not_returning_alarms_after_five_minutes(client, msp, hub):
    h, dev = await _setup(client, msp)
    await client.post(f"/api/v1/devices/{dev['id']}/reboot", headers=h)
    get_router(dev["tunnel_ip"]).offline = True
    await poll_all()
    await poll_all()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    # 5 Minuten später: Gerät ist nicht zurückgekommen -> normaler Offline-Alarm
    await _shift_reboot(dev["id"], at=dt.timedelta(minutes=-6), until=dt.timedelta(minutes=-6))
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert len(alerts) == 1 and alerts[0]["status"] == "firing" and alerts[0]["severity"] == "critical"
    await poll_all()  # Markierung läuft ab, auch ohne Rückkehr
    assert "reboot" not in (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()["facts"]


async def test_reboot_rbac_and_unpaired(client, msp, hub):
    h, dev = await _setup(client, msp)
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@acme.example.com", role="readonly")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/reboot", headers=ro)).status_code == 403
    new = (await client.post("/api/v1/devices", json={"name": "neu"}, headers=h)).json()
    nid = new["device"]["id"] if "device" in new else new["id"]
    assert (await client.post(f"/api/v1/devices/{nid}/reboot", headers=h)).status_code == 409
