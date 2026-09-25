"""Firmware-Update: Offline-Alarm während des Update-Neustarts unterdrückt (10 min), danach normal."""

from __future__ import annotations

import datetime as dt

from app.routeros.simulator import get_router
from app.services.alerts import REBOOT_SUPPRESS, evaluate_all
from app.services.firmware import firmware_tick
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin
from tests.test_reboot import _shift_reboot


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="fw")
    await client.post("/api/v1/alert-rules", json={"name": "offline", "type": "device_offline", "severity": "critical", "duration_s": 0}, headers=h)
    await poll_all()
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [dev["id"]], "batch_size": 1, "batch_interval_s": 0}, headers=h)
    assert r.status_code == 201, r.text
    return h, dev


async def _dev(client, h, dev):
    return (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()


def test_durations_per_reason():
    assert REBOOT_SUPPRESS["manual"] == dt.timedelta(minutes=5)
    assert REBOOT_SUPPRESS["firmware"] == dt.timedelta(minutes=10)


async def test_no_offline_alarm_during_update_then_normal(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    await firmware_tick()  # Backup, check, install -> rebooting
    rb = (await _dev(client, h, dev))["facts"]["reboot"]
    assert rb["reason"] == "firmware" and rb["by"] == "admin@acme.example.com"
    span = dt.datetime.fromisoformat(rb["until"]) - dt.datetime.fromisoformat(rb["at"])
    assert span == dt.timedelta(minutes=10)
    # Router bootet: offline, aber kein Alarm
    rt.offline = True
    await poll_all()
    await poll_all()
    assert (await _dev(client, h, dev))["status"] == "offline"
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    # zurück mit neuer Version -> Markierung weg, Job erfolgreich
    rt.offline = False
    await _shift_reboot(dev["id"], at=dt.timedelta(seconds=-60))
    await poll_all()
    assert "reboot" not in (await _dev(client, h, dev))["facts"]
    for _ in range(3):
        await firmware_tick()
    items = (await client.get("/api/v1/firmware/jobs", headers=h)).json()[0]
    assert items["status"] == "completed", items
    # danach wieder normal: Ausfall wird alarmiert
    rt.offline = True
    await poll_all()
    await poll_all()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert len(alerts) == 1 and alerts[0]["status"] == "firing"


async def test_update_device_not_returning_alarms_after_ten_minutes(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    await firmware_tick()
    rt.offline = True
    await poll_all()
    await poll_all()
    await _shift_reboot(dev["id"], at=dt.timedelta(minutes=-9), until=dt.timedelta(minutes=-9))  # 9 min: noch unterdrückt
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    await _shift_reboot(dev["id"], at=dt.timedelta(minutes=-2), until=dt.timedelta(minutes=-2))  # 11 min
    await evaluate_all()
    assert len((await client.get("/api/v1/alerts", headers=h)).json()) == 1
