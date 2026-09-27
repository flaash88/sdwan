"""Audit-Behebung AP4 (Robustheit von Poll und Jobs) – zusätzliche Tests (docs/AUDIT-2026-09.md, docs/PLAN-AUDIT-FIX.md)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from app import locks
from app.db import system_session, utcnow
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _admin(client, msp, slug="acme"):
    t = await make_tenant(client, msp, slug)
    return t, await make_tenant_admin(client, msp, t["id"], email=f"admin@{slug}.example.com")


# ----------------------------------------------------------------------------- Sperren
async def test_locks_exclusive_reentrant_and_released():
    locks.reset()
    async with locks.hold("x", "a") as a:
        assert a
        async with locks.hold("x", "b") as b:
            assert b  # reentrant im selben Kontext
        other = asyncio.create_task(_try_other("x"))
        assert await other is False  # anderer Task (Kontext ohne die Sperre) bekommt sie nicht
        assert (await locks.owner_of("x")).startswith("a#")
    assert await locks.owner_of("x") is None
    async with locks.hold("x", "c") as c:
        assert c


async def _try_other(key: str) -> bool:
    locks._held.set(frozenset())
    async with locks.hold(key, "other") as got:
        return got


async def test_lock_wait_then_acquire():
    locks.reset()

    async def holder():
        async with locks.hold("y", "a"):
            await asyncio.sleep(0.3)

    t = asyncio.create_task(holder())
    await asyncio.sleep(0.05)
    locks._held.set(frozenset())
    async with locks.hold("y", "b", wait=2) as got:
        assert got
    await t


# ----------------------------------------------------------------------------- AUDIT-014: Post-Poll-Hooks
async def test_014_post_poll_hooks_skip_locked_devices_and_do_not_overlap(client, msp, hub, monkeypatch):
    from app.services import poller, registry

    _t, h = await _admin(client, msp)
    d1 = await make_paired_device(client, h, name="d1")
    d2 = await make_paired_device(client, h, name="d2")
    calls: list[str] = []
    running = {"n": 0, "max": 0}

    async def slow_hook(db, devices):
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        calls.extend(d.name for d in devices)
        await asyncio.sleep(0.2)
        running["n"] -= 1

    monkeypatch.setattr(registry, "post_poll_hooks", lambda: [slow_hook])
    locks.reset()
    # Gerät d1 gerade „in Arbeit“ (z. B. Deployment): Hook läuft nur für d2
    async with locks.device(d1["id"], "deploy:test"):
        locks._held.set(frozenset())
        await poller.poll_all()
    assert calls == ["d2"]
    calls.clear()
    # Live-Poll und Flotten-Poll gleichzeitig: jedes Gerät höchstens einmal im Hook, nie parallel
    await asyncio.gather(poller.poll_all(), poller.poll_all(only={d1["id"], d2["id"]}))
    assert sorted(calls) == ["d1", "d2"] and running["max"] == 1
    assert d2


# ----------------------------------------------------------------------------- AUDIT-016: Deploy
async def test_016_parallel_deploys_on_same_device_are_serialized(client, msp, hub, monkeypatch):
    from app.services import policy

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    from tests.test_backup_meta import POLICY

    pol = (await client.post("/api/v1/policies", json={"name": "P", "content": POLICY}, headers=h)).json()
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    active = {"n": 0, "max": 0}
    orig_push = policy.push

    async def slow_push(api, cfg):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.2)
        try:
            return await orig_push(api, cfg)
        finally:
            active["n"] -= 1

    monkeypatch.setattr(policy, "push", slow_push)
    from app.models import PolicyDeployment

    ids = []
    async with system_session() as db:
        for _ in range(2):
            d = PolicyDeployment(tenant_id=uuid.UUID(dev["tenant_id"]), policy_id=uuid.UUID(pol["id"]), started_by="test")
            db.add(d)
            await db.flush()
            ids.append(d.id)
        await db.commit()
    locks.reset()
    await asyncio.gather(*(policy.run_deployment(i, [uuid.UUID(dev["id"])]) for i in ids))
    assert active["max"] == 1
    async with system_session() as db:
        assert {(await db.get(PolicyDeployment, i)).status for i in ids} == {"success"}


async def test_016_deploy_waits_then_reports_locked_device(client, msp, hub, monkeypatch):
    from app.models import PolicyDeployment
    from app.services import policy

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    monkeypatch.setattr(policy, "DEPLOY_LOCK_WAIT_S", 0.2)
    async with system_session() as db:
        d = PolicyDeployment(tenant_id=uuid.UUID(dev["tenant_id"]), started_by="test")
        db.add(d)
        await db.commit()
        dep_id = d.id
    locks.reset()

    async def other():
        async with locks.device(dev["id"], "firmware:x"):
            await asyncio.sleep(0.6)

    t = asyncio.create_task(other())
    await asyncio.sleep(0.05)
    await policy.run_deployment(dep_id, [uuid.UUID(dev["id"])])
    await t
    async with system_session() as db:
        dep = await db.get(PolicyDeployment, dep_id)
        assert dep.status == "failed" and "gesperrt" in dep.results[dev["id"]]["error"] and "firmware" in dep.results[dev["id"]]["error"]


async def test_016_stale_running_deployment_aborted_but_active_one_kept(client, msp):
    from app.models import PolicyDeployment
    from app.services import policy

    t, _h = await _admin(client, msp)
    old = utcnow() - dt.timedelta(minutes=10)
    async with system_session() as db:
        stale = PolicyDeployment(tenant_id=uuid.UUID(t["id"]), status="running", started_by="x", created_at=old,
                                 results={"d": {"name": "r", "ok": False}})
        alive = PolicyDeployment(tenant_id=uuid.UUID(t["id"]), status="running", started_by="x", created_at=old)
        fresh = PolicyDeployment(tenant_id=uuid.UUID(t["id"]), status="queued", started_by="x")
        db.add_all([stale, alive, fresh])
        await db.commit()
        ids = (stale.id, alive.id, fresh.id)
    locks.reset()
    async with locks.hold(policy.deployment_key(ids[1]), "deploy"):
        locks._held.set(frozenset())
        assert await policy.abort_stale_deployments() == 1
    async with system_session() as db:
        s, a, f = [await db.get(PolicyDeployment, i) for i in ids]
        assert s.status == "aborted" and "Neustart" in s.results["_error"] and s.results["d"]["error"] == "abgebrochen"
        assert a.status == "running" and f.status == "queued"


async def test_016_unexpected_error_never_leaves_running(client, msp, hub, monkeypatch):
    from app.models import PolicyDeployment
    from app.services import policy

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)

    async def boom(*_a, **_k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(policy, "device_policies", boom)
    async with system_session() as db:
        d = PolicyDeployment(tenant_id=uuid.UUID(dev["tenant_id"]), started_by="test")
        db.add(d)
        await db.commit()
        dep_id = d.id
    with pytest.raises(RuntimeError):
        await policy.run_deployment(dep_id, [uuid.UUID(dev["id"])])
    async with system_session() as db:
        assert (await db.get(PolicyDeployment, dep_id)).status == "failed"


async def test_016_offboarding_refused_while_device_locked(client, msp, hub):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    locks.reset()
    import app.api.v1.offboarding as off

    async with locks.device(dev["id"], "deploy:x"):
        locks._held.set(frozenset())
        orig = locks.device

        def short(device_id, purpose, *, ttl=120, wait=0):
            return orig(device_id, purpose, ttl=ttl, wait=0.1)

        off.locks.device = short  # type: ignore[assignment]
        try:
            r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": dev["name"], "mode": "platform_only"},
                                  headers=h)
        finally:
            off.locks.device = orig  # type: ignore[assignment]
    assert r.status_code == 409 and "gesperrt" in r.text


# ----------------------------------------------------------------------------- AUDIT-015: facts-Merge
def test_015_three_way_merge_rules():
    from app.facts_merge import three_way

    base = {"a": 1, "b": 2, "c": 3}
    ours = {"a": 10, "b": 2}  # a geändert, c entfernt
    theirs = {"a": 1, "b": 20, "c": 3, "d": 4}  # b geändert, d neu
    assert three_way(base, ours, theirs) == {"a": 10, "b": 20, "d": 4}


# ----------------------------------------------------------------------------- AUDIT-017: Plattform-Sicherung
async def test_017_stale_running_platform_backup_marked_failed_with_alarm():
    from app import platform_backup
    from app.models import PlatformAlert, PlatformBackup

    async with system_session() as db:
        rec = PlatformBackup(trigger="scheduled", status="running", created_at=utcnow() - dt.timedelta(hours=5))
        db.add(rec)
        await db.commit()
        rid = rec.id
    assert await platform_backup.fail_stale_running() == 1
    from sqlalchemy import select

    async with system_session() as db:
        assert (await db.get(PlatformBackup, rid)).status == "failed"
        alerts = (await db.execute(select(PlatformAlert).where(PlatformAlert.type == "platform_backup_failed"))).scalars().all()
        assert any("abgebrochen" in (a.message or "") and a.status == "firing" for a in alerts)


# ----------------------------------------------------------------------------- AUDIT-018: Commit vor Router-Aktion
async def test_018_firmware_state_committed_before_install(client, msp, hub, monkeypatch):
    from app.models import FirmwareJobItem
    from app.routeros.simulator import get_router
    from app.services import firmware

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    rt = get_router(dev["tunnel_ip"])
    rt.latest_version = "7.99"  # Update verfügbar
    r = await client.post("/api/v1/firmware/jobs", json={"name": "j", "device_ids": [dev["id"]], "channel": "stable"}, headers=h)
    assert r.status_code in (200, 201), r.text
    seen: list[str] = []
    orig = firmware.connect_device

    class Probe:
        def __init__(self, d):
            self.cm = orig(d)

        async def __aenter__(self):
            async with system_session() as db:
                from sqlalchemy import select

                st = (await db.execute(select(FirmwareJobItem.status))).scalars().all()
            seen.extend(st)
            return await self.cm.__aenter__()

        async def __aexit__(self, *a):
            return await self.cm.__aexit__(*a)

    monkeypatch.setattr(firmware, "connect_device", Probe)
    await firmware.firmware_tick()
    assert "rebooting" in seen, f"Status vor Install nicht festgeschrieben: {seen}"


async def test_018_interrupted_script_item_not_rerun(client, msp, hub):
    from app.models import ScriptRun, ScriptRunItem
    from app.services import scripts

    t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    async with system_session() as db:
        run = ScriptRun(tenant_id=uuid.UUID(t["id"]), script_name="s", script_version=1, category="change", status="running", content=":put 1",
                        current_batch=0, max_failures=5, created_by="x")
        db.add(run)
        await db.flush()
        db.add(ScriptRunItem(tenant_id=uuid.UUID(t["id"]), run_id=run.id, device_id=uuid.UUID(dev["id"]), batch_no=0, status="running",
                             rendered=":put 1"))
        await db.commit()
        rid = run.id
    await scripts.script_tick()
    from sqlalchemy import select

    async with system_session() as db:
        item = (await db.execute(select(ScriptRunItem).where(ScriptRunItem.run_id == rid))).scalar_one()
        assert item.status == "failed" and "Unterbrochen" in item.error and item.output is None


# ----------------------------------------------------------------------------- AUDIT-033: Zeit und Transaktionen
def test_033_previous_month_in_tenant_timezone():
    from app.services.sla import previous_month

    start, end = previous_month(dt.datetime(2026, 4, 15, 12, tzinfo=dt.UTC), "Europe/Vienna")
    assert start == dt.datetime(2026, 2, 28, 23, 0, tzinfo=dt.UTC)  # 1. März 00:00 MEZ
    assert end == dt.datetime(2026, 3, 31, 22, 0, tzinfo=dt.UTC)  # 1. April 00:00 MESZ (nach Umstellung)
    s2, e2 = previous_month(dt.datetime(2026, 3, 15, tzinfo=dt.UTC))
    assert (s2.day, e2.day) == (1, 1)


def test_033_wan_volume_month_in_tenant_timezone():
    from app.models import WanLink
    from app.services.wan import account_volume

    lk = WanLink(slot=1, name="x", interface="ether1", vol_month="2026-03", vol_bytes=5)
    account_volume(lk, None, dt.datetime(2026, 3, 31, 22, 30, tzinfo=dt.UTC), "Europe/Vienna")
    assert lk.vol_month == "2026-04" and lk.vol_bytes == 0


async def test_033_alert_mail_only_after_commit_and_tenant_isolation(client, msp, hub, monkeypatch):
    from sqlalchemy import select

    from app.models import Alert
    from app.services import alerts

    t1, h1 = await _admin(client, msp, "eins")
    t2, h2 = await _admin(client, msp, "zwei")
    for h in (h1, h2):
        dev = await make_paired_device(client, h)
        await client.post("/api/v1/alert-rules", json={"name": "off", "type": "device_offline", "duration_s": 0,
                                                       "recipients": ["noc@example.com"]}, headers=h)
        from app.routeros.simulator import get_router

        get_router(dev["tunnel_ip"]).offline = True
    from app.services.poller import poll_all

    await poll_all()
    await poll_all()
    committed_at_send: list[bool] = []

    async def fake_send(to, subject, text, attachments=None, html=None):
        async with system_session() as db:
            committed_at_send.append(bool((await db.execute(select(Alert).where(Alert.status == "firing"))).first()))
        return True

    monkeypatch.setattr(alerts, "send_mail", fake_send)
    orig = alerts.evaluate_tenant

    async def flaky(db, tenant):
        if tenant.slug == "eins":
            raise RuntimeError("Mandant eins kaputt")
        return await orig(db, tenant)

    monkeypatch.setattr(alerts, "evaluate_tenant", flaky)
    await alerts.evaluate_all()
    async with system_session() as db:
        firing = (await db.execute(select(Alert).where(Alert.status == "firing"))).scalars().all()
    assert {str(a.tenant_id) for a in firing} == {t2["id"]}  # Mandant zwei trotz Fehler bei eins ausgewertet
    assert committed_at_send and all(committed_at_send)


# ----------------------------------------------------------------------------- AUDIT-043: Worker-Leader
async def test_043_second_worker_is_standby():
    from app.worker.__main__ import LEADER_KEY

    locks.reset()
    async with locks.hold(LEADER_KEY, "worker:a", ttl=60) as first:
        locks._held.set(frozenset())
        async with locks.hold(LEADER_KEY, "worker:b", ttl=60) as second:
            assert first and not second
