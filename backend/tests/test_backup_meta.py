"""Backups: Auslöser und Ersteller je Weg, Backup nach Policy-Push, Prüfsumme."""

from __future__ import annotations

import re

from app.routeros.simulator import get_router
from app.services.backup import backup_all
from app.services.firmware import firmware_tick
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

ADMIN = "admin@acme.example.com"
POLICY = {"address_lists": [{"list": "blocked", "address": "203.0.113.0/24"}], "filter": [{"chain": "forward", "action": "drop", "src-address-list": "blocked"}]}


async def _setup(client, msp, n=1):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, [await make_paired_device(client, h, name=f"r{i}") for i in range(n)]


async def _backups(client, h, dev):
    return (await client.get(f"/api/v1/devices/{dev['id']}/backups", headers=h)).json()


async def test_trigger_and_creator_per_path(client, msp, hub):
    h, (dev,) = await _setup(client, msp)
    await poll_all()
    await backup_all()
    r = await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h)
    assert r.status_code == 201
    r = await client.post("/api/v1/firmware/jobs", json={"device_ids": [dev["id"]], "batch_size": 1, "batch_interval_s": 0}, headers=h)
    assert r.status_code == 201, r.text
    await firmware_tick()
    got = [(b["trigger"], b["created_by"], b["pinned"]) for b in await _backups(client, h, dev)]
    assert got == [("pre-update", ADMIN, True), ("manual", ADMIN, True), ("scheduled", "system", False)]
    assert all(re.fullmatch(r"[0-9a-f]{64}", b["sha256"]) for b in await _backups(client, h, dev))


async def test_backup_after_successful_policy_push_only(client, msp, hub):
    h, devs = await _setup(client, msp, n=2)
    pid = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()["id"]
    await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    get_router(devs[1]["tunnel_ip"]).fail_next.add("/ip/firewall/filter/add")
    r = await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "partial"
    (b,) = await _backups(client, h, devs[0])
    assert (b["trigger"], b["created_by"], b["pinned"]) == ("post-policy", ADMIN, False)
    full = (await client.get(f"/api/v1/backups/{b['id']}", headers=h)).json()
    assert "203.0.113.0/24" in full["content"]  # Stand nach dem Push
    assert await _backups(client, h, devs[1]) == []  # fehlgeschlagenes Gerät: kein Backup


async def test_failed_post_policy_export_does_not_fail_deployment(client, msp, hub):
    h, (dev,) = await _setup(client, msp)
    pid = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()["id"]
    await client.post(f"/api/v1/policies/{pid}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    get_router(dev["tunnel_ip"]).fail_next.add("/export")
    r = await client.post(f"/api/v1/policies/{pid}/deploy", json={}, headers=h)
    dep = (await client.get(f"/api/v1/deployments/{r.json()['deployment_id']}", headers=h)).json()
    assert dep["status"] == "success"
    assert await _backups(client, h, dev) == []
