"""Phase 17 – Script-Bibliothek: Variablen, Rechte, Vorschau, Backup vor Änderung, Batches mit Abbruch, Suche, Audit."""

from __future__ import annotations

import pytest

from app.routeros.simulator import get_router
from app.services.scripts import ScriptError, render, validate_content, warnings
from app.services.scripts import script_tick
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


def test_render_and_validation():
    ctx = {"device.name": "fil-01", "device.identity": "r1", "site.name": "Nord"}
    assert render(':put "{{ device.name }} @ {{site.name}}"', ctx) == ':put "fil-01 @ Nord"'
    with pytest.raises(ScriptError):
        validate_content(":put {{ device.password }}")
    with pytest.raises(ScriptError):
        render(':put "{{ device.name }}"', {"device.name": 'x"; /system reset-configuration; "'})
    assert warnings("/ip firewall filter remove [find comment~\"sdwan:fw\"]")
    assert not warnings("/system resource print")


async def _setup(client, msp, n=3):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    devs = [await make_paired_device(client, h, name=f"s{i}") for i in range(n)]
    return t, h, devs


async def _run(client, h, rid):
    for _ in range(10):
        await script_tick()
        r = (await client.get(f"/api/v1/script-runs/{rid}", headers=h)).json()
        if r["status"] != "running":
            return r
    return r


async def test_read_script_run_output_and_search(client, msp, hub):
    t, h, devs = await _setup(client, msp)
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    lib = (await client.get("/api/v1/scripts", headers=tech)).json()
    assert "device.name" in lib["variables"] and any(s["builtin"] for s in lib["scripts"])
    s = (await client.post("/api/v1/scripts", json={"name": "Hallo", "category": "read", "content": ':put "Hallo {{ device.name }}"'}, headers=tech)).json()
    pv = (await client.post(f"/api/v1/scripts/{s['id']}/preview", json={"device_ids": [d["id"] for d in devs]}, headers=tech)).json()
    assert [x["rendered"] for x in pv["devices"]] == [':put "Hallo s0"', ':put "Hallo s1"', ':put "Hallo s2"']
    r = await client.post("/api/v1/script-runs", json={"script_id": s["id"], "device_ids": [d["id"] for d in devs], "batch_size": 2}, headers=tech)
    assert r.status_code == 201, r.text
    run = await _run(client, h, r.json()["id"])
    assert run["status"] == "completed" and [i["output"].strip() for i in run["items"]] == ["Hallo s0", "Hallo s1", "Hallo s2"]
    assert [i["batch_no"] for i in run["items"]] == [0, 0, 1]
    hits = (await client.get("/api/v1/script-runs/search", params={"q": "Hallo s1"}, headers=h)).json()
    assert hits and hits[0]["device"] == "s1"
    audit = (await client.get("/api/v1/audit?action=script.run", headers=h)).json()
    start = next(a for a in audit if a["action"] == "script.run")
    assert start["details"]["content"] == ':put "Hallo {{ device.name }}"'  # voller Script-Text


async def test_change_script_rights_confirmation_backup_and_abort(client, msp, hub):
    t, h, devs = await _setup(client, msp)
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    body = {"name": "Kommentar setzen", "category": "change", "content": '/system identity set name="{{ device.name }}"\n:put "ok"'}
    assert (await client.post("/api/v1/scripts", json=body, headers=tech)).status_code == 403  # ändernd nur Admin
    s = (await client.post("/api/v1/scripts", json=body, headers=h)).json()
    ids = [d["id"] for d in devs]
    assert (await client.post("/api/v1/script-runs", json={"script_id": s["id"], "device_ids": ids}, headers=tech)).status_code == 403
    assert (await client.post("/api/v1/script-runs", json={"script_id": s["id"], "device_ids": ids}, headers=h)).status_code == 409  # ohne Bestätigung
    # erstes Gerät scheitert -> Abbruch nach Batch 0 (batch_size 1, max_failures 1)
    get_router(devs[0]["tunnel_ip"]).script_fail = True
    r = await client.post("/api/v1/script-runs", json={"script_id": s["id"], "device_ids": ids, "batch_size": 1, "max_failures": 1,
                                                       "confirm_name": "Kommentar setzen"}, headers=h)
    assert r.status_code == 201, r.text
    run = await _run(client, h, r.json()["id"])
    assert run["status"] == "paused" and run["summary"]["failed"] == 1 and run["summary"]["queued"] == 2
    first = next(i for i in run["items"] if i["device"] == "s0")
    assert first["backup_id"] and "syntax error" in first["error"]
    bk = (await client.get(f"/api/v1/devices/{devs[0]['id']}/backups", headers=h)).json()
    assert bk[0]["trigger"] == "pre-script" and bk[0]["pinned"]
    # fortsetzen -> Rest läuft
    get_router(devs[0]["tunnel_ip"]).script_fail = False
    await client.post(f"/api/v1/script-runs/{run['id']}/resume", headers=h)
    run = await _run(client, h, run["id"])
    assert run["summary"]["success"] == 2 and run["status"] == "failed"  # ein Fehler bleibt dokumentiert


async def test_cancel_and_builtin_protection(client, msp, hub):
    t, h, devs = await _setup(client, msp, n=2)
    lib = (await client.get("/api/v1/scripts", headers=h)).json()["scripts"]
    b = next(s for s in lib if s["builtin"])
    assert (await client.patch(f"/api/v1/scripts/{b['id']}", json={"name": "x", "content": ":put 1"}, headers=h)).status_code == 403
    r = (await client.post("/api/v1/script-runs", json={"script_id": b["id"], "device_ids": [devs[0]["id"]]}, headers=h)).json() \
        if b["category"] == "read" else None
    assert r is not None
    c = (await client.post(f"/api/v1/script-runs/{r['id']}/cancel", headers=h)).json()
    assert c["status"] == "cancelled" and c["items"][0]["status"] == "cancelled"
    assert (await client.post("/api/v1/scripts", json={"name": "x", "content": ":put {{ tenant.secret }}"}, headers=h)).status_code == 422
