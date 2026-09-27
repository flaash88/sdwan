"""Audit Teil C – End-to-End-Szenarien gegen den Simulator (über die HTTP-API wie die Oberfläche).

C.1 Lebenszyklus: Mandant → Standort → ZTP → Onboarding → Selbsttest → WAN-Failover → VRRP → Policy mit
    Default-Drop (defconf) → Rollback → Backup/Diff → Firmware → Fernzugriff → Vor-Ort-Zugang → Hotspot → WLAN →
    Threat-Feed → Compliance → Script → Wartungsfenster → Offboarding (danach keine sdwan:-Objekte, defconf aktiv).
C.2 Störungen: Router offline während Deploy/Firmware/Offboarding/„Rechte einschränken“.
C.3 Parallelität: zwei Deploys gleichzeitig, parallele Fernzugriffe.

Protokoll je Schritt: Umgebungsvariable ``AUDIT_SCENARIO_LOG=<datei>``. Aufruf: ``tools/audit/run_scenarios.sh``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from typing import Any

from app.config import get_settings
from app.routeros.simulator import _seed_wlan, get_router
from app.services.poller import poll_all
from tests.conftest import make_tenant, make_tenant_admin
from tests.test_phase14_defconf import _factory, _state

LOG: list[str] = []


def _log(step: str, ok: bool, detail: Any = "") -> bool:
    LOG.append(f"{'OK ' if ok else 'ERR'} {step}{' – ' + str(detail)[:300] if detail != '' else ''}")
    return ok


def _flush(name: str) -> None:
    out = os.environ.get("AUDIT_SCENARIO_LOG")
    if out:
        with open(out, "a") as f:
            f.write(f"\n## {name}\n" + "\n".join(LOG) + "\n")
    LOG.clear()


def _leftovers(rt) -> list[tuple[str, str]]:
    return [(p, str(r.get("comment"))) for p, rows in rt.tables.items() for r in rows if str(r.get("comment", "")).startswith("sdwan:")]


async def test_c1_full_lifecycle(client, msp, hub, monkeypatch):
    from app.services import feeds as feeds_mod
    from app.services.feeds import feeds_tick
    from app.services.firmware import firmware_tick
    from app.services.scripts import script_tick

    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41980-41989")

    async def fake_download(_url):
        return "1.10.16.0/20\n2.56.192.0/22\n"

    monkeypatch.setattr(feeds_mod, "_download", fake_download)
    t0 = time.time()
    t = await make_tenant(client, msp, "lebenszyklus")
    h = await make_tenant_admin(client, msp, t["id"], email="admin@lz.example.com")
    site = (await client.post("/api/v1/sites", json={"name": "Filiale 1", "lan_subnets": ["192.168.50.0/24"]}, headers=h)).json()
    _log("Mandant + Standort", bool(site.get("id")))
    tpl = await client.post("/api/v1/ztp/templates", json={"name": "Standard", "content": {
        "wan_interface": "ether1", "timezone": "Europe/Vienna",
        "wan": {"mode": "failover", "links": [
            {"name": "Glasfaser", "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"},
            {"name": "LTE", "interface": "ether8", "gateway": "198.51.100.1", "priority": 2, "check_target": "9.9.9.9"}]}}}, headers=h)
    _log("ZTP-Vorlage", tpl.status_code == 201, tpl.text[:120] if tpl.status_code != 201 else "")
    st = await client.post("/api/v1/ztp/stage", json={"template_id": tpl.json()["id"], "site_id": site["id"], "devices": [{"name": "fil1", "serial": "HGKLZ0001"}]},
                           headers=h)
    dev = st.json()[0]["device"]
    _log("ZTP vorbereiten", st.status_code == 201)
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)  # Werkszustand mit defconf-Firewall
    rt.wlan_driver = "wifi"
    _seed_wlan(rt)
    r = await client.post(f"/api/v1/devices/{dev['id']}/simulate-pair", headers=h)
    _log("Onboarding (Pairing)", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    _log("ZTP provisioniert", d.get("ztp_state") == "provisioned", d.get("ztp_state"))
    stest = await client.post(f"/api/v1/devices/{dev['id']}/selftest", headers=h)
    _log("Selbsttest", stest.status_code == 200 and stest.json().get("status") in ("ok", "warn"), stest.json().get("status"))
    # WAN-Failover: Leitung 1 down → Backup aktiv → up
    rt.down_hosts.add("1.1.1.1")
    await poll_all()
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    _log("WAN-Failover auf LTE", [(lk["status"], lk["active"]) for lk in links] == [("down", False), ("up", True)],
         [(lk["name"], lk["status"], lk["active"]) for lk in links])
    rt.down_hosts.discard("1.1.1.1")
    await poll_all()
    links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json()["links"]
    _log("WAN zurück auf Glasfaser (nach Hysterese sofort im Simulator)", links[0]["active"] is True, [(lk["name"], lk["active"]) for lk in links])
    # VRRP
    v = await client.put(f"/api/v1/devices/{dev['id']}/vrrp", json={"instances": [
        {"name": "vrrp-lan", "interface": "ether2", "vrid": 10, "priority": 100, "vip": "192.168.50.254", "local_address": "192.168.50.2/24"}]}, headers=h)
    _log("VRRP anlegen", v.status_code == 200, v.text[:150] if v.status_code != 200 else "")
    # Policy mit Default-Drop auf Werks-Firewall (defconf) + Rollback
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]},
                                                                           {"interface": "bridge", "zone_id": z["lan"]}]}, headers=h)
    pol = (await client.post("/api/v1/policies", json={"name": "FW", "mode": "simple", "spec": {"rules": [
        {"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]}}, headers=h)).json()
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    dep = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    _log("Policy Default-Drop ausrollen", dep.status_code == 202, dep.text[:150] if dep.status_code != 202 else "")
    _log("defconf deaktiviert", set(_state(rt).values()) == {"yes"}, _state(rt))
    await client.patch(f"/api/v1/policies/{pol['id']}", json={"spec": {"rules": [
        {"src_zone": z["management"], "dst_zone": "router", "action": "accept"},
        {"src_zone": z["lan"], "dst_zone": "router", "action": "accept"}]}}, headers=h)
    rb = await client.post(f"/api/v1/policies/{pol['id']}/rollback", json={"version": 1}, headers=h)
    _log("Policy-Rollback", rb.status_code in (200, 202), rb.text[:150] if rb.status_code not in (200, 202) else "")
    # Backup + Diff
    b1 = (await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h))
    rt.identity = "geaendert"
    b2 = (await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h))
    bid = (b2.json().get("id") or (b2.json().get("backup") or {}).get("id")) if b2.status_code < 300 else None
    diff = await client.get(f"/api/v1/backups/{bid}/diff", headers=h) if bid else None
    _log("Backup + Diff", b1.status_code < 300 and diff is not None and diff.status_code == 200, (diff.text[:120] if diff else b2.text[:120]))
    # Firmware
    fj = await client.post("/api/v1/firmware/jobs", json={"device_ids": [dev["id"]], "batch_size": 1, "batch_interval_s": 0}, headers=h)
    for _ in range(6):
        await firmware_tick()
        await poll_all()
    jobs = (await client.get("/api/v1/firmware/jobs", headers=h)).json()
    _log("Firmware-Rollout", fj.status_code == 201 and jobs and jobs[0]["status"] in ("completed", "running"), jobs[0]["status"] if jobs else fj.text[:100])
    # Fernzugriff inkl. Dienst-Wiederherstellung
    www = next(s for s in rt.tables["/ip/service"] if s["name"] == "www")
    www["disabled"] = "true"
    rs = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "webfig", "duration_minutes": 15,
                                                                               "allowed_cidr": "198.51.100.7/32"}, headers=h)
    opened = rs.status_code == 201 and str(www["disabled"]).lower() in ("no", "false")
    await client.post(f"/api/v1/remote-sessions/{rs.json()['id']}/close", headers=h) if rs.status_code == 201 else None
    www = next(s for s in rt.tables["/ip/service"] if s["name"] == "www")
    _log("Fernzugriff + Dienst zurück", opened and str(www["disabled"]).lower() in ("true", "yes"), www)
    # Vor-Ort-Zugang + Rotation
    la = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={}, headers=h)
    rot = await client.post(f"/api/v1/devices/{dev['id']}/local-access/rotate", headers=h)
    _log("Vor-Ort-Zugang + Rotation", la.status_code == 200 and la.json().get("status") == "active" and rot.status_code == 200, la.text[:150])
    # Hotspot + Voucher
    portal = next(p for p in (await client.get("/api/v1/hotspot/portals", headers=h)).json() if p["name"] == "Hotel")
    inst = await client.post("/api/v1/hotspot/instances", json={"name": "Lobby", "slug": "lobby", "device_id": dev["id"], "portal_id": portal["id"],
                                                               "interface": "bridge"}, headers=h)
    ok = inst.status_code == 201 and (await client.post(f"/api/v1/hotspot/instances/{inst.json()['id']}/apply", headers=h)).status_code == 200
    vp = (await client.post("/api/v1/hotspot/voucher-profiles", json={"name": "Tag", "slug": "tag", "validity_min": 1440}, headers=h)).json()
    vb = await client.post(f"/api/v1/hotspot/instances/{inst.json()['id']}/batches", json={"profile_id": vp["id"], "count": 3}, headers=h) if ok else None
    _log("Hotspot + Voucher", ok and vb is not None and vb.status_code in (200, 201), (vb.text[:120] if vb is not None else inst.text[:120]))
    # WLAN
    prof = (await client.post("/api/v1/wlan/profiles", json={"name": "Büro", "slug": "buero", "ssid": "Beispiel", "security": "wpa2-psk",
                                                             "passphrase": "geheim-12345"}, headers=h)).json()
    await client.put(f"/api/v1/wlan/profiles/{prof['id']}/assignments", json=[{"device_ids": [dev["id"]]}], headers=h)
    await client.post(f"/api/v1/wlan/profiles/{prof['id']}/apply", headers=h)
    p = next(x for x in (await client.get("/api/v1/wlan/profiles", headers=h)).json() if x["id"] == prof["id"])
    _log("WLAN ausrollen", p["devices"] and p["devices"][0]["status"] == "ok", p["devices"])
    # Threat-Feed
    feed = (await client.post("/api/v1/feeds", json={"name": "Liste", "url": "https://feeds.example/l.txt"}, headers=h)).json()
    await client.post(f"/api/v1/feeds/{feed['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await feeds_tick()
    _log("Threat-Feed verteilt", any(x.get("list") == "sdwan-feed-liste" for x in rt.tables["/ip/firewall/address-list"]))
    # Compliance
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    ev = await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev["id"]]}, headers=h)
    _log("Compliance", ev.status_code == 200 and ev.json().get("results"), ev.text[:100])
    # Script-Lauf
    sc = (await client.post("/api/v1/scripts", json={"name": "Info", "category": "read", "content": ":put \"hallo\""}, headers=h)).json()
    run = await client.post("/api/v1/script-runs", json={"script_id": sc["id"], "device_ids": [dev["id"]]}, headers=h)
    for _ in range(3):
        await script_tick()
    rr = (await client.get(f"/api/v1/script-runs/{run.json()['id']}", headers=h)).json()
    _log("Script-Lauf", rr.get("status") in ("completed", "done", "success"), rr.get("status"))
    # Wartungsfenster (jetzt aktiv)
    now = dt.datetime.now(dt.UTC)
    mw = await client.post("/api/v1/maintenance-windows", json={"name": "Jetzt", "kind": "once", "device_id": dev["id"],
                                                             "start_at": (now - dt.timedelta(minutes=5)).isoformat(),
                                                             "end_at": (now + dt.timedelta(minutes=55)).isoformat()}, headers=h)
    _log("Wartungsfenster", mw.status_code == 201, mw.text[:120] if mw.status_code != 201 else "")
    # Offboarding
    off = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "fil1", "keep_local_access": False}, headers=h)
    _log("Offboarding", off.status_code == 200, off.text[:200] if off.status_code != 200 else "")
    rt.fire_scheduler("sdwan-offboard")
    left = _leftovers(rt)
    _log("keine sdwan:-Objekte mehr", not left, left[:5])
    _log("defconf wieder aktiv", set(_state(rt).values()) <= {"no", "false"}, _state(rt))
    _log("Laufzeit", True, f"{time.time() - t0:.1f}s")
    errs = [x for x in LOG if x.startswith("ERR")]
    _flush("C.1 Lebenszyklus")
    assert not errs, "\n".join(errs)


async def test_c2_router_offline_during_operations(client, msp, hub):
    """Router fällt während Policy-Deploy / „Rechte einschränken“ / Offboarding aus → kein inkonsistenter Zustand."""
    from app.services import policy as policy_mod  # noqa: F401

    t = await make_tenant(client, msp, "stoerung")
    h = await make_tenant_admin(client, msp, t["id"], email="admin@st.example.com")
    from tests.conftest import make_paired_device

    dev = await make_paired_device(client, h, name="st1")
    rt = get_router(dev["tunnel_ip"])
    _factory(rt)
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]}]}, headers=h)
    pol = (await client.post("/api/v1/policies", json={"name": "FW", "mode": "simple", "spec": {"rules": [
        {"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]}}, headers=h)).json()
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    before = [dict(x) for x in rt.tables["/ip/firewall/filter"]]
    # Ausfall mitten im Deploy: Filter-Add schlägt fehl
    rt.fail_next.add("/ip/firewall/filter/add")
    dep = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    deps = (await client.get(f"/api/v1/policies/{pol['id']}/deployments", headers=h))
    status = deps.json()[0]["status"] if deps.status_code == 200 and deps.json() else dep.text[:120]
    after = rt.tables["/ip/firewall/filter"]
    same = [(x.get("comment"), x.get("disabled")) for x in before] == [(x.get("comment"), x.get("disabled")) for x in after]
    _log("Deploy-Fehler → Zustand wie vorher (Rollback)", same, status)
    _log("defconf nach fehlgeschlagenem Deploy aktiv", set(_state(rt).values()) <= {"false", "no"}, _state(rt))
    # Router komplett offline während Offboarding (clean) → abgelehnt, Gerät bleibt
    rt.offline = True
    await poll_all()
    await poll_all()
    off = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "st1"}, headers=h)
    _log("Offboarding offline abgelehnt", off.status_code == 409, off.status_code)
    rt.offline = False
    await poll_all()
    # Rechte einschränken mit Ausfall beim Setzen der Gruppe → Totmannschaltung bleibt/zurückgestellt
    rt.fail_next.add("/user/group/set")
    rr = await client.post(f"/api/v1/devices/{dev['id']}/restrict-api-user", headers=h)
    grp = next((g for g in rt.tables["/user/group"] if g.get("name") == "sdwan-api"), {})
    _log("Rechte einschränken mit Fehler → API-Gruppe intakt", "api" in str(grp.get("policy", "")), f"{rr.status_code} {grp.get('policy')}")
    errs = [x for x in LOG if x.startswith("ERR")]
    _flush("C.2 Störungen")
    assert not errs, "\n".join(errs)


async def test_c3_parallel_deploys_and_sessions(client, msp, hub, monkeypatch):
    """Zwei Deploys derselben Policy gleichzeitig; zwei Fernzugriffs-Sitzungen gleichzeitig."""
    from tests.conftest import make_paired_device

    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41990-41999")
    t = await make_tenant(client, msp, "parallel")
    h = await make_tenant_admin(client, msp, t["id"], email="admin@par.example.com")
    dev = await make_paired_device(client, h, name="p1")
    rt = get_router(dev["tunnel_ip"])
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]}]}, headers=h)
    pol = (await client.post("/api/v1/policies", json={"name": "FW", "mode": "simple", "spec": {"rules": [
        {"src_zone": z["management"], "dst_zone": "router", "action": "accept"}]}}, headers=h)).json()
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    r1, r2 = await asyncio.gather(client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h),
                                  client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h))
    managed = [x for x in rt.tables["/ip/firewall/filter"] if str(x.get("comment", "")).startswith("sdwan:fw:")]
    comments = [x["comment"] for x in managed]
    _log("zwei parallele Deploys: keine doppelten Regeln", len(comments) == len(set(comments)), f"{r1.status_code}/{r2.status_code} {len(comments)} Regeln")
    s1, s2 = await asyncio.gather(
        client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "allowed_cidr": "192.0.2.1/32"}, headers=h),
        client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "allowed_cidr": "192.0.2.2/32"}, headers=h))
    ports = {s1.json().get("listen_port"), s2.json().get("listen_port")}
    # bekannter Fund AUDIT-038: gleicher Port bei gleichzeitigem Öffnen – hier nur protokolliert
    LOG.append(f"{'OK ' if len(ports) == 2 else 'FUND'} parallele Fernzugriffe: Ports {sorted(ports)} (AUDIT-038)")
    for s in (s1, s2):
        if s.status_code == 201:
            await client.post(f"/api/v1/remote-sessions/{s.json()['id']}/close", headers=h)
    users = [u for u in rt.tables["/user"] if str(u.get("name", "")).startswith("sdwan-rs-")]
    _log("nach Schließen keine temporären Benutzer", not users, users)
    errs = [x for x in LOG if x.startswith("ERR")]
    _flush("C.3 Parallelität")
    assert not errs, "\n".join(errs)
