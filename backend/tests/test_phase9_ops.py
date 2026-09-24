from __future__ import annotations

from sqlalchemy import update

from app.db import system_session
from app.models import FirmwareJob
from app.routeros.simulator import get_router
from app.services.backup import backup_all
from app.services.firmware import firmware_tick
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp, n=1):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, [await make_paired_device(client, h, name=f"r{i}") for i in range(n)]


async def test_backup_dedupe_and_diff(client, msp, hub):
    h, (dev,) = await _setup(client, msp)
    await poll_all()
    stats = await backup_all()
    assert stats == {"new": 1, "unchanged": 0, "failed": 0}
    assert (await backup_all())["unchanged"] == 1  # unverändert -> kein neues Backup
    # Konfiguration ändern -> neues Backup mit Diff
    get_router(dev["tunnel_ip"]).tables["/ip/firewall/address-list"].append({".id": "*A1", "list": "blocked", "address": "203.0.113.9"})
    assert (await backup_all())["new"] == 1
    lst = (await client.get(f"/api/v1/devices/{dev['id']}/backups", headers=h)).json()
    assert len(lst) == 2 and lst[0]["added"] == 2 and lst[0]["removed"] == 0
    full = (await client.get(f"/api/v1/backups/{lst[0]['id']}", headers=h)).json()
    assert "add list=blocked address=203.0.113.9" in full["content"]
    assert "# 20" not in full["content"].splitlines()[0]  # Zeitstempel-Kopf entfernt
    assert any(ln == "+add list=blocked address=203.0.113.9" for ln in full["diff"])
    d = (await client.get(f"/api/v1/backups/{lst[0]['id']}/diff?against={lst[1]['id']}", headers=h)).json()
    assert d["added"] == 2
    r = await client.get(f"/api/v1/backups/{lst[0]['id']}/download", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    # Manuelles Backup wird immer gespeichert und gepinnt
    r = await client.post(f"/api/v1/devices/{dev['id']}/backups?note=vor%20Umbau", headers=h)
    assert r.status_code == 201 and r.json()["pinned"] is True
    ov = (await client.get("/api/v1/backups", headers=h)).json()
    assert ov[0]["count"] == 3


async def test_firmware_batches(client, msp, hub):
    h, devs = await _setup(client, msp, n=5)
    await poll_all()
    get_router(devs[2]["tunnel_ip"]).version = "7.16.1"  # schon aktuell -> skipped
    chk = (await client.post("/api/v1/firmware/check", headers=h)).json()
    assert sum(1 for c in chk if c["update_available"]) == 4
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [d["id"] for d in devs], "batch_size": 2, "batch_interval_s": 0}, headers=h)
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert [i["batch_no"] for i in job["items"]] == [0, 0, 1, 1, 2]

    await firmware_tick()  # Batch 0 startet
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    b0 = [i for i in job["items"] if i["batch_no"] == 0]
    assert {i["status"] for i in b0} == {"rebooting"}
    assert all(i["status"] == "queued" for i in job["items"] if i["batch_no"] > 0)  # Batching: Rest wartet
    for _ in range(8):
        await firmware_tick()
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert job["status"] == "completed", job
    assert job["summary"]["success"] == 4 and job["summary"]["skipped"] == 1
    assert all(i["to_version"] == "7.16.1" for i in job["items"])
    # Pre-Update-Backups angelegt
    bk = (await client.get(f"/api/v1/devices/{devs[0]['id']}/backups", headers=h)).json()
    assert bk[0]["trigger"] == "pre-update"


async def test_firmware_pause_on_failure_and_resume(client, msp, hub):
    h, devs = await _setup(client, msp, n=3)
    get_router(devs[0]["tunnel_ip"]).fail_next.add("/system/package/update/check-for-updates")
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [d["id"] for d in devs], "batch_size": 1, "batch_interval_s": 0, "max_failures": 1}, headers=h)
    jid = r.json()["id"]
    for _ in range(4):
        await firmware_tick()
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert job["status"] == "paused"
    assert job["summary"]["failed"] == 1 and job["summary"]["queued"] == 2
    r = await client.post(f"/api/v1/firmware/jobs/{jid}/resume", headers=h)
    assert r.json()["status"] == "running"
    for _ in range(8):
        await firmware_tick()
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert job["status"] == "completed" and job["summary"]["success"] == 2


async def test_firmware_batch_interval_and_cancel(client, msp, hub):
    h, devs = await _setup(client, msp, n=3)
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [d["id"] for d in devs], "batch_size": 1, "batch_interval_s": 3600}, headers=h)
    jid = r.json()["id"]
    for _ in range(4):
        await firmware_tick()
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert job["summary"]["success"] == 1 and job["summary"]["queued"] == 2  # wartet auf Intervall
    assert job["next_batch_at"] is not None
    r = await client.post(f"/api/v1/firmware/jobs/{jid}/cancel", headers=h)
    assert r.json()["status"] == "cancelled"
    async with system_session() as db:
        await db.execute(update(FirmwareJob).values(next_batch_at=None))
        await db.commit()
    await firmware_tick()
    job = (await client.get(f"/api/v1/firmware/jobs/{jid}", headers=h)).json()
    assert job["summary"]["cancelled"] == 2
