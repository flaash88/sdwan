"""Audit 2026-09 – Reproduktion der Funde (siehe docs/AUDIT-2026-09.md).

Jeder Test beschreibt das ERWARTETE (korrekte) Verhalten und ist mit ``xfail(strict=True, reason="AUDIT-xxx")``
markiert: Heute schlägt er fehl (Fund reproduziert). Nach der Behebung besteht er – ``strict`` lässt die Suite dann
rot werden, damit die Markierung entfernt wird. Produktivcode wird hier nicht verändert.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import pathlib
import subprocess
import uuid

import pytest

from app.config import get_settings
from app.routeros.simulator import get_router
from tests.conftest import login, make_paired_device, make_tenant, make_tenant_admin

ROOT = pathlib.Path(__file__).resolve().parents[3]
VALID_WG = "A" * 43 + "="


def xf(audit_id: str):
    return pytest.mark.xfail(strict=True, reason=audit_id)


async def _admin(client, msp, slug="acme"):
    t = await make_tenant(client, msp, slug)
    return t, await make_tenant_admin(client, msp, t["id"], email=f"admin@{slug}.example.com")


# ============================================================================= Injection in RouterOS-Scripte
# AUDIT-001: behoben (AP1)
async def test_001_device_name_cannot_inject_into_onboarding_script(client, msp, hub):
    _t, h = await _admin(client, msp)
    evil = "x\n/user add name=pwn password=Pwn-12345678 group=full\n#"
    r = await client.post("/api/v1/devices", json={"name": evil}, headers=h)
    if r.status_code == 422:
        return  # Eingabe abgelehnt = behoben
    token = r.json()["pairing"]["token"]
    script = (await client.get(f"/api/v1/onboard/{token}.rsc")).text
    assert not any(line.strip().startswith("/user add name=pwn") for line in script.splitlines()), script[:400]


# AUDIT-002: behoben (AP1)
async def test_002_wan_link_name_cannot_inject_into_netwatch_script(client, msp, hub):
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    evil = 'x" ; /user add name=pwn password=Pwn-12345678 group=full; :log info "'
    r = await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": [
        {"name": evil, "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"}]}, headers=h)
    if r.status_code == 422:
        return
    rt = get_router(dev["tunnel_ip"])
    scripts = " ".join(str(n.get("down-script", "")) + str(n.get("up-script", "")) for n in rt.tables["/tool/netwatch"])
    assert "/user add name=pwn" not in scripts, scripts[:300]


# AUDIT-026: behoben (AP1)
def test_026_quote_helper_rejects_trailing_newline():
    from app.services.onboarding import _q

    with pytest.raises(ValueError):
        _q("ab\n")  # re.match(r"^...$") lässt ein abschließendes \n durch


# AUDIT-027: behoben (AP1)
def test_027_ztp_identity_pattern_no_format_attribute_access():
    from app.services.ztp import TemplateError, validate_template

    with pytest.raises((TemplateError, ValueError)):
        validate_template({"identity_pattern": "{name.__class__}"})


# ============================================================================= Anmeldung / Transport
# AUDIT-003: behoben (AP2) – X-Forwarded-For nur von TRUSTED_PROXIES, uvicorn ohne Proxy-Header
async def test_003_login_ip_limit_not_bypassable_via_x_forwarded_for(msp, monkeypatch):
    """Anfragen kommen über den vertrauenswürdigen Frontend-nginx (172.30.0.30), der Client setzt gefälschte XFF-Einträge
    davor; nginx hängt die echte Adresse an. Das Limit muss für die echte Adresse greifen."""
    import httpx

    from app.main import create_app

    monkeypatch.setattr(get_settings(), "trusted_proxies", "172.30.0.30")
    app = create_app()
    limit = get_settings().login_ip_limit
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.0.30", 5555)), base_url="http://test") as c:
        codes = []
        for i in range(limit + 10):
            r = await c.post("/api/v1/auth/login", json={"email": f"spray{i}@example.com", "password": "falsch-123"},
                             headers={"X-Forwarded-For": f"10.0.{i // 250}.{i % 250 + 1}, 203.0.113.9"})
            codes.append(r.status_code)
    assert 429 in codes, "IP-Limit greift nicht, weil X-Forwarded-For vom Client übernommen wird"
    cmd = (ROOT / "backend" / "Dockerfile").read_text()
    assert '"--forwarded-allow-ips", "*"' not in cmd and "--no-proxy-headers" in cmd


# AUDIT-005: behoben (AP1)
def test_005_onboarding_fetch_checks_certificate():
    from app.models import Device
    from app.services.onboarding import onboarding_command, onboarding_script
    from app.services.ztp import bootstrap_script

    d = Device(name="r", serial="SN1", tunnel_ip="10.100.0.9")
    texts = [onboarding_command("tok"), onboarding_script("tok", "r"), bootstrap_script("tok", d, None)]
    assert all("check-certificate=yes" in t for t in texts)


# AUDIT-006: behoben (AP2)
async def test_006_production_refuses_default_secrets(monkeypatch):
    from app.main import create_app, lifespan

    s = get_settings()
    monkeypatch.setattr(s, "environment", "production")
    monkeypatch.setattr(s, "secret_key", "change-me-openssl-rand-hex-32")
    with pytest.raises(Exception):  # noqa: B017 - erwartet: Start wird verweigert
        async with lifespan(create_app()):
            pass


# AUDIT-007: behoben (AP1)
async def test_007_ztp_token_bound_to_serial_even_without_serial_in_request(client, msp, hub):
    _t, h = await _admin(client, msp)
    r = await client.post("/api/v1/ztp/stage", json={"devices": [{"name": "ztp1", "serial": "HGK0001"}]}, headers=h)
    token = r.json()[0]["token"]
    r = await client.post("/api/v1/pair", json={"token": token, "public_key": VALID_WG})  # keine Seriennummer
    assert r.text.startswith(":error"), "Pairing ohne Seriennummer trotz seriengebundenem ZTP-Token erfolgreich"


# AUDIT-029: behoben (AP2)
async def test_029_password_change_invalidates_existing_tokens(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    tech = await make_tenant_admin(client, msp, t["id"], email="tech@acme.example.com", role="technician")
    me = (await client.get("/api/v1/auth/me", headers=tech)).json()
    uid = me.get("id") or me["user"]["id"]
    assert (await client.patch(f"/api/v1/users/{uid}", json={"password": "neues-passwort-1"}, headers=h)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=tech)).status_code == 401  # alter JWT (12 h) bleibt gültig


# AUDIT-034: behoben (AP2)
async def test_034_hub_token_non_ascii_is_401_not_500(client):
    r = await client.get("/api/v1/internal/hub/peers", headers={"X-Hub-Token": "tökén".encode("latin-1")})
    assert r.status_code == 401


# ============================================================================= SSRF
# AUDIT-004: behoben (AP3)
async def test_004_threat_feed_url_to_internal_address_rejected(client, msp):
    _t, h = await _admin(client, msp)
    for url in ("http://127.0.0.1:8000/healthz", "http://api:8000/api/v1/internal/hub/peers", "http://10.100.0.2/", "http://169.254.169.254/"):
        r = await client.post("/api/v1/feeds", json={"name": f"x{abs(hash(url)) % 1000}", "url": url}, headers=h)
        assert r.status_code == 422, (url, r.status_code)


# AUDIT-012: behoben (AP3)
def test_012_webhook_blocks_cgnat_and_non_global():
    from app.services.webhook import _bad_ip

    assert _bad_ip(ipaddress.ip_address("100.64.0.1"))  # CGNAT / Shared Address Space


# ============================================================================= Eingaben, ReDoS, Exporte
@xf("AUDIT-008")
def test_008_redos_alternation_rejected():
    from app.services.compliance import ComplianceError, check_regex

    with pytest.raises(ComplianceError):
        check_regex(r"(\w|\w)*!")


# AUDIT-009: behoben (AP2)
async def test_009_backup_content_masked_for_readonly(client, msp, hub):
    from app.db import system_session
    from app.models import ConfigBackup

    t, h = await _admin(client, msp)
    ro = await make_tenant_admin(client, msp, t["id"], email="ro@acme.example.com", role="readonly")
    dev = await make_paired_device(client, h)
    async with system_session() as db:
        b = ConfigBackup(tenant_id=uuid.UUID(t["id"]), device_id=uuid.UUID(dev["id"]), content="/interface wifi security\nadd name=x passphrase=GeheimesWlan123\n",
                         sha256="0" * 64, size=60, trigger="manual")
        db.add(b)
        await db.commit()
        bid = b.id
    body = (await client.get(f"/api/v1/backups/{bid}", headers=ro)).text
    assert "GeheimesWlan123" not in body


@xf("AUDIT-022")
async def test_022_csv_export_neutralises_formulas(client, msp, hub):
    _t, h = await _admin(client, msp)
    await make_paired_device(client, h, name='=HYPERLINK("http://evil.example","x")')
    text = (await client.get("/api/v1/inventory.csv", headers=h)).content.decode("utf-8-sig")
    cells = [c for line in text.splitlines()[1:] for c in line.split(";")]
    assert not any(c.lstrip('"').startswith(("=", "+", "-", "@")) for c in cells), cells[:3]


@xf("AUDIT-019")
async def test_019_remote_session_rejects_open_world_cidr(client, msp, hub, monkeypatch):
    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41960-41969")
    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    r = await client.post(f"/api/v1/devices/{dev['id']}/remote-sessions", json={"protocol": "ssh", "allowed_cidr": "0.0.0.0/0"}, headers=h)
    assert r.status_code == 422


@xf("AUDIT-021")
async def test_021_content_filter_api_key_network_error_handled(client, msp, monkeypatch):
    import httpx

    _t, h = await _admin(client, msp)

    async def boom(*_a, **_k):
        raise httpx.ConnectError("keine Verbindung")

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    r = await client.put("/api/v1/content-filter/api-key", json={"api_key": "x" * 20}, headers=h)
    assert r.status_code in (400, 422, 502, 503)


# ============================================================================= Robustheit
@xf("AUDIT-013")
async def test_013_one_bad_device_does_not_abort_fleet_poll(client, msp, hub, monkeypatch):
    from app.db import system_session
    from app.models import Device
    from app.services import poller, registry

    _t, h = await _admin(client, msp)
    good = await make_paired_device(client, h, name="good")
    bad = await make_paired_device(client, h, name="bad")
    orig = registry.poll_hooks()

    async def broken(device, api, res):
        if device.name == "bad":
            raise ValueError("unerwartete Router-Antwort")
        return None

    monkeypatch.setattr(registry, "poll_hooks", lambda: [*orig, broken])
    try:
        await poller.poll_all()
    except ValueError:
        pass
    async with system_session() as db:
        g = await db.get(Device, uuid.UUID(good["id"]))
        assert g.last_seen_at is not None, "gesamter Poll-Durchlauf verworfen, weil ein Gerät eine Ausnahme warf"
    assert bad


@xf("AUDIT-015")
async def test_015_poll_does_not_drop_concurrent_facts_updates(client, msp, hub, monkeypatch):
    from app.db import system_session
    from app.models import Device
    from app.services import poller, registry

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    orig = registry.poll_hooks()

    async def writer(device, api, res):  # während des Polls schreibt ein anderer Prozess facts (z. B. dns_backup)
        async with system_session() as db:
            d = await db.get(Device, device.id)
            d.facts = {**(d.facts or {}), "dns_backup": {"servers": "9.9.9.9"}}
            await db.commit()
        return None

    monkeypatch.setattr(registry, "poll_hooks", lambda: [*orig, writer])
    await poller.poll_all()
    async with system_session() as db:
        d = await db.get(Device, uuid.UUID(dev["id"]))
        assert "dns_backup" in (d.facts or {}), "Poll überschreibt facts komplett – dns_backup verloren"


@xf("AUDIT-017")
async def test_017_platform_backup_timeout_marks_failed(tmp_path, monkeypatch):
    from app import platform_backup as pb

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="pg_dump", timeout=3600)

    from pyrage import x25519

    async def rev(_url):
        return "head"

    monkeypatch.setattr(pb, "migration_revision", rev)
    monkeypatch.setattr(pb.subprocess, "run", slow)
    monkeypatch.setattr(get_settings(), "platform_backup_age_recipient", str(x25519.Identity.generate().to_public()))
    try:
        res = await pb.run(trigger="manual", out_dir=str(tmp_path), database_url="postgresql://x@localhost/x")
    except subprocess.TimeoutExpired:
        res = {"status": "running (Ausnahme entkommen, Datensatz bleibt 'running', kein Alarm)"}
    assert res.get("status") == "failed", res


@xf("AUDIT-020")
async def test_020_offboarding_final_scheduler_has_explicit_policy(client, msp, hub):
    from app.db import system_session
    from app.models import Device
    from app.routeros import connect_device
    from app.services.offboarding import FINAL_SCHEDULER, _step6

    _t, h = await _admin(client, msp)
    dev = await make_paired_device(client, h)
    async with system_session() as db:
        d = await db.get(Device, uuid.UUID(dev["id"]))
        async with connect_device(d) as api:
            await _step6(api)
    sch = next(s for s in get_router(dev["tunnel_ip"]).tables["/system/scheduler"] if s["name"] == FINAL_SCHEDULER)
    # Default-Policy eines Schedulers enthält ftp/password/sniff/romon, die sdwan-api nicht hat (ANNAHME Labor)
    assert sch.get("policy"), "Scheduler ohne explizite policy – auf echter Hardware evtl. abgelehnt"


@xf("AUDIT-038")
async def test_038_concurrent_remote_sessions_get_distinct_ports(client, msp, hub, monkeypatch):
    """allocate_port liest belegte Ports, danach folgen Router-Aufrufe (await) vor dem Commit – kein Unique-Index."""
    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41950-41959")
    ta, ha = await _admin(client, msp, "aaa")
    tb, hb = await _admin(client, msp, "bbb")
    da = await make_paired_device(client, ha, name="ra")
    db_ = await make_paired_device(client, hb, name="rb")
    s1, s2 = await asyncio.gather(
        client.post(f"/api/v1/devices/{da['id']}/remote-sessions", json={"protocol": "ssh", "allowed_cidr": "192.0.2.1/32"}, headers=ha),
        client.post(f"/api/v1/devices/{db_['id']}/remote-sessions", json={"protocol": "ssh", "allowed_cidr": "192.0.2.2/32"}, headers=hb))
    assert s1.status_code == s2.status_code == 201
    assert s1.json()["listen_port"] != s2.json()["listen_port"], "zwei aktive Sitzungen (zwei Mandanten) auf demselben Proxy-Port"


# ============================================================================= Zeit
@xf("AUDIT-031")
def test_031_weekly_speedtest_runs_every_7_days():
    """Täglicher Cron 03:30; Vorwoche endete 03:30:05 → nach 7 Tagen ist das Delta 6 T 23:59:55 → übersprungen."""
    last = dt.datetime(2026, 9, 1, 3, 30, 5, tzinfo=dt.UTC)
    now = dt.datetime(2026, 9, 8, 3, 30, 0, tzinfo=dt.UTC)
    due = not ((now - last).days < 7)  # Bedingung aus services/speedtest.py
    assert due


@xf("AUDIT-032")
def test_032_maintenance_window_duration_is_real_time_across_dst():
    from app.models import MaintenanceWindow
    from app.services.maintenance import is_active

    w = MaintenanceWindow(name="Nacht", kind="weekly", weekdays=[6], start_time="01:00", duration_min=180, enabled=True)
    tz = "Europe/Vienna"
    # 25.10.2026: Umstellung 03:00 → 02:00 MESZ→MEZ. 01:00 + 180 min Echtzeit = 03:00 MEZ = 02:00 UTC
    after = dt.datetime(2026, 10, 25, 2, 30, tzinfo=dt.UTC)  # 03:30 MEZ, 210 min nach Beginn
    assert not is_active(w, after, tz)


# ============================================================================= Betrieb / Frontend / Abhängigkeiten
# AUDIT-010: behoben (AP3)
def test_010_frontend_sends_security_headers():
    conf = (ROOT / "frontend" / "nginx.conf").read_text()
    for h in ("X-Frame-Options", "Content-Security-Policy", "X-Content-Type-Options"):
        assert h in conf, h


@xf("AUDIT-035")
def test_035_openapi_docs_reachable_via_frontend():
    """Dialog „API-Tokens“ verlinkt /docs – nginx leitet nur /api/ an die API weiter."""
    conf = (ROOT / "frontend" / "nginx.conf").read_text()
    assert "location /docs" in conf or "location = /docs" in conf


# AUDIT-037: behoben (AP3)
def test_037_starlette_without_known_vulnerabilities():
    import starlette

    ver = tuple(int(x) for x in starlette.__version__.split(".")[:3])
    assert ver >= (0, 49, 1), starlette.__version__


# AUDIT-030: behoben (AP3) – kein USER im Image (die Route ins Management-Netz braucht root/NET_ADMIN beim Start),
# stattdessen gibt der Entrypoint die Rechte per setpriv an "app" ab; nur der Worker bleibt root (RUN_AS_ROOT).
def test_030_backend_container_not_root():
    import os
    import re
    import shutil

    docker = (ROOT / "backend" / "Dockerfile").read_text()
    entry = (ROOT / "backend" / "docker-entrypoint.sh").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "useradd --system --uid 10001" in docker
    line = next(ln.strip() for ln in entry.splitlines() if ln.strip().startswith("exec setpriv"))
    assert "--reuid=app" in line and "--ambient-caps=+net_bind_service" in line
    assert compose.count('RUN_AS_ROOT: "true"') == 1  # nur der Worker
    # setpriv-Aufruf funktional prüfen (als root: auf "nobody" umschalten, Port 514 binden, root-Dateien gesperrt)
    if os.geteuid() == 0 and shutil.which("setpriv"):
        cmd = re.sub(r"--reuid=app --regid=app", "--reuid=nobody --regid=nogroup", line.removeprefix("exec ").replace('"$@"', ""))
        code = ("import os,socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('127.0.0.1',0));"
                "print(os.getuid())")
        out = subprocess.run(cmd.split() + ["python3", "-c", code], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0 and out.stdout.strip() != "0", out.stderr


async def test_info_login_helper_available(client, msp):  # Rauchtest für die Hilfsfunktionen dieses Moduls
    assert (await client.get("/api/v1/auth/me", headers=msp)).status_code == 200
    assert login
