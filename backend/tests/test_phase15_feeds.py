"""Phase 15 – Threat-Feeds: Parsen/Prüfen, Differenz-Verteilung, RAM-Check, letzte gültige Liste, feed_stale."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import update

from app.db import system_session
from app.models import ThreatFeed
from app.routeros.simulator import get_router
from app.services import feeds as feeds_mod
from app.services.alerts import evaluate_all
from app.services.feeds import feeds_tick, parse_feed
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

# Beispieldaten aus Dokumentations-/öffentlichen Beispielnetzen
LINES = """; Kommentar
192.0.2.0/24 ; SBL1
198.51.100.0/24
10.0.0.0/8        # privat -> verworfen
0.0.0.0/0         # zu weit -> verworfen
kaputt
203.0.113.7
2001:db8::/32
2a00:1450::/29
"""


def test_parse_lines_and_jsonl():
    nets, rej = parse_feed(LINES)
    # Dokumentationsnetze (192.0.2/24, 198.51.100/24, 203.0.113/24, 2001:db8::/32) sind nicht global -> verworfen
    assert nets == ["2a00:1450::/29"] and rej == 7
    real = "1.10.16.0/20 ; SBL256894\n2.56.192.0/22\n8.8.8.8\n"
    assert parse_feed(real)[0] == ["1.10.16.0/20", "2.56.192.0/22", "8.8.8.8/32"]
    jl = '{"cidr":"1.10.16.0/20","sblid":"SBL1"}\n{"cidr":"10.1.0.0/16"}\n{"type":"metadata","records":2}\n'
    assert parse_feed(jl, "jsonl") == (["1.10.16.0/20"], 1)


async def _setup(client, msp, monkeypatch, text="1.10.16.0/20\n2.56.192.0/22\n2a00:1450::/29\n"):
    state = {"text": text, "fail": False}

    async def fake_download(url):
        if state["fail"]:
            raise RuntimeError("HTTP 503")
        return state["text"]

    monkeypatch.setattr(feeds_mod, "_download", fake_download)
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name="fw1")
    await poll_all()
    r = await client.post("/api/v1/feeds", json={"name": "Eigene Liste", "url": "https://feeds.example/list.txt", "interval_min": 60}, headers=h)
    assert r.status_code == 201, r.text
    return h, dev, r.json(), state


def _entries(rt, path="/ip/firewall/address-list"):
    return sorted(str(x["address"]) for x in rt.tables[path] if x.get("list") == "sdwan-feed-eigene-liste")


async def test_feed_distribution_diff_and_last_good(client, msp, hub, monkeypatch):
    h, dev, feed, state = await _setup(client, msp, monkeypatch)
    rt = get_router(dev["tunnel_ip"])
    rt.tables["/ip/firewall/address-list"].append({".id": "*MAN", "list": "manuell", "address": "1.10.16.0/20"})
    assert (await client.post(f"/api/v1/feeds/{feed['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)).json()["assigned"]
    await feeds_tick()
    assert _entries(rt) == ["1.10.16.0/20", "2.56.192.0/22"]
    assert _entries(rt, "/ipv6/firewall/address-list") == ["2a00:1450::/29"]
    ids_before = {x[".id"] for x in rt.tables["/ip/firewall/address-list"]}
    # nur Differenzen: ein Eintrag weg, einer neu
    state["text"] = "1.10.16.0/20\n5.8.37.0/24\n"
    r = (await client.post(f"/api/v1/feeds/{feed['id']}/refresh", headers=h)).json()
    assert r["changed"] and r["count"] == 2 and r["assignments"][0]["status"] == "ok"
    assert _entries(rt) == ["1.10.16.0/20", "5.8.37.0/24"] and _entries(rt, "/ipv6/firewall/address-list") == []
    kept = next(x for x in rt.tables["/ip/firewall/address-list"] if x.get("address") == "1.10.16.0/20" and x.get("list") == "sdwan-feed-eigene-liste")
    assert kept[".id"] in ids_before  # unveränderter Eintrag wurde nicht neu angelegt
    assert any(x[".id"] == "*MAN" for x in rt.tables["/ip/firewall/address-list"])  # manuelle Liste unberührt
    # Fehler beim Laden -> letzte gültige Liste bleibt
    state["fail"] = True
    r = (await client.post(f"/api/v1/feeds/{feed['id']}/refresh", headers=h)).json()
    assert r["last_error"] == "HTTP 503" and r["count"] == 2 and _entries(rt) == ["1.10.16.0/20", "5.8.37.0/24"]
    # zu viele Einträge -> abgelehnt
    state.update(fail=False, text="\n".join(f"5.{i}.0.0/16" for i in range(20)))
    await client.patch(f"/api/v1/feeds/{feed['id']}", json={"name": "Eigene Liste", "url": "https://feeds.example/list.txt", "max_entries": 10}, headers=h)
    r = (await client.post(f"/api/v1/feeds/{feed['id']}/refresh", headers=h)).json()
    assert "Obergrenze" in r["last_error"] and r["count"] == 2
    # Entzug entfernt die Liste vom Router
    r = await client.delete(f"/api/v1/feeds/{feed['id']}/assign/{dev['id']}", headers=h)
    assert r.json()["removed_from_router"] and _entries(rt) == []


async def test_ram_check_skips_device(client, msp, hub, monkeypatch):
    h, dev, feed, state = await _setup(client, msp, monkeypatch)
    rt = get_router(dev["tunnel_ip"])
    monkeypatch.setattr(feeds_mod, "memory_needed", lambda n: 10**12)
    await client.post(f"/api/v1/feeds/{feed['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await feeds_tick()
    f = (await client.get(f"/api/v1/feeds/{feed['id']}", headers=h)).json()
    a = f["assignments"][0]
    assert a["status"] == "skipped_memory" and "Zu wenig freier Speicher" in a["last_error"]
    assert _entries(rt) == []


async def test_feed_stale_alert_and_rbac(client, msp, hub, monkeypatch):
    h, dev, feed, state = await _setup(client, msp, monkeypatch)
    await client.post("/api/v1/alert-rules", json={"name": "Feed", "type": "feed_stale", "severity": "warning", "duration_s": 0}, headers=h)
    await client.post(f"/api/v1/feeds/{feed['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await feeds_tick()
    await evaluate_all()
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []
    async with system_session() as db:
        await db.execute(update(ThreatFeed).values(last_ok_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=4)))
        await db.commit()
    await evaluate_all()
    alerts = (await client.get("/api/v1/alerts", headers=h)).json()
    assert len(alerts) == 1 and "nicht aktualisiert" in alerts[0]["message"]
    tech = await make_tenant_admin(client, msp, dev["tenant_id"], email="tech@acme.example.com", role="technician")
    assert (await client.post("/api/v1/feeds", json={"name": "X", "url": "https://x.example/"}, headers=tech)).status_code == 403


async def test_seed_feeds_and_editor_object(client, msp, hub, monkeypatch):
    h, dev, feed, state = await _setup(client, msp, monkeypatch)
    lst = (await client.get("/api/v1/feeds", headers=h)).json()
    seeded = [f for f in lst if f["builtin"]]
    assert {f["slug"] for f in seeded} == {"spamhaus-drop4", "spamhaus-drop6"} and all(f["scope"] == "global" for f in seeded)
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    objs = {o["slug"]: o for o in cat["objects"] if o["kind"] == "feed"}
    assert {"spamhaus-drop4", "eigene-liste"} <= set(objs)
    block = next(b for b in cat["blocks"] if b["name"] == "Threat-Feeds eingehend verwerfen")
    ex = (await client.post(f"/api/v1/fw/blocks/{block['id']}/expand", json={"params": {"feed": [objs["eigene-liste"]["id"]]}}, headers=h)).json()
    pol = (await client.post("/api/v1/policies", json={"name": "Feeds", "mode": "simple", "spec": {"options": {"default_drop": False}, "rules": ex["rules"]}}, headers=h)).json()
    lists = {r.get("src-address-list") or r.get("dst-address-list") for r in pol["content"]["filter"] if r["comment"].startswith("r:")}
    assert lists == {"sdwan-feed-eigene-liste"} and not any(a["list"] == "sdwan-feed-eigene-liste" for a in pol["content"]["address_lists"])
    # Feed in Verwendung -> nicht löschbar; vordefinierter Feed nicht löschbar
    assert (await client.delete(f"/api/v1/feeds/{feed['id']}", headers=h)).status_code == 409
    assert (await client.delete(f"/api/v1/feeds/{seeded[0]['id']}", headers=msp)).status_code == 403
