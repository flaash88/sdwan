"""Audit-Behebung AP3 (SSRF und Web-Härtung) – zusätzliche Tests (docs/AUDIT-2026-09.md, docs/PLAN-AUDIT-FIX.md)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import pathlib
import re
import shutil
import subprocess

import httpx
import pytest

from app import net_guard
from tests.conftest import make_tenant, make_tenant_admin

ROOT = pathlib.Path(__file__).resolve().parents[3]


# ----------------------------------------------------------------------------- AUDIT-004/012: net_guard
@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://10.100.0.2/", "http://172.30.0.20:8000/",
                                 "http://192.168.1.1/", "https://100.64.0.1/", "http://169.254.169.254/", "http://api:8000/",
                                 "http://influxdb:8086/", "https://x.internal/", "https://printer.local/", "ftp://example.com/",
                                 "https://user:pw@example.com/", "http://[::ffff:10.0.0.1]/", "http://0.0.0.0/"])
def test_004_check_url_rejects_internal(url):
    with pytest.raises(net_guard.GuardError):
        net_guard.check_url(url)


def test_004_check_url_accepts_public():
    assert net_guard.check_url("https://www.spamhaus.org/drop/drop.txt") == ("https", "www.spamhaus.org", 443)
    assert net_guard.check_url("http://1.1.1.1:8080/x")[2] == 8080


async def test_004_resolve_rejects_names_pointing_inside(monkeypatch):
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, **_kw):
        ips = {"evil.example.com": ["93.184.216.34", "10.0.0.5"], "good.example.com": ["93.184.216.34"]}[host]
        return [(0, 0, 0, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    assert await net_guard.resolve_public("good.example.com", 443) == "93.184.216.34"
    with pytest.raises(net_guard.GuardError):
        await net_guard.resolve_public("evil.example.com", 443)  # EINE interne Adresse genügt zum Ablehnen


def test_012_pinned_request_keeps_host_and_sni():
    target, headers, ext = net_guard._pinned("https://hooks.example.com:8443/a?b=1", "93.184.216.34")
    assert target == "https://93.184.216.34:8443/a?b=1" and headers == {"Host": "hooks.example.com:8443"}
    assert ext == {"sni_hostname": "hooks.example.com"}
    target6, _h, ext6 = net_guard._pinned("http://example.com/", "2606:2800:220:1::1")
    assert target6 == "http://[2606:2800:220:1::1]/" and ext6 == {}


async def test_004_redirect_to_internal_blocked_and_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://10.100.0.1/secret"})
        if request.url.path.startswith("/loop"):
            return httpx.Response(302, headers={"location": f"/loop{len(request.url.path)}"})
        return httpx.Response(200, text="ok")

    tr = httpx.MockTransport(handler)
    with pytest.raises(net_guard.GuardError):
        async with net_guard.stream("GET", "https://feeds.example.com/start", transport=tr) as r:
            await r.aread()
    with pytest.raises(net_guard.GuardError):
        async with net_guard.stream("GET", "https://feeds.example.com/loop", transport=tr, max_redirects=3) as r:
            await r.aread()
    async with net_guard.stream("GET", "https://feeds.example.com/ok", transport=tr) as r:
        assert (await r.aread()) == b"ok"


def test_012_webhook_validation():
    from app.services.webhook import WebhookError, _bad_ip, validate_url

    for bad in ("https://100.64.0.1/x", "https://hooks/x", "https://192.0.2.1/x", "http://hooks.example.com/"):
        with pytest.raises(WebhookError):
            validate_url(bad)
    assert validate_url("https://outlook.office.com/webhook/abc") == "https://outlook.office.com/webhook/abc"
    assert _bad_ip(ipaddress.ip_address("198.51.100.1")) and not _bad_ip(ipaddress.ip_address("8.8.8.8"))


async def test_012_webhook_redirect_not_followed():
    from app.services import webhook

    tr = httpx.MockTransport(lambda req: httpx.Response(307, headers={"location": "http://127.0.0.1/"}))
    old = webhook.transport
    webhook.transport = tr
    try:
        assert await webhook.send("https://hooks.example.com/x", {"a": 1}) is False
    finally:
        webhook.transport = old


async def test_004_feed_update_to_internal_rejected(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    r = await client.post("/api/v1/feeds", json={"name": "Extern", "url": "https://feeds.example.com/list.txt"}, headers=h)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    r = await client.patch(f"/api/v1/feeds/{fid}", json={"name": "Extern", "url": "http://10.100.0.1/"}, headers=h)
    assert r.status_code == 422


async def test_004_feed_download_uses_guard(monkeypatch):
    from app.services import feeds

    async def bad(*_a, **_k):
        raise net_guard.GuardError("feeds.example.com löst auf eine interne Adresse auf")

    monkeypatch.setattr(net_guard, "resolve_public", bad)
    with pytest.raises(feeds.FeedError):
        await feeds._download("https://feeds.example.com/list.txt")


# ----------------------------------------------------------------------------- AUDIT-010: Header + CSP-Hash
def _csp() -> str:
    conf = (ROOT / "frontend" / "nginx.conf").read_text()
    return re.search(r'add_header Content-Security-Policy "([^"]+)"', conf).group(1)


def test_010_csp_hash_matches_inline_theme_script():
    html = (ROOT / "frontend" / "index.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert f"'sha256-{digest}'" in _csp(), "Inline-Skript in index.html geändert – Hash in frontend/nginx.conf aktualisieren"
    csp = _csp()
    for part in ("frame-ancestors 'self'", "object-src 'none'", "base-uri 'self'"):
        assert part in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx nicht installiert")
def test_010_nginx_config_valid(tmp_path):
    conf = (ROOT / "frontend" / "nginx.conf").read_text()
    conf = conf.replace("http://api:8000", "http://127.0.0.1:8000").replace("http://grafana:3000", "http://127.0.0.1:3000")
    (tmp_path / "site.conf").write_text(conf)
    (tmp_path / "nginx.conf").write_text(f"pid {tmp_path}/n.pid;\nerror_log {tmp_path}/e.log;\nevents {{}}\n"
                                         f"http {{ access_log off; include {tmp_path}/site.conf; }}\n")
    r = subprocess.run(["nginx", "-t", "-c", str(tmp_path / "nginx.conf")], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr


# ----------------------------------------------------------------------------- AUDIT-011/052: WebSocket-Ticket
class FakeWS:
    def __init__(self) -> None:
        self.closed: int | None = None
        self.accepted = False
        self.sent: list[dict] = []
        self._stop = asyncio.Event()

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        self._stop.set()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        from fastapi import WebSocketDisconnect

        await self._stop.wait()
        raise WebSocketDisconnect(self.closed or 1000)


async def test_011_ticket_single_use_and_expiry(client, msp):
    from app import ws_tickets

    r = await client.post("/api/v1/auth/ws-ticket", headers=msp)
    assert r.status_code == 200 and r.json()["expires_in"] == 30
    ticket = r.json()["ticket"]
    assert await ws_tickets.redeem(ticket) is not None
    assert await ws_tickets.redeem(ticket) is None  # einmalig
    assert (await client.post("/api/v1/auth/ws-ticket")).status_code == 401


async def test_011_ws_with_ticket_and_recheck_closes_after_password_change(client, msp, monkeypatch):
    from app.api.v1 import ws as ws_mod

    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    ticket = (await client.post("/api/v1/auth/ws-ticket", headers=tech)).json()["ticket"]
    monkeypatch.setattr(ws_mod, "RECHECK_S", 0.05)
    fake = FakeWS()
    task = asyncio.create_task(ws_mod.live(fake, ticket=ticket))
    await asyncio.sleep(0.1)
    assert fake.accepted and fake.sent[0]["tenant_id"] == t["id"] and fake.closed is None
    me = (await client.get("/api/v1/auth/me", headers=tech)).json()
    uid = me.get("id") or me["user"]["id"]
    assert (await client.patch(f"/api/v1/users/{uid}", json={"password": "neues-passwort-1"}, headers=h)).status_code == 200
    await asyncio.wait_for(task, 5)
    assert fake.closed == 4401
    # derselbe Ticket-Wert ist verbraucht
    fake2 = FakeWS()
    await ws_mod.live(fake2, ticket=ticket)
    assert fake2.closed == 4401 and not fake2.accepted


def test_052_frontend_ws_without_jwt_in_url():
    src = (ROOT / "frontend" / "src" / "lib" / "api.ts").read_text()
    fn = src[src.index("export async function wsUrl"):]
    fn = fn[:fn.index("\n}\n")]
    assert "/auth/ws-ticket" in fn and "session.token" not in fn and "token:" not in fn


# ----------------------------------------------------------------------------- AUDIT-025: /meta
async def test_025_public_meta_minimal_full_meta_needs_login(client, msp):
    pub = (await client.get("/api/v1/meta")).json()
    assert set(pub) == {"version", "product_name", "product_short", "onboarding_min_routeros"}
    assert (await client.get("/api/v1/meta/full")).status_code == 401
    full = (await client.get("/api/v1/meta/full", headers=msp)).json()
    assert {"management_network", "hub_endpoint", "simulator", "grafana_url", "smtp_configured"} <= set(full)


# ----------------------------------------------------------------------------- AUDIT-037/040: Abhängigkeiten
def test_040_pyzipper_fixed_version_and_requirements_pinned():
    import pyzipper

    ver = tuple(int(x) for x in pyzipper.__version__.split(".")[:2])
    assert ver >= (0, 4)
    req = (ROOT / "backend" / "requirements.txt").read_text()
    assert "starlette>=1.3.1" in req and "pyzipper==0.4.*" in req
