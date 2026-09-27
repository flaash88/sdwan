"""Phase 24 – Vor-Ort-Zugang (Break-Glass) und API-Tokens."""

from __future__ import annotations

import io
import ipaddress

import pyzipper
from pyrage import decrypt, x25519

from app.config import get_settings
from app.routeros.simulator import get_router
from app.services.fw_compile import LOCAL_ACCESS_LIST
from app.services.local_access import PASSWORD_ALPHABET, PASSWORD_LENGTH, rotation_tick
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin
from tests.test_phase14_defconf import _factory

WAN_NET = "203.0.113.0/24"
_KNOWN = {".id", "chain", "action", "comment", "disabled", "protocol", "dst-port", "in-interface", "in-interface-list", "src-address",
          "connection-state", "log", "log-prefix", "dynamic", "invalid", "bytes", "packets"}


def _members(rt, lst: str) -> set[str]:
    return {m["interface"] for m in rt.tables["/interface/list/member"] if m.get("list") == lst}


def _ports(spec: str) -> set[int]:
    out: set[int] = set()
    for p in str(spec).split(","):
        a, _, b = p.partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


def verdict(rt, iface: str, proto: str, port: int, src: str = "192.168.88.10") -> str:
    """Erste passende Input-Regel für ein neues Paket (Firewall-Auswertung wie RouterOS, nur benötigte Felder)."""
    for r in rt.tables["/ip/firewall/filter"]:
        if r.get("chain") != "input" or str(r.get("disabled", "false")) in ("true", "yes"):
            continue
        unknown = set(r) - _KNOWN
        assert not unknown, f"Evaluator kennt Feld nicht: {unknown} in {r}"
        if "connection-state" in r and "new" not in r["connection-state"].split(","):
            continue
        if "protocol" in r and r["protocol"] != proto:
            continue
        if "dst-port" in r and port not in _ports(r["dst-port"]):
            continue
        if "in-interface" in r and r["in-interface"] != iface:
            continue
        if "in-interface-list" in r:
            neg = r["in-interface-list"].startswith("!")
            if (iface in _members(rt, r["in-interface-list"].lstrip("!"))) == neg:
                continue
        if "src-address" in r and ipaddress.ip_address(src) not in ipaddress.ip_network(r["src-address"], strict=False):
            continue
        return r["action"]
    return "accept"  # RouterOS: ohne passende Regel erlaubt


async def _device(client, msp, name="la1", factory=True):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h, name=name)
    rt = get_router(dev["tunnel_ip"])
    if factory:
        _factory(rt)  # defconf: LAN=bridge, WAN=ether1
    rt._insert("/ip/address", {"address": "203.0.113.2/24", "network": "203.0.113.0", "interface": "ether1", "dynamic": "false"})
    rt._insert("/ip/dhcp-client", {"interface": "ether1", "disabled": "false"})
    return t, h, dev, rt


async def _default_drop(client, h, dev):
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether3", "zone_id": z["management"]},
                                                                           {"interface": "bridge", "zone_id": z["lan"]}]}, headers=h)
    pol = (await client.post("/api/v1/policies", json={"name": "FW", "mode": "simple", "spec": {"rules": [
        {"src_zone": z["lan"], "dst_zone": z["lan"], "action": "accept"}]}}, headers=h)).json()
    assert "id" in pol, pol
    await client.post(f"/api/v1/policies/{pol['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    r = await client.post(f"/api/v1/policies/{pol['id']}/deploy", json={}, headers=h)
    assert r.status_code == 202, r.text


def _user(rt, name="localadmin"):
    return next((u for u in rt.tables["/user"] if u.get("name") == name), None)


async def test_created_after_pairing_local_only_and_default_drop(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    rt._insert("/ip/address", {"address": "10.9.9.1/24", "network": "10.9.9.0", "interface": "ether3", "dynamic": "false"})
    await _default_drop(client, h, dev)
    base = rt.tables["/ip/firewall/filter"]
    assert any(str(r.get("comment", "")).endswith("base:local-access") for r in base)
    # vor dem Anlegen: Liste leer → Default-Drop greift auch für WinBox aus dem LAN (bestehendes Verhalten unverändert)
    assert _members(rt, LOCAL_ACCESS_LIST) == set()
    assert verdict(rt, "bridge", "tcp", 8291) == "drop"
    st = (await client.get(f"/api/v1/devices/{dev['id']}/local-access", headers=h)).json()
    assert st["status"] == "pending"
    await poll_all()  # Post-Poll-Hook legt an
    st = (await client.get(f"/api/v1/devices/{dev['id']}/local-access", headers=h)).json()
    assert st["status"] == "active", st
    assert st["networks"] == ["10.9.9.0/24", "192.168.88.0/24"] and set(st["interfaces"]) == {"bridge", "ether3"}
    u = _user(rt)
    assert u["group"] == "sdwan-local" and u["comment"] == "sdwan:local" and u["address"] == "10.9.9.0/24,192.168.88.0/24"
    pw = u["password"]
    assert len(pw) >= 20 and len(pw) == PASSWORD_LENGTH and set(pw) <= set(PASSWORD_ALPHABET) and not set(pw) & set("0Ol1I")
    assert WAN_NET not in u["address"]
    # Default-Drop blockiert WinBox/SSH aus LAN/Management nicht – aus WAN nie
    for iface in ("bridge", "ether3"):
        assert verdict(rt, iface, "tcp", 8291) == "accept" and verdict(rt, iface, "tcp", 22) == "accept"
    assert verdict(rt, "ether1", "tcp", 8291, src="198.51.100.9") == "drop"
    assert verdict(rt, "ether1", "tcp", 22, src="198.51.100.9") == "drop"
    assert "ether1" not in _members(rt, LOCAL_ACCESS_LIST)
    assert rt._mac_winbox()["allowed-interface-list"] == LOCAL_ACCESS_LIST
    # zweites Gerät bekommt ein anderes Passwort
    dev2 = await make_paired_device(client, h, name="la2")
    _factory(get_router(dev2["tunnel_ip"]))
    await poll_all()
    assert _user(get_router(dev2["tunnel_ip"]))["password"] != pw
    # Liste (Mandant) und Gerätdetail zeigen kein Passwort
    lst = (await client.get("/api/v1/local-access", headers=h)).json()
    assert {x["access"]["status"] for x in lst} == {"active"} and pw not in str(lst) and "'password'" not in str(lst)


async def test_wan_zone_member_excluded(client, msp, hub):
    """Auch wenn ein WAN-Interface fälschlich in Zone LAN liegt: nie in der Liste, nie in address=."""
    t, h, dev, rt = await _device(client, msp)
    cat = (await client.get("/api/v1/fw/catalog", headers=h)).json()
    z = {x["slug"]: x["id"] for x in cat["zones"]}
    await client.put(f"/api/v1/devices/{dev['id']}/zones", json={"members": [{"interface": "ether1", "zone_id": z["lan"]},
                                                                           {"interface": "bridge", "zone_id": z["lan"]}]}, headers=h)
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={}, headers=h)
    assert r.json()["status"] == "active" and r.json()["interfaces"] == ["bridge"] and r.json()["networks"] == ["192.168.88.0/24"]


async def test_address_restrict_off_uses_services_and_restores(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    s = (await client.get("/api/v1/local-access/settings", headers=h)).json()
    assert s["local_admin_address_restrict"] is True and s["local_admin_name"] == "localadmin"
    r = await client.put("/api/v1/local-access/settings", json={**s, "local_admin_address_restrict": False, "local_admin_name": "vorort",
                                                              "local_access_webhook_url": None}, headers=h)
    assert r.status_code == 200, r.text
    await poll_all()  # neuer Datensatz aus dem Pairing nutzt noch den alten Namen – daher über Button neu
    await client.delete(f"/api/v1/devices/{dev['id']}/local-access", headers=h)
    assert _user(rt) is None and rt._mac_winbox()["allowed-interface-list"] == "LAN"
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={}, headers=h)
    assert r.json()["status"] == "active"
    u = _user(rt, "localadmin")  # Name bleibt je Gerät (Datensatz existiert)
    assert u["address"] == ""
    svc = {x["name"]: x for x in rt.tables["/ip/service"]}
    hub_ip = get_settings().wg_hub_ip
    assert svc["winbox"]["address"] == f"192.168.88.0/24,{hub_ip}/32" and svc["ssh"]["address"] == f"192.168.88.0/24,{hub_ip}/32"
    assert svc["www"]["address"] == ""  # andere Dienste unverändert
    # Compliance „nur Tunnel“ toleriert die lokalen Netze des aktiven Vor-Ort-Zugangs
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev["id"]]}, headers=h)
    rows = {r["rule_id"]: r for res in (await client.get(f"/api/v1/devices/{dev['id']}/compliance", headers=h)).json() for r in res["results"]}
    # nach dem Entfernen neu per Button angelegt → Gruppe nur mit der Schnittmenge → Warnung, kein Fehler
    assert rows["local-admin"]["status"] == "warn" and "eingeschränkt" in rows["local-admin"]["detail"]
    assert rows["mgmt-tunnel"]["status"] != "fail" or "ssh" not in rows["mgmt-tunnel"]["detail"]
    # Deaktivieren stellt Dienst-Adressen und MAC-WinBox zurück
    await client.delete(f"/api/v1/devices/{dev['id']}/local-access", headers=h)
    svc = {x["name"]: x for x in rt.tables["/ip/service"]}
    assert svc["winbox"]["address"] == "" and svc["ssh"]["address"] == "" and rt._mac_winbox()["allowed-interface-list"] == "LAN"
    assert not [m for m in rt.tables["/interface/list/member"] if m.get("list") == LOCAL_ACCESS_LIST]


async def test_not_created_manual_networks_and_compliance(client, msp, hub):
    t, h, dev, rt = await _device(client, msp, factory=False)  # keine defconf-Listen, keine Zonen
    await poll_all()
    st = (await client.get(f"/api/v1/devices/{dev['id']}/local-access", headers=h)).json()
    assert st["status"] == "not_created" and "manuell" in st["reason"] and _user(rt) is None
    lst = (await client.get("/api/v1/local-access", headers=h)).json()
    assert lst[0]["access"]["status"] == "not_created"
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev["id"]]}, headers=h)
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev["id"]]}, headers=h)
    rows = {r["rule_id"]: r for res in (await client.get(f"/api/v1/devices/{dev['id']}/compliance", headers=h)).json() for r in res["results"]}
    assert rows["local-admin"]["status"] == "fail" and "nicht angelegt" in rows["local-admin"]["detail"]  # kein stilles Grün
    # manuelle Netze: kein 0.0.0.0/0, keine WAN-Netze, nur gültige CIDR
    for bad in (["0.0.0.0/0"], ["203.0.113.128/25"], ["kaputt"]):
        r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={"manual_networks": bad}, headers=h)
        assert r.status_code == 422, bad
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={"manual_networks": ["192.168.88.0/24"]}, headers=h)
    assert r.json()["status"] == "active" and r.json()["interfaces"] == ["bridge"]
    assert _user(rt)["address"] == "192.168.88.0/24"
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev["id"]]}, headers=h)
    rows = {r["rule_id"]: r for res in (await client.get(f"/api/v1/devices/{dev['id']}/compliance", headers=h)).json() for r in res["results"]}
    assert rows["local-admin"]["status"] == "ok"


async def test_service_port(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    body = {"service_port": {"enabled": True, "interface": "ether5"}}
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    assert "192.168.254.0/29" in r.json()["networks"] and "ether5" in r.json()["interfaces"]
    assert not any(bp["interface"] == "ether5" for bp in rt.tables["/interface/bridge/port"])
    assert any(a["address"] == "192.168.254.1/29" and a["interface"] == "ether5" for a in rt.tables["/ip/address"])
    assert any(d["name"] == "sdwan-local-sp" and d["interface"] == "ether5" for d in rt.tables["/ip/dhcp-server"])
    assert "192.168.254.0/29" in _user(rt)["address"]  # Service-Port-Netz immer in address=
    assert (await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={"service_port": {"enabled": True, "interface": "ether1"}},
                              headers=h)).json()["status"] == "error"  # WAN-Port nie
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access", json={"service_port": {"enabled": False}}, headers=h)
    assert r.json()["status"] == "active"
    assert any(bp["interface"] == "ether5" and bp["bridge"] == "bridge" for bp in rt.tables["/interface/bridge/port"])
    assert not rt.tables["/ip/dhcp-server"] and not any(a["address"] == "192.168.254.1/29" for a in rt.tables["/ip/address"])


async def test_reveal_rotation_error_keeps_password_and_export(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    await poll_all()
    tech = (await client.post("/api/v1/users", json={"email": "t@example.com", "password": "Technik-Pass-123", "role": "technician",
                                                    "tenant_id": t["id"]}, headers=msp))
    assert tech.status_code == 201
    th = {"Authorization": "Bearer " + (await client.post("/api/v1/auth/login", json={"email": "t@example.com", "password": "Technik-Pass-123"})).json()["access_token"]}
    assert (await client.post(f"/api/v1/devices/{dev['id']}/local-access/reveal", json={"reason": "Vor-Ort-Einsatz"}, headers=th)).status_code == 403
    assert (await client.post(f"/api/v1/devices/{dev['id']}/local-access/reveal", json={"reason": ""}, headers=h)).status_code == 422
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access/reveal", json={"reason": "Vor-Ort-Einsatz"}, headers=h)
    old = r.json()["password"]
    assert old == _user(rt)["password"]
    audit = (await client.get("/api/v1/audit", params={"action": "local_access.reveal"}, headers=h)).json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert any(a["action"] == "local_access.reveal" and a["details"]["reason"] == "Vor-Ort-Einsatz" for a in items)
    # Rotation mit Router-Fehler: altes Passwort bleibt gespeichert und gültig
    rt.fail_next.add("/user/set")
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access/rotate", headers=h)
    assert r.status_code == 502 and "bleibt gültig" in r.text
    assert _user(rt)["password"] == old
    assert (await client.post(f"/api/v1/devices/{dev['id']}/local-access/reveal", json={"reason": "Kontrolle"}, headers=h)).json()["password"] == old
    # erfolgreiche Rotation ändert nur diesen Benutzer
    api_user = next(u for u in rt.tables["/user"] if u["group"] == "sdwan-api")
    before = dict(api_user)
    assert (await client.post(f"/api/v1/devices/{dev['id']}/local-access/rotate", headers=h)).status_code == 200
    new = _user(rt)["password"]
    assert new != old and api_user == before
    # „nach Anzeige rotieren“: Fälligkeit gesetzt, Worker rotiert
    s = (await client.get("/api/v1/local-access/settings", headers=h)).json()
    await client.put("/api/v1/local-access/settings", json={**s, "local_access_rotate_after_view": True, "local_access_webhook_url": None}, headers=h)
    r = await client.post(f"/api/v1/devices/{dev['id']}/local-access/reveal", json={"reason": "Einsatz 2"}, headers=h)
    assert r.json()["rotate_due_at"]
    from app.db import system_session, utcnow
    from app.models import LocalAccess

    async with system_session() as db:
        la = (await db.execute(LocalAccess.__table__.select())).first()
        await db.execute(LocalAccess.__table__.update().values(rotate_due_at=utcnow()))
        await db.commit()
    await rotation_tick()
    assert _user(rt)["password"] != new
    # Export: age (nur mit Schlüssel lesbar) und AES-ZIP
    ident = x25519.Identity.generate()
    r = await client.post("/api/v1/local-access/export", json={"format": "age", "recipient": str(ident.to_public())}, headers=h)
    assert r.status_code == 200
    csv_text = decrypt(r.content, [ident]).decode()
    assert csv_text.startswith('"Group","Title","Username","Password","URL","Notes"') and _user(rt)["password"] in csv_text and "la1" in csv_text
    assert (await client.post("/api/v1/local-access/export", json={"format": "zip", "password": "kurz"}, headers=h)).status_code == 422
    r = await client.post("/api/v1/local-access/export", json={"format": "zip", "password": "ein-langes-zip-passwort"}, headers=h)
    with pyzipper.AESZipFile(io.BytesIO(r.content)) as z:
        z.setpassword(b"ein-langes-zip-passwort")
        assert _user(rt)["password"] in z.read("vor-ort-zugang.csv").decode()
    assert la is not None


async def test_offboarding_keeps_local_access_by_default(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    await poll_all()
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "la1"}, headers=h)
    assert r.status_code == 200, r.text
    rt.fire_scheduler("sdwan-offboard")
    u = _user(rt)
    assert u is not None and u["comment"] == "lokaler Zugang (ehemals verwaltet)" and u["group"] == "sdwan-local"
    assert any(g["name"] == "sdwan-local" for g in rt.tables["/user/group"])
    assert _members(rt, LOCAL_ACCESS_LIST) == {"bridge"} and rt._mac_winbox()["allowed-interface-list"] == LOCAL_ACCESS_LIST
    assert not [r for p, rows in rt.tables.items() for r in rows if str(r.get("comment", "")).startswith("sdwan:")]


async def test_offboarding_remove_local_access(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    await poll_all()
    r = await client.post(f"/api/v1/devices/{dev['id']}/offboard", json={"confirm_name": "la1", "keep_local_access": False}, headers=h)
    assert r.status_code == 200, r.text
    rt.fire_scheduler("sdwan-offboard")
    assert _user(rt) is None and rt._mac_winbox()["allowed-interface-list"] == "LAN"
    assert not any(g["name"] == "sdwan-local" for g in rt.tables["/user/group"])


async def test_api_tokens(client, msp, hub):
    t, h, dev, rt = await _device(client, msp)
    await poll_all()
    r = await client.post("/api/v1/auth/api-tokens", json={"name": "Monitoring", "scope": "read", "expires_in_days": 30}, headers=h)
    assert r.status_code == 201
    ro = r.json()
    assert ro["token"].startswith("sdw_") and ro["state"] == "active"
    listed = (await client.get("/api/v1/auth/api-tokens", headers=h)).json()
    assert "token" not in listed[0] and listed[0]["prefix"] == ro["token"][:12]
    rh = {"Authorization": f"Bearer {ro['token']}"}
    assert (await client.get("/api/v1/devices", headers=rh)).status_code == 200
    assert (await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=rh)).status_code == 403  # nur lesend
    listed = (await client.get("/api/v1/auth/api-tokens", headers=h)).json()
    assert listed[0]["last_used_at"] and listed[0]["last_used_ip"]
    rw = (await client.post("/api/v1/auth/api-tokens", json={"name": "Automation", "scope": "role"}, headers=h)).json()
    wh = {"Authorization": f"Bearer {rw['token']}"}
    assert (await client.post(f"/api/v1/devices/{dev['id']}/backups", headers=wh)).status_code in (200, 201, 202)
    audit = (await client.get("/api/v1/audit", params={"action": "api_token.use"}, headers=h)).json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert any(a["action"] == "api_token.use" and a["details"]["name"] == "Automation" for a in items)
    # nie per Token: Vor-Ort-Passwörter, Export, Einstellungen, 2FA, Token-Verwaltung
    for method, path, body in (("post", f"/api/v1/devices/{dev['id']}/local-access/reveal", {"reason": "Automatisch"}),
                               ("post", "/api/v1/local-access/export", {"format": "zip", "password": "ein-langes-zip-passwort"}),
                               ("post", "/api/v1/auth/api-tokens", {"name": "x"}),
                               ("post", "/api/v1/auth/2fa/setup", {}),
                               ("put", "/api/v1/tenants/current/security", {"require_2fa": True})):
        r = await getattr(client, method)(path, json=body, headers=wh)
        assert r.status_code == 403, (path, r.text)
    # Widerruf und Ablauf
    assert (await client.delete(f"/api/v1/auth/api-tokens/{rw['id']}", headers=h)).json()["state"] == "revoked"
    assert (await client.get("/api/v1/devices", headers=wh)).status_code == 401
    admin_view = (await client.get("/api/v1/api-tokens", headers=h)).json()
    assert {x["name"] for x in admin_view} == {"Monitoring", "Automation"}
    from app.db import system_session, utcnow
    from app.models import ApiToken

    async with system_session() as db:
        await db.execute(ApiToken.__table__.update().values(expires_at=utcnow()))
        await db.commit()
    assert (await client.get("/api/v1/devices", headers=rh)).status_code == 401
    # OpenAPI dokumentiert das Bearer-Schema mit API-Token-Hinweis
    spec = (await client.get("/openapi.json")).json()
    assert "sdw_" in spec["components"]["securitySchemes"]["Bearer"]["description"]


async def test_pair_script_full_group_and_later_creation_is_intersection(client, msp, hub):
    from app.models import Device
    from app.routeros.schema import API_POLICIES, LOCAL_POLICIES_FULL, policy_set
    from app.services.onboarding import pair_response_script

    full = ",".join(LOCAL_POLICIES_FULL)
    script = pair_response_script(Device(name="x", tunnel_ip="10.100.0.9"), "A" * 43 + "=", "pw12345678", local_group=True)
    assert f'/user group add name="sdwan-local" policy={full} comment="sdwan:local"' in script
    assert "telnet" not in full and "rest-api" not in full and "api," not in full + ","
    assert "sdwan-local" not in pair_response_script(Device(name="x", tunnel_ip="10.100.0.9"), "A" * 43 + "=", "pw12345678")
    # Onboarding (Simulator bildet das lokal laufende Pair-Script nach): volle Rechte, keine Einschränkung
    t, h, dev, rt = await _device(client, msp)
    await poll_all()
    g = next(x for x in rt.tables["/user/group"] if x["name"] == "sdwan-local")
    assert policy_set(g["policy"]) == set(LOCAL_POLICIES_FULL)
    st = (await client.get(f"/api/v1/devices/{dev['id']}/local-access", headers=h)).json()
    assert st["status"] == "active" and st["restricted"] is False and st["missing_policies"] == []
    # Mandant ohne automatisches Anlegen: Pair-Script ohne Gruppe; nachträglich per Button = Schnittmenge
    s = (await client.get("/api/v1/local-access/settings", headers=h)).json()
    await client.put("/api/v1/local-access/settings", json={**s, "local_access_auto": False, "local_access_webhook_url": None}, headers=h)
    dev2 = await make_paired_device(client, h, name="la-spaet")
    rt2 = get_router(dev2["tunnel_ip"])
    _factory(rt2)
    assert not any(x["name"] == "sdwan-local" for x in rt2.tables["/user/group"])
    await poll_all()
    assert (await client.get(f"/api/v1/devices/{dev2['id']}/local-access", headers=h)).json() is None
    r = (await client.post(f"/api/v1/devices/{dev2['id']}/local-access", json={}, headers=h)).json()
    g2 = next(x for x in rt2.tables["/user/group"] if x["name"] == "sdwan-local")
    assert policy_set(g2["policy"]) == set(LOCAL_POLICIES_FULL) & set(API_POLICIES)
    assert r["status"] == "active" and r["restricted"] is True
    assert set(r["missing_policies"]) == set(LOCAL_POLICIES_FULL) - set(API_POLICIES) and "local" in r["missing_policies"]
    assert r["full_group_command"] and f"policy={full}" in r["full_group_command"]
    base = next(x for x in (await client.get("/api/v1/compliance/rule-sets", headers=h)).json() if x["name"] == "MSP-Baseline")
    await client.post(f"/api/v1/compliance/rule-sets/{base['id']}/assign", json={"device_ids": [dev2["id"]]}, headers=h)
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev2["id"]]}, headers=h)
    rep = (await client.get(f"/api/v1/devices/{dev2['id']}/compliance", headers=h)).json()
    rows = {x["rule_id"]: x for res in rep for x in res["results"]}
    assert rows["local-admin"]["status"] == "warn" and "local" in rows["local-admin"]["detail"]
    assert rep[0]["failed"] == sum(1 for x in rep[0]["results"] if x["status"] == "fail")  # Warnung zählt nicht als Fehler
    # Terminal-Einzeiler (als Admin) nachgerüstet → erneuter Abgleich: nicht mehr eingeschränkt, Gruppe unverändert
    g2["policy"] = full
    r = (await client.post(f"/api/v1/devices/{dev2['id']}/local-access", json={}, headers=h)).json()
    assert r["restricted"] is False and g2["policy"] == full
    await client.post("/api/v1/compliance/evaluate", json={"device_ids": [dev2["id"]]}, headers=h)
    rows = {x["rule_id"]: x for res in (await client.get(f"/api/v1/devices/{dev2['id']}/compliance", headers=h)).json() for x in res["results"]}
    assert rows["local-admin"]["status"] == "ok"
