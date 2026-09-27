"""Audit C.4 – Last: 500 simulierte Geräte über 5 Mandanten (nur mit ``AUDIT_LOAD=1``; am besten auf PostgreSQL).

Aufruf: ``tools/audit/run_load.sh`` (setzt ``TEST_DATABASE_URL`` auf eine eigene PostgreSQL-Datenbank).
Misst Laufzeit und Anzahl SQL-Abfragen je Vorgang (N+1-Erkennung) und schreibt ``AUDIT_LOAD_OUT`` (JSON).
"""

from __future__ import annotations

import json
import os
import resource
import time
import tracemalloc

import pytest
from sqlalchemy import event

from app.db import get_engine
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

pytestmark = pytest.mark.skipif(not os.environ.get("AUDIT_LOAD"), reason="Lasttest nur mit AUDIT_LOAD=1")

N_TENANTS = int(os.environ.get("AUDIT_LOAD_TENANTS", "5"))
N_DEVICES = int(os.environ.get("AUDIT_LOAD_DEVICES", "100"))  # je Mandant


class QueryCounter:
    def __init__(self) -> None:
        self.n = 0
        eng = get_engine().sync_engine
        event.listen(eng, "before_cursor_execute", self._on)

    def _on(self, *_a, **_k) -> None:
        self.n += 1


async def _measure(results: dict, key: str, qc: QueryCounter, coro):
    q0, t0 = qc.n, time.perf_counter()
    out = await coro
    results[key] = {"s": round(time.perf_counter() - t0, 3), "queries": qc.n - q0}
    return out


async def test_load_500_devices(client, msp, hub):
    from app.services import feeds as feeds_mod
    from app.services.alerts import evaluate_all
    from app.services.feeds import feeds_tick
    from app.services.poller import poll_all

    res: dict = {"tenants": N_TENANTS, "devices_per_tenant": N_DEVICES, "db": get_engine().url.get_backend_name()}
    qc = QueryCounter()
    tracemalloc.start()
    t0 = time.perf_counter()
    heads = []
    for i in range(N_TENANTS):
        t = await make_tenant(client, msp, f"last{i}")
        h = await make_tenant_admin(client, msp, t["id"], email=f"admin@last{i}.example.com")
        heads.append(h)
        for j in range(N_DEVICES):
            await make_paired_device(client, h, name=f"t{i}-r{j:03d}")
    res["setup_s"] = round(time.perf_counter() - t0, 1)
    h = heads[0]
    for rule in ({"name": "Offline", "type": "device_offline", "duration_s": 0}, {"name": "WAN", "type": "wan_down", "duration_s": 0},
                 {"name": "Adv", "type": "security_advisory", "duration_s": 0}):
        for hh in heads:
            await client.post("/api/v1/alert-rules", json=rule, headers=hh)
    await client.post("/api/v1/advisories", json={"cve": "CVE-LAST-1", "title": "t", "function": "general", "severity": "high",
                                                   "affected_from": "7.10", "fixed_in": "7.99", "enabled": True}, headers=msp)
    await _measure(res, "poll_all_1", qc, poll_all())
    await _measure(res, "poll_all_2", qc, poll_all())
    await _measure(res, "alerts_evaluate_all", qc, evaluate_all())
    for path in ("/api/v1/devices", "/api/v1/dashboard/summary", "/api/v1/dashboard/fleet-state", "/api/v1/advisories/fleet", "/api/v1/inventory", "/api/v1/local-access",
                 "/api/v1/alerts", "/api/v1/compliance/report", "/api/v1/firmware/overview", "/api/v1/sites"):
        r = await _measure(res, f"GET {path}", qc, client.get(path, headers=h))
        res[f"GET {path}"]["status"] = r.status_code
        res[f"GET {path}"]["bytes"] = len(r.content)
    # Compliance über alle Geräte eines Mandanten
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    devs = (await client.get("/api/v1/devices", headers=h)).json()
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    await _measure(res, "compliance_evaluate_100", qc, client.post("/api/v1/compliance/evaluate", json={}, headers=h))
    await _measure(res, "GET compliance/report (100 ausgewertet)", qc, client.get("/api/v1/compliance/report", headers=h))
    # Backups für 100 Geräte, dann Config-Suche
    for d in devs:
        await client.post(f"/api/v1/devices/{d['id']}/backups", headers=h)
    await _measure(res, "config_search_100_backups", qc, client.get("/api/v1/config-search", params={"q": "identity"}, headers=h))
    await _measure(res, "config_search_regex", qc, client.get("/api/v1/config-search", params={"q": "set name=.*r0[0-9]", "regex": "true"},
                                                               headers=h))
    # Threat-Feed an 100 Geräte verteilen (5000 Einträge)

    async def fake_download(_url):
        return "\n".join(f"10.{i // 250}.{i % 250}.0/24" for i in range(5000))

    feeds_mod._download = fake_download
    feed = (await client.post("/api/v1/feeds", json={"name": "Groß", "url": "https://feeds.example/x"}, headers=h)).json()
    await client.post(f"/api/v1/feeds/{feed['id']}/assign", json={"device_ids": [d["id"] for d in devs]}, headers=h)
    await _measure(res, "feeds_tick_100dev_5000", qc, feeds_tick())
    cur, peak = tracemalloc.get_traced_memory()
    res["python_heap_mb"] = {"current": round(cur / 2**20, 1), "peak": round(peak / 2**20, 1)}
    res["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    out = os.environ.get("AUDIT_LOAD_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(res, f, indent=1)
    assert res["poll_all_2"]["s"] < 600
