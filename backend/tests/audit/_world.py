"""Audit-Hilfen: zwei Mandanten mit allen Rollen und möglichst vielen Objekten (über die API angelegt).

Alle Objekte von Mandant B tragen den Marker ``BMARK`` im Namen, damit Listen-Endpunkte auf Datenabfluss geprüft
werden können. Anlegen ist bewusst tolerant: scheitert ein Objekt, fehlt nur dieser Teil der Matrix (wird gemeldet).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db import system_session
from tests.conftest import login, make_paired_device

BMARK = "bmarkzz"


@dataclass
class Tenant:
    id: str
    slug: str
    admin: dict[str, str]
    tech: dict[str, str]
    ro: dict[str, str]
    token_role: dict[str, str] = field(default_factory=dict)
    token_read: dict[str, str] = field(default_factory=dict)
    ids: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


async def _user(client, msp, tid: str, email: str, role: str) -> dict[str, str]:
    r = await client.post("/api/v1/users", json={"email": email, "password": "password123", "role": role, "tenant_id": tid}, headers=msp)
    assert r.status_code == 201, r.text
    return await login(client, email, "password123")


async def _try(t: Tenant, label: str, coro) -> Any:
    try:
        r = await coro
    except Exception as exc:  # noqa: BLE001 - Audit: Aufbau darf nicht abbrechen
        t.failures.append(f"{label}: {exc!r}")
        return None
    if r.status_code >= 300:
        t.failures.append(f"{label}: {r.status_code} {r.text[:160]}")
        return None
    try:
        return r.json()
    except ValueError:
        return {}


async def build_tenant(client, msp, slug: str, mark: str = "") -> Tenant:
    r = await client.post("/api/v1/tenants", json={"name": f"{slug.title()}{mark}", "slug": slug}, headers=msp)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = Tenant(id=tid, slug=slug, admin=await _user(client, msp, tid, f"admin@{slug}.example.com", "admin"),
               tech=await _user(client, msp, tid, f"tech@{slug}.example.com", "technician"),
               ro=await _user(client, msp, tid, f"ro@{slug}.example.com", "readonly"))
    h = t.admin
    site = await _try(t, "site", client.post("/api/v1/sites", json={"name": f"Standort{mark}", "lan_subnets": ["10.9.0.0/24"]}, headers=h))
    t.ids["site_id"] = site and site["id"]
    dev = await make_paired_device(client, h, site_id=t.ids["site_id"], name=f"r1{mark}")
    t.ids["device_id"] = dev["id"]
    dev2 = await make_paired_device(client, h, name=f"r2{mark}")
    t.ids["device2_id"] = dev2["id"]
    wan = await _try(t, "wan", client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": [
        {"name": f"Glasfaser{mark}", "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"}]}, headers=h))
    if wan:
        links = (await client.get(f"/api/v1/devices/{dev['id']}/wan", headers=h)).json().get("links", [])
        t.ids["link_id"] = links[0]["id"] if links else None
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    zones = {z["slug"]: z["id"] for z in cat["zones"]}
    pol = await _try(t, "policy", client.post("/api/v1/policies", json={"name": f"FW{mark}", "mode": "simple", "spec": {"rules": [
        {"src_zone": zones.get("lan"), "dst_zone": zones.get("lan"), "action": "accept"}]}}, headers=h))
    t.ids["policy_id"] = pol and pol["id"]
    if pol:
        await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
        d = await _try(t, "deploy", client.post(f"/api/v1/policies/{pol['id']}/deploy", json={"confirm_lint": True}, headers=h))
        t.ids["deployment_id"] = d and (d.get("id") or d.get("deployment_id"))
    obj = await _try(t, "fw object", client.post("/api/v1/fw/objects", json={"name": f"Obj{mark}", "kind": "host", "values": ["192.0.2.50"]}, headers=h))
    t.ids["fw_object_id"] = obj and obj["id"]
    blk = await _try(t, "fw block", client.post("/api/v1/fw/blocks", json={"name": f"Block{mark}"}, headers=h))
    t.ids["block_id"] = blk and blk["id"]
    prof = await _try(t, "wlan", client.post("/api/v1/wlan/profiles", json={"name": f"WLAN{mark}", "slug": f"w{slug[:8]}", "ssid": "Beispiel",
                                                                           "security": "wpa2-psk", "passphrase": "geheim-12345"}, headers=h))
    t.ids["wlan_profile_id"] = prof and prof["id"]
    portals = (await client.get("/api/v1/hotspot/portals", headers=h)).json()
    own = await _try(t, "portal copy", client.post(f"/api/v1/hotspot/portals/{portals[0]['id']}/copy", json={"name": f"Portal{mark}"}, headers=h))
    t.ids["portal_id"] = own and own.get("id")
    inst = await _try(t, "hotspot", client.post("/api/v1/hotspot/instances", json={"name": f"HS{mark}", "slug": f"hs{slug[:6]}", "device_id": dev["id"],
                                                                               "portal_id": portals[0]["id"], "interface": "bridge"}, headers=h))
    t.ids["instance_id"] = inst and inst["id"]
    vp = await _try(t, "voucher profile", client.post("/api/v1/hotspot/voucher-profiles", json={"name": f"Tag{mark}", "slug": f"d{slug[:6]}",
                                                                                              "validity_min": 1440}, headers=h))
    t.ids["voucher_profile_id"] = vp and vp["id"]
    if inst and vp:
        b = await _try(t, "vouchers", client.post(f"/api/v1/hotspot/instances/{inst['id']}/batches", json={"profile_id": vp["id"], "count": 2,
                                                                                                        "note": f"Batch{mark}"}, headers=h))
        if b:
            t.ids["batch_id"] = b.get("batch_id") or (b.get("batch") or {}).get("id")
            vs = (await client.get(f"/api/v1/hotspot/instances/{inst['id']}/vouchers", headers=h)).json()
            vs = vs.get("vouchers", vs) if isinstance(vs, dict) else vs
            t.ids["voucher_id"] = vs[0]["id"] if vs and isinstance(vs[0], dict) and "id" in vs[0] else None
    cf = await _try(t, "content filter", client.post("/api/v1/content-filter/profiles", json={"name": f"CF{mark}"}, headers=h))
    t.ids["cf_profile_id"] = cf and cf["id"]
    feed = await _try(t, "feed", client.post("/api/v1/feeds", json={"name": f"Feed{mark}", "url": "https://feeds.example/list.txt"}, headers=h))
    t.ids["feed_id"] = feed and feed["id"]
    sc = await _try(t, "script", client.post("/api/v1/scripts", json={"name": f"Skript{mark}", "category": "read",
                                                                   "content": ':put "hallo"'}, headers=h))
    t.ids["script_id"] = sc and sc["id"]
    if sc:
        run = await _try(t, "script run", client.post("/api/v1/script-runs", json={"script_id": sc["id"], "device_ids": [dev["id"]]}, headers=h))
        t.ids["run_id"] = run and run["id"]
    ar = await _try(t, "alert rule", client.post("/api/v1/alert-rules", json={"name": f"Offline{mark}", "type": "device_offline",
                                                                          "duration_s": 0}, headers=h))
    t.ids["rule_id"] = ar and ar["id"]
    tpl = await _try(t, "ztp template", client.post("/api/v1/ztp/templates", json={"name": f"Vorlage{mark}", "content": {}}, headers=h))
    t.ids["template_id"] = tpl and tpl["id"]
    now = dt.datetime.now(dt.UTC)
    mw = await _try(t, "maintenance", client.post("/api/v1/maintenance-windows", json={
        "name": f"Nacht{mark}", "kind": "once", "start_at": (now + dt.timedelta(days=1)).isoformat(),
        "end_at": (now + dt.timedelta(days=1, hours=2)).isoformat()}, headers=h))
    t.ids["window_id"] = mw and mw["id"]
    cs = await _try(t, "compliance set", client.post("/api/v1/compliance/rule-sets", json={"name": f"Regeln{mark}", "rules": []}, headers=h))
    t.ids["set_id"] = cs and cs["id"]
    if cs:
        a = await _try(t, "compliance assign", client.post(f"/api/v1/compliance/rule-sets/{cs['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h))
        t.ids["assignment_id"] = a and (a.get("id") if isinstance(a, dict) else None)
    bk = await _try(t, "backup", client.post(f"/api/v1/devices/{dev['id']}/backups", headers=h))
    t.ids["backup_id"] = bk and (bk.get("id") or (bk.get("backup") or {}).get("id"))
    fj = await _try(t, "firmware job", client.post("/api/v1/firmware/jobs", json={"name": f"FW{mark}", "device_ids": [dev["id"]], "batch_size": 1,
                                                                             "batch_interval_s": 3600}, headers=h))
    t.ids["job_id"] = fj and fj["id"]
    rs = await _try(t, "remote session", client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "duration_minutes": 15,
                                                                                                        "allowed_cidr": "192.0.2.0/24"}, headers=h))
    t.ids["session_id"] = rs and rs["id"]
    tok = await _try(t, "api token role", client.post("/api/v1/auth/api-tokens", json={"name": f"Tok{mark}", "scope": "role"}, headers=h))
    if tok:
        t.ids["token_id"] = tok["id"]
        t.token_role = {"Authorization": f"Bearer {tok['token']}"}
    tok2 = await _try(t, "api token read", client.post("/api/v1/auth/api-tokens", json={"name": f"Ro{mark}", "scope": "read"}, headers=h))
    if tok2:
        t.token_read = {"Authorization": f"Bearer {tok2['token']}"}
    # Offboarding-Archiv: zweites Gerät „nur aus der Plattform entfernen“
    off = await _try(t, "offboard", client.post(f"/api/v1/devices/{dev2['id']}/offboard", json={"mode": "platform_only", "confirm_name": f"r2{mark}"},
                                                headers=h))
    t.ids["archive_id"] = off and off.get("archive_id")
    await _db_ids(t)
    return t


async def _db_ids(t: Tenant) -> None:
    """IDs, die sich nicht bequem über die API ermitteln lassen, direkt aus der DB (nur dieses Mandanten)."""
    from app.models import Alert, AlertRule, SlaReport, User

    async with system_session() as db:
        tid = uuid.UUID(t.id)
        a = (await db.execute(select(Alert.id).where(Alert.tenant_id == tid).limit(1))).scalar()
        t.ids.setdefault("alert_id", a and str(a))
        rep = (await db.execute(select(SlaReport.id).where(SlaReport.tenant_id == tid).limit(1))).scalar()
        t.ids.setdefault("report_id", rep and str(rep))
        u = (await db.execute(select(User.id).where(User.tenant_id == tid, User.email.like("ro@%")))).scalar()
        t.ids["user_id"] = str(u)
        if not t.ids.get("rule_id"):
            r = (await db.execute(select(AlertRule.id).where(AlertRule.tenant_id == tid).limit(1))).scalar()
            t.ids["rule_id"] = r and str(r)


# Pfadparameter -> Schlüssel in Tenant.ids (je nach Pfad)
def param_value(t: Tenant, path: str, name: str) -> str | None:
    if name == "profile_id":
        if "/wlan/" in path:
            return t.ids.get("wlan_profile_id")
        if "/voucher-profiles/" in path:
            return t.ids.get("voucher_profile_id")
        if "/content-filter/" in path:
            return t.ids.get("cf_profile_id")
    if name == "item_id":
        return t.ids.get("fw_object_id")
    if name == "kind":
        return "objects"
    if name == "slot":
        return "1"
    if name == "action":
        return "cancel"
    if name == "tenant_id":
        return t.id
    return t.ids.get(name)
