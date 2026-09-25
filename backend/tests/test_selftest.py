"""Hardware-Selbsttest: im Simulator vollständig grün, Abweichungen werden erkannt, nichts wird geschrieben."""

from __future__ import annotations

import copy

from app.routeros.schema import PATH_SPECS
from app.routeros.simulator import get_router
from app.services.selftest import check_fields
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _dev(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, await make_paired_device(client, h, name="lab-l009")


def _by_key(res):
    return {c["key"]: c for c in res["checks"]}


async def test_selftest_green_in_simulator(client, msp, hub):
    h, dev = await _dev(client, msp)
    assert (await client.get(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json() is None
    rt = get_router(dev["tunnel_ip"])
    before = copy.deepcopy(rt.tables)
    r = await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    bad = [(c["key"], c.get("missing"), c.get("notes"), c.get("error")) for c in res["checks"] if c["status"] != "ok"]
    assert res["status"] == "ok", bad
    checks = _by_key(res)
    # jeder Pfad aus der zentralen Liste wurde geprüft (keine eigene Feldliste im Test)
    assert {s.key for s in PATH_SPECS} <= set(checks)
    for s in PATH_SPECS:
        assert checks[s.key]["reachable"] is True and checks[s.key]["ms"] >= 0
    assert {"version", "architecture", "rights", "service_api", "service_ssh", "clock_skew", "export"} <= set(checks)
    # rein lesend: Router-Tabellen unverändert
    assert rt.tables == before
    # gespeichert
    last = (await client.get(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    assert last["status"] == "ok" and last["ran_by"] == "admin@acme.example.com" and len(last["checks"]) == len(res["checks"])
    audit = (await client.get("/api/v1/audit?action=device.selftest", headers=h)).json()
    assert audit and audit[0]["details"]["status"] == "ok"


async def test_selftest_detects_deviations(client, msp, hub):
    h, dev = await _dev(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt.version = "6.49.10"
    rt.clock_skew_s = 600
    for g in rt.tables["/user/group"]:
        if g["name"] == "sdwan-api":
            g["policy"] = g["policy"].replace(",reboot", "").replace(",sensitive", "")
    for svc in rt.tables["/ip/service"]:
        if svc["name"] == "ssh":
            svc["address"] = "192.168.88.0/24"
    rt.tables["/tool/netwatch"].append({".id": "*F1", "host": "1.1.1.1", "status": "up"})
    orig = rt._table_op

    def no_rx(path, action, params):  # /interface ohne rx-byte (Feld anders benannt)
        rows = orig(path, action, params)
        if path == "/interface" and action == "print":
            for x in rows:
                x.pop("rx-byte", None)
        if path == "/tool/netwatch" and action == "print":  # ältere Version ohne Latenzfelder
            for x in rows:
                x.pop("rtt-avg", None)
                x.pop("loss-percent", None)
        return rows

    rt._table_op = no_rx
    try:
        res = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    finally:
        rt._table_op = orig
    c = _by_key(res)
    assert res["status"] == "error"
    assert c["version"]["status"] == "error"
    assert c["clock_skew"]["status"] == "error" and c["clock_skew"]["value"] >= 590
    assert c["rights"]["status"] == "error" and "reboot" in c["rights"]["missing"]
    assert c["service_ssh"]["status"] == "error" and "Backup-Export" in c["service_ssh"]["notes"][0]
    assert c["interface"]["status"] == "error" and c["interface"]["missing"] == ["rx-byte"]
    assert c["netwatch"]["status"] == "warn" and "rtt-avg" in c["netwatch"]["missing_optional"]


async def test_selftest_sensitive_optional_and_update_hint(client, msp, hub):
    h, dev = await _dev(client, msp)
    rt = get_router(dev["tunnel_ip"])
    for g in rt.tables["/user/group"]:
        if g["name"] == "sdwan-api":
            g["policy"] = g["policy"].replace(",sensitive", "")
    rt._update_print = lambda p: [{"channel": "stable", "installed-version": rt.version, "status": ""}]  # vor check-for-updates
    c = _by_key((await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json())
    assert c["rights"]["status"] == "warn" and "unvollständig" in c["rights"]["notes"][0]
    assert c["package_update"]["status"] == "ok" and any("Update-Prüfung" in n for n in c["package_update"]["notes"])


async def test_selftest_offline_and_rbac(client, msp, hub):
    h, dev = await _dev(client, msp)
    get_router(dev["tunnel_ip"]).offline = True
    res = (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)).json()
    assert res["status"] == "error" and res["checks"][0]["key"] == "connect"
    ro = await make_tenant_admin(client, msp, dev["tenant_id"], email="ro@acme.example.com", role="readonly")
    assert (await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=ro)).status_code == 403
    assert (await client.get(f"/api/v1/devices/{dev['id']}/selftest", headers=ro)).status_code == 200


def test_check_fields_rules():
    spec = next(s for s in PATH_SPECS if s.key == "vrrp")
    r = check_fields(spec, [{"name": "v1", "running": "true"}])
    assert r["status"] == "warn" and "master" in r["missing_optional"]  # Flags fehlen -> Warnung
    assert check_fields(spec, [])["notes"][0].startswith("Keine Einträge – Feldprüfung übersprungen")
    iface = next(s for s in PATH_SPECS if s.key == "interface")
    assert check_fields(iface, [])["status"] == "warn"  # Pflicht-Tabelle leer
