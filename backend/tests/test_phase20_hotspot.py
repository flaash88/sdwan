"""Phase 20 – Hotspot/Gäste-Portal: Vorlagen, Login-Seiten, Ausrollen, Voucher, Live-Gäste, Registrierung, DSGVO, Lint-Hinweis."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import update

from app.db import system_session
from app.models import GuestRegistration, HotspotPortal
from app.routeros.simulator import get_router
from app.services import hotspot as hs
from app.services.fw_lint import DeviceCtx, lint_spec
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin
from tests.test_phase14_fw_editor import CAT


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="gw1")
    return t, h, dev


async def _portal(client, h, seed_name: str) -> dict:
    return next(p for p in (await client.get("/api/v1/hotspot/portals", headers=h)).json() if p["name"] == seed_name)


async def _instance(client, h, dev, portal, slug="lobby", **kw) -> dict:
    body = {"name": "Lobby", "slug": slug, "device_id": dev["id"], "portal_id": portal["id"], "interface": "bridge",
            "walled_garden": ["example.com"], "session_timeout_min": 120, "rate_limit": "5M/20M", **kw}
    r = await client.post("/api/v1/hotspot/instances", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def test_seed_templates_readonly_and_copy(client, msp, hub):
    _t, h, _dev = await _setup(client, msp)
    ps = (await client.get("/api/v1/hotspot/portals", headers=h)).json()
    names = {p["name"]: p for p in ps if p["builtin"]}
    assert set(names) == {"Hotel", "Gastronomie", "Veranstaltung", "Büro-Gäste"}
    assert names["Hotel"]["login_type"] == "voucher" and names["Gastronomie"]["login_type"] == "click"
    assert [f["key"] for f in names["Büro-Gäste"]["form_fields"]] == ["name", "company", "host"]
    body = {k: names["Hotel"][k] for k in ("name", "login_type", "design", "texts", "terms_required")}
    assert (await client.put(f"/api/v1/hotspot/portals/{names['Hotel']['id']}", json=body, headers=h)).status_code == 403
    cp = (await client.post(f"/api/v1/hotspot/portals/{names['Hotel']['id']}/copy", headers=h)).json()
    assert cp["scope"] == "tenant" and not cp["builtin"]
    body["design"] = {**body["design"], "primary": "#ff0000"}
    assert (await client.put(f"/api/v1/hotspot/portals/{cp['id']}", json=body, headers=h)).json()["version"] == 2
    bad = {**body, "design": {"primary": "red"}}
    assert (await client.put(f"/api/v1/hotspot/portals/{cp['id']}", json=bad, headers=h)).status_code == 422
    html = (await client.get(f"/api/v1/hotspot/portals/{cp['id']}/preview", headers=h)).text
    assert "#ff0000" in html and "$(" not in html


def test_render_pages_escape_and_types():
    p = HotspotPortal(login_type="voucher", design={}, terms_required=True, form_fields=[], custom_files={},
                      texts={"de": {"title": "Hallo $(username) <b>", "button": "Los"}, "en": {"title": "Hi"}})
    from app.models import HotspotInstance
    import uuid

    inst = HotspotInstance(id=uuid.uuid4(), slug="x", name="x")
    pages = hs.render_pages(p, inst)
    assert set(pages) == {"login.html", "status.html", "alogin.html", "logout.html"}
    login = pages["login.html"]
    assert 'action="$(link-login-only)"' in login and 'name="username"' in login and 'id="terms" required' in login
    assert "&#36;(username) &lt;b&gt;" in login  # Portal-Text kann keine RouterOS-Variablen einschleusen
    assert "html[lang=de] [data-l=de]" in login and "e.style.display" not in login  # Sprachumschaltung per CSS
    p.login_type = "click"
    assert 'value="T-$(mac-esc)"' in hs.render_pages(p, inst)["login.html"]
    p.login_type, p.form_fields = "form", [{"key": "name", "label_de": "Name", "label_en": "Name", "type": "text", "required": True, "max_len": 50}]
    login = hs.render_pages(p, inst)["login.html"]
    assert f"/api/v1/portal/{inst.id}/register" in login and 'data-f="name"' in login
    p.custom_files = {"login.html": "<html>eigen</html>"}
    assert hs.render_pages(p, inst)["login.html"] == "<html>eigen</html>"
    assert hs.parse_uptime("1d02:03:04") == 93784 and hs.parse_uptime("1h30m") == 5400


async def test_apply_click_portal_and_remove(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = get_router(dev["tunnel_ip"])
    defaults = ([dict(x) for x in r.tables["/ip/hotspot/profile"]], [dict(x) for x in r.tables["/ip/hotspot/user/profile"]])
    inst = await _instance(client, h, dev, await _portal(client, h, "Gastronomie"))
    assert inst["undeployed"] and inst["status"] == "pending"
    res = await client.post(f"/api/v1/hotspot/instances/{inst['id']}/apply", headers=h)
    assert res.status_code == 200, res.text
    assert not res.json()["instance"]["undeployed"]
    prof = next(x for x in r.tables["/ip/hotspot/profile"] if x["name"] == "sdwan-hs-lobby")
    assert prof["hotspot-address"] == "192.168.88.1" and prof["html-directory"] == "sdwan-hs-lobby"
    assert prof["login-by"] == "http-pap,trial" and prof["trial-uptime-limit"] == "120m" and prof["trial-user-profile"] == "sdwan-hs-lobby-trial"
    (srv,) = r.tables["/ip/hotspot"]
    assert srv["interface"] == "bridge" and srv["address-pool"] == "none" and srv["profile"] == "sdwan-hs-lobby"
    trial = next(x for x in r.tables["/ip/hotspot/user/profile"] if x["name"] == "sdwan-hs-lobby-trial")
    assert trial["rate-limit"] == "5M/20M"
    assert [x["dst-host"] for x in r.tables["/ip/hotspot/walled-garden"]] == ["example.com"]
    assert r.tables["/ip/hotspot/walled-garden/ip"] == []  # Klick-Anmeldung braucht die Plattform nicht
    files = {x["name"] for x in r.tables["/file"]}
    assert {"sdwan-hs-lobby/login.html", "sdwan-hs-lobby/status.html"} <= files
    # Entfernen: alles weg, Standardprofile unverändert
    assert (await client.delete(f"/api/v1/hotspot/instances/{inst['id']}", headers=h)).status_code == 204
    assert r.tables["/ip/hotspot"] == [] and r.tables["/ip/hotspot/walled-garden"] == []
    assert (r.tables["/ip/hotspot/profile"], r.tables["/ip/hotspot/user/profile"]) == defaults


async def test_interface_without_address_errors(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    inst = await _instance(client, h, dev, await _portal(client, h, "Hotel"), interface="ether5")
    r = await client.post(f"/api/v1/hotspot/instances/{inst['id']}/apply", headers=h)
    assert r.status_code == 502 and "keine IP-Adresse" in r.json()["detail"]
    assert get_router(dev["tunnel_ip"]).tables["/ip/hotspot"] == []


async def test_vouchers_batch_print_csv_status_block(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = get_router(dev["tunnel_ip"])
    inst = await _instance(client, h, dev, await _portal(client, h, "Hotel"), dns_name="wlan.example.net")
    vp = (await client.post("/api/v1/hotspot/voucher-profiles", json={"name": "1 Tag", "slug": "day", "validity_min": 1440, "data_limit_mb": 2048,
                                                                     "rate_limit": "2M/10M"}, headers=h)).json()
    b = (await client.post(f"/api/v1/hotspot/instances/{inst['id']}/batches", json={"profile_id": vp["id"], "count": 5, "note": "Rezeption"}, headers=h)).json()
    assert b["pushed"] and b["count"] == 5
    users = [u for u in r.tables["/ip/hotspot/user"] if u.get("comment") == "sdwan:hs:lobby:v"]
    assert len(users) == 5 and all(u["limit-uptime"] == "1440m" and u["limit-bytes-total"] == str(2048 * 1024 * 1024) for u in users)
    assert all(u["profile"] == "sdwan-hs-lobby-v-day" and u["server"] == "sdwan-hs-lobby" and u["password"] == "" for u in users)
    up = next(x for x in r.tables["/ip/hotspot/user/profile"] if x["name"] == "sdwan-hs-lobby-v-day")
    assert up["rate-limit"] == "2M/10M"
    pr = (await client.get(f"/api/v1/hotspot/batches/{b['batch_id']}/print", headers=h)).json()
    assert len(pr["vouchers"]) == 5 and pr["login_url"] == "http://wlan.example.net/login" and pr["vouchers"][0]["qr_svg"].startswith("<svg")
    csv = await client.get(f"/api/v1/hotspot/batches/{b['batch_id']}/csv", headers=h)
    assert csv.status_code == 200 and csv.text.count("\n") == 6 and "Rezeption" not in csv.text
    # Status aus dem Router
    users[0]["uptime"], users[0]["bytes-in"], users[0]["bytes-out"] = "10m", "1000", "5000"
    users[1]["uptime"] = "1d00:00:00"
    await hs.hotspot_tick()
    vs = {v["code"]: v for v in (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/vouchers", headers=h)).json()["vouchers"]}
    assert vs[users[0]["name"]]["status"] == "active" and vs[users[0]["name"]]["bytes_total"] == 6000
    assert vs[users[1]["name"]]["status"] == "used"
    # Voucher sperren
    blk = await client.post(f"/api/v1/hotspot/vouchers/{vs[users[2]['name']]['id']}/block", headers=h)
    assert blk.json()["status"] == "blocked" and users[2]["disabled"] == "yes"
    assert len((await client.get(f"/api/v1/hotspot/batches/{b['batch_id']}/print", headers=h)).json()["vouchers"]) == 4


async def test_live_guests_disconnect_block_unblock(client, msp, hub):
    _t, h, dev = await _setup(client, msp)
    r = get_router(dev["tunnel_ip"])
    inst = await _instance(client, h, dev, await _portal(client, h, "Gastronomie"))
    await client.post(f"/api/v1/hotspot/instances/{inst['id']}/apply", headers=h)
    aid = r._insert("/ip/hotspot/active", {"server": "sdwan-hs-lobby", "user": "T-02:00:00:00:00:09", "address": "192.168.88.50",
                                          "mac-address": "02:00:00:00:00:09", "uptime": "3m", "bytes-in": "100", "bytes-out": "900"})
    r._insert("/ip/hotspot/active", {"server": "anderer", "user": "x", "mac-address": "02:00:00:00:00:0A"})
    live = (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/active", headers=h)).json()
    assert [g["mac"] for g in live["active"]] == ["02:00:00:00:00:09"] and live["active"][0]["trial"]
    res = await client.post(f"/api/v1/hotspot/instances/{inst['id']}/block", json={"active_id": aid, "user": "T-02:00:00:00:00:09",
                                                                                    "mac": "02:00:00:00:00:09"}, headers=h)
    assert res.status_code == 200
    (binding,) = r.tables["/ip/hotspot/ip-binding"]
    assert binding["type"] == "blocked" and binding["mac-address"] == "02:00:00:00:00:09"
    assert all(a[".id"] != aid for a in r.tables["/ip/hotspot/active"])
    live = (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/active", headers=h)).json()
    assert len(live["blocked"]) == 1
    assert (await client.delete(f"/api/v1/hotspot/instances/{inst['id']}/blocked/{live['blocked'][0]['id']}", headers=h)).status_code == 204
    assert r.tables["/ip/hotspot/ip-binding"] == []
    assert (await client.get("/api/v1/audit?action=hotspot.guest.block", headers=h)).json()


async def test_register_endpoint_limits_fields_retention(client, msp, hub):
    t, h, dev = await _setup(client, msp)
    inst = await _instance(client, h, dev, await _portal(client, h, "Büro-Gäste"))
    await client.post(f"/api/v1/hotspot/instances/{inst['id']}/apply", headers=h)
    r = get_router(dev["tunnel_ip"])
    assert [x["dst-host"] for x in r.tables["/ip/hotspot/walled-garden/ip"]] == [hs.platform_host()]  # Plattform für das Formular
    url = f"/api/v1/portal/{inst['id']}/register"
    pre = await client.options(url)
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    ok = await client.post(url, json={"fields": {"name": " Erika ", "company": "Beispiel GmbH", "evil": "x" * 50}, "terms_accepted": True, "lang": "de"})
    assert ok.status_code == 201 and ok.headers["access-control-allow-origin"] == "*"
    assert (await client.post(url, json={"fields": {"name": "A"}, "terms_accepted": False})).status_code == 422
    assert (await client.post(url, json={"fields": {"company": "x"}, "terms_accepted": True})).status_code == 422  # Pflichtfeld
    assert (await client.post(url, json={"fields": {"name": "x" * 101}, "terms_accepted": True})).status_code == 422
    assert (await client.post(url, content=b'{"fields":{"name":"' + b"x" * 5000 + b'"}}', headers={"content-type": "application/json"})).status_code == 413
    regs = (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/registrations", headers=h)).json()
    assert regs["retention_days"] == 30 and [x["data"] for x in regs["registrations"]] == [{"name": "Erika", "company": "Beispiel GmbH", "host": ""}]
    # Rate-Limit je IP und Portal
    codes = [(await client.post(url, json={"fields": {"name": "B"}, "terms_accepted": True})).status_code for _ in range(12)]
    assert 429 in codes
    # Nur Formular-Portale; unbekannte Hotspots
    other = await _instance(client, h, dev, await _portal(client, h, "Hotel"), slug="other")
    assert (await client.post(f"/api/v1/portal/{other['id']}/register", json={"fields": {}, "terms_accepted": True})).status_code == 404
    # Aufbewahrung: ältere Einträge löscht der Job
    await client.put("/api/v1/hotspot/retention", json={"days": 7}, headers=h)
    async with system_session() as db:
        await db.execute(update(GuestRegistration).values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=8)))
        await db.commit()
    assert await hs.purge_registrations() >= 1
    assert (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/registrations", headers=h)).json()["registrations"] == []
    # Registrierungen nur für Admins, andere Mandanten sehen nichts
    ro = await make_tenant_admin(client, msp, t["id"], email="ro@acme.example.com", role="readonly")
    assert (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/registrations", headers=ro)).status_code == 403
    assert (await client.post("/api/v1/hotspot/instances", json={"name": "x", "slug": "x", "device_id": dev["id"], "portal_id": other["portal_id"],
                                                                 "interface": "bridge"}, headers=ro)).status_code == 403
    t2 = await make_tenant(client, msp, slug="other")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="admin@other.example.com")
    assert (await client.get("/api/v1/hotspot/instances", headers=h2)).json() == []
    assert (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/active", headers=h2)).status_code == 404


def test_lint_suggests_guest_isolation_for_hotspot_devices():
    cat = CAT
    cat.zones["zg"] = {"id": "zg", "tenant_id": None, "name": "Gäste", "slug": "guest", "source": "manual", "management": False}
    try:
        spec = {"rules": [{"id": "b0000001", "enabled": True, "action": "accept", "src_zone": "zl", "dst_zone": "zw"}], "nat": []}
        devs = [DeviceCtx(name="gw1", zone_ids={"zl", "zm", "zg"}, has_wan=True, has_hotspot=True)]
        assert "hotspot_isolation" in {i["code"] for i in lint_spec(spec, cat, devs)}
        spec["rules"].insert(0, {"id": "b0000002", "enabled": True, "action": "drop", "src_zone": "zg", "dst_zone": "zl"})
        assert "hotspot_isolation" not in {i["code"] for i in lint_spec(spec, cat, devs)}
        devs[0].has_hotspot = False
        assert "hotspot_isolation" not in {i["code"] for i in lint_spec({**spec, "rules": spec["rules"][1:]}, cat, devs)}
    finally:
        del cat.zones["zg"]
