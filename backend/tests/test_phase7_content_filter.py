from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.routeros.simulator import get_router
from app.services import nextdns
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


class FakeNextDNS:
    def __init__(self) -> None:
        self.profiles: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["X-Api-Key"] == "test-key-123456"
        self.calls.append((req.method, req.url.path))
        if self.fail:
            return httpx.Response(500, text="boom")
        body = json.loads(req.content) if req.content else None
        parts = req.url.path.strip("/").split("/")
        if req.method == "POST" and parts == ["profiles"]:
            pid = f"p{len(self.profiles) + 1:05d}"
            self.profiles[pid] = {"name": body["name"]}
            return httpx.Response(200, json={"data": {"id": pid}})
        if req.method == "GET" and parts == ["profiles"]:
            return httpx.Response(200, json={"data": []})
        pid = parts[1]
        if pid not in self.profiles:
            return httpx.Response(404, json={"errors": [{"code": "notFound"}]})
        if req.method == "DELETE" and len(parts) == 2:
            del self.profiles[pid]
            return httpx.Response(204)
        self.profiles[pid]["/".join(parts[2:]) or "root"] = body
        return httpx.Response(204)


@pytest.fixture
def fake_nextdns(monkeypatch):
    fake = FakeNextDNS()
    monkeypatch.setattr(nextdns, "transport_override", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(get_settings(), "nextdns_api_key", "test-key-123456")
    return fake


PROFILE = {"name": "Schule", "categories": ["porn", "gambling"], "services": ["tiktok"], "denylist": ["example.org"],
           "safe_search": True, "force_dns": True}


async def test_profile_sync_and_apply(client, msp, hub, fake_nextdns):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    site = (await client.post("/api/v1/sites", json={"name": "Campus", "lan_subnets": ["10.10.0.0/16"]}, headers=h)).json()
    dev = await make_paired_device(client, h, site_id=site["id"], name="campus rtr")
    get_router(dev["tunnel_ip"]).tables["/ip/dhcp-client"].append({".id": "*D1", "interface": "ether1", "use-peer-dns": "yes", "gateway": "172.16.235.1"})
    r = await client.post("/api/v1/content-filter/profiles", json=PROFILE, headers=h)
    assert r.status_code == 201, r.text
    prof = r.json()
    assert prof["sync_status"] == "synced" and prof["nextdns_profile_id"] == "p00001"
    stored = fake_nextdns.profiles["p00001"]
    assert stored["name"] == "acme-Schule"
    assert stored["parentalControl/categories"] == [{"id": "porn", "active": True}, {"id": "gambling", "active": True}]
    assert stored["parentalControl"]["safeSearch"] is True
    assert stored["denylist"] == [{"id": "example.org", "active": True}]
    assert stored["security"]["threatIntelligenceFeeds"] is True

    # Standort-Zuweisung -> DoH auf dem Router
    r = await client.put("/api/v1/content-filter/assignment", json={"sites": {site["id"]: prof["id"]}}, headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()["applied"][dev["id"]]
    assert rep["ok"] and rep["doh"] == "https://dns.nextdns.io/p00001/campus-rtr"
    rt = get_router(dev["tunnel_ip"])
    assert rt.dns["use-doh-server"] == "https://dns.nextdns.io/p00001/campus-rtr"
    assert rt.dns["verify-doh-cert"] == "yes" and rt.dns["servers"] == ""
    # Provider-DNS vom DHCP-Client abgeschaltet (sonst Umgehung des Filters)
    assert rt.tables["/ip/dhcp-client"][0]["use-peer-dns"] == "no"
    assert {s["address"] for s in rt.tables["/ip/dns/static"] if s.get("name") == "dns.nextdns.io"} == {"45.90.28.0", "45.90.30.0"}
    nat = [n for n in rt.tables["/ip/firewall/nat"] if str(n.get("comment", "")).startswith("sdwan:dns:force")]
    assert len(nat) == 2 and nat[0]["src-address"] == "10.10.0.0/16"

    # Update ändert Kategorien, force_dns aus -> Redirect verschwindet
    r = await client.put(f"/api/v1/content-filter/profiles/{prof['id']}", json={**PROFILE, "categories": ["dating"], "force_dns": False}, headers=h)
    assert fake_nextdns.profiles["p00001"]["parentalControl/categories"] == [{"id": "dating", "active": True}]
    assert not [n for n in rt.tables["/ip/firewall/nat"] if str(n.get("comment", "")).startswith("sdwan:dns:")]

    # Zuweisung entfernen -> DNS wiederhergestellt
    r = await client.put("/api/v1/content-filter/assignment", json={"sites": {site["id"]: None}}, headers=h)
    assert rt.dns["use-doh-server"] == ""
    assert rt.tables["/ip/dhcp-client"][0]["use-peer-dns"] == "yes"  # wiederhergestellt
    assert not [s for s in rt.tables["/ip/dns/static"] if str(s.get("comment", "")).startswith("sdwan:dns:")]
    # Löschen entfernt Profil bei NextDNS
    assert (await client.delete(f"/api/v1/content-filter/profiles/{prof['id']}", headers=h)).status_code == 204
    assert "p00001" not in fake_nextdns.profiles


async def test_tenant_default_and_errors(client, msp, hub, fake_nextdns):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    fake_nextdns.fail = True
    prof = (await client.post("/api/v1/content-filter/profiles", json={"name": "Office"}, headers=h)).json()
    assert prof["sync_status"] == "error" and "HTTP 500" in prof["last_error"]
    fake_nextdns.fail = False
    prof = (await client.post(f"/api/v1/content-filter/profiles/{prof['id']}/sync", headers=h)).json()
    assert prof["sync_status"] == "synced"
    await client.put("/api/v1/content-filter/assignment", json={"default_profile_id": prof["id"]}, headers=h)
    assert get_router(dev["tunnel_ip"]).dns["use-doh-server"].startswith("https://dns.nextdns.io/p00001/")
    # zugewiesen -> nicht löschbar
    assert (await client.delete(f"/api/v1/content-filter/profiles/{prof['id']}", headers=h)).status_code == 409
    # Validierung
    r = await client.post("/api/v1/content-filter/profiles", json={"name": "x", "categories": ["nope"]}, headers=h)
    assert r.status_code == 422
    r = await client.post("/api/v1/content-filter/profiles", json={"name": "x", "denylist": ["not a domain"]}, headers=h)
    assert r.status_code == 422
    # Fremdes Profil nicht zuweisbar
    t2 = await make_tenant(client, msp, "other")
    h2 = await make_tenant_admin(client, msp, t2["id"], email="x@other.example.com")
    r = await client.put("/api/v1/content-filter/assignment", json={"default_profile_id": prof["id"]}, headers=h2)
    assert r.status_code == 404
