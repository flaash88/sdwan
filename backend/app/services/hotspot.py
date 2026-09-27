"""Hotspot / Gäste-Portal (Phase 20).

Der Hotspot läuft auf dem MikroTik (``/ip hotspot``) auf einem Interface oder VLAN – unabhängig davon, welche
Access-Points (auch anderer Hersteller) dahinter hängen.

Verwaltete Objekte je Hotspot ``sdwan-hs-<kürzel>``:
* ``/ip/hotspot/profile`` und ``/ip/hotspot`` mit diesem Namen, ``/ip/hotspot/user/profile`` ``sdwan-hs-<kürzel>-…``
  (Erkennung über das Namenspräfix ``sdwan-hs-``);
* ``/ip/hotspot/walled-garden``, ``/ip/hotspot/walled-garden/ip``, ``/ip/hotspot/user`` (Voucher) und
  ``/ip/hotspot/ip-binding`` (gesperrte Geräte) mit Kommentar ``sdwan:hs:<kürzel>…``;
* Login-Seiten im Verzeichnis ``sdwan-hs-<kürzel>/`` (Upload per SFTP).

Anmeldearten: Voucher (Benutzer = Code, leeres Passwort, ``http-pap``), Klick (Trial-Login) und Formular (Felder gehen
per ``fetch`` an ``POST /api/v1/portal/<id>/register``, danach Trial-Login). Die Plattform-Adresse steht dafür im
Walled Garden. ANNAHME (Labor): Pfade, Feldnamen, Trial-Benutzername ``T-<MAC>``, Login per ``http-pap`` mit leerem
Passwort, Variablen der Login-Seite (``$(link-login-only)`` …), Upload-Ziel des HTML-Verzeichnisses.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import secrets
import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, GuestRegistration, HotspotInstance, HotspotPortal, Tenant, Voucher, VoucherProfile
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI, _norm
from app.routeros.naming import routeros_safe_name

log = logging.getLogger(__name__)

PREFIX = "sdwan-hs-"
COMMENT = "sdwan:hs:"
LOGIN_TYPES = ("voucher", "click", "form")
FIELD_TYPES = ("text", "email", "tel", "checkbox")
DEFAULT_RETENTION_DAYS = 30
REGISTER_MAX_BYTES = 4096
REGISTER_LIMIT = (10, 600)  # max. 10 Registrierungen je IP und Portal in 10 Minuten
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
_RATE = re.compile(r"^\d+[kKmMgG]?(/\d+[kKmMgG]?)?$")
_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_FILE = re.compile(r"^[a-z0-9_-]{1,40}\.(html|css|js|txt)$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOGO = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class HotspotError(ValueError):
    pass


class HotspotBlocked(HotspotError):
    """Sicherheitsmeldung (Phase 23) verhindert das Ausrollen – erst Firmware aktualisieren."""


# ----------------------------------------------------------------------------- Validierung
def check_slug(s: str) -> None:
    if not _SLUG.match(s or ""):
        raise HotspotError("Kürzel: a–z, 0–9, '-' (max. 20 Zeichen)")


def check_rate(r: str | None) -> None:
    if r and not _RATE.match(r):
        raise HotspotError("Bandbreite als Upload/Download, z. B. 5M/20M")


def check_hosts(hosts: list[str]) -> None:
    for h in hosts:
        if not _HOST.match(h):
            raise HotspotError(f"Walled Garden: ungültiger Host {h!r} (z. B. example.com oder *.example.com)")


def check_portal(data: dict[str, Any]) -> None:
    if data.get("login_type") not in LOGIN_TYPES:
        raise HotspotError("Anmeldeart: voucher | click | form")
    design = data.get("design") or {}
    for k in ("primary", "background", "text"):
        if design.get(k) and not _COLOR.match(str(design[k])):
            raise HotspotError(f"Farbe {k}: #RRGGBB")
    logo = design.get("logo")
    if logo and (len(logo) > 200_000 or not _LOGO.match(logo)):
        raise HotspotError("Logo: PNG, JPEG oder WebP, max. ca. 150 KB")
    keys = set()
    for f in data.get("form_fields") or []:
        if not re.match(r"^[a-z][a-z0-9_]{0,29}$", str(f.get("key", ""))) or f["key"] in keys:
            raise HotspotError("Formularfeld: eindeutiger Schlüssel a–z, 0–9, _")
        keys.add(f["key"])
        if f.get("type") not in FIELD_TYPES:
            raise HotspotError(f"Feldtyp: {', '.join(FIELD_TYPES)}")
        if not 1 <= int(f.get("max_len") or 100) <= 500:
            raise HotspotError("Feldlänge 1–500")
    if data.get("login_type") == "form" and not data.get("form_fields"):
        raise HotspotError("Formular-Anmeldung braucht mindestens ein Feld")
    if len(data.get("form_fields") or []) > 10:
        raise HotspotError("Höchstens 10 Formularfelder")


def check_files(files: dict[str, str]) -> None:
    total = 0
    for name, content in files.items():
        if not _FILE.match(name):
            raise HotspotError(f"Dateiname {name!r}: a–z, 0–9, _-, Endung .html/.css/.js/.txt")
        if len(content) > 100_000:
            raise HotspotError(f"{name}: max. 100 KB")
        total += len(content)
    if total > 500_000:
        raise HotspotError("Login-Seiten zusammen max. 500 KB")
    if files and "login.html" not in files:
        raise HotspotError("Eigene Login-Seiten brauchen mindestens login.html")


def generate_code(existing: set[str], length: int = 8) -> str:
    for _ in range(1000):
        c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        if c not in existing:
            existing.add(c)
            return c
    raise HotspotError("Keine eindeutigen Codes mehr erzeugbar")


def base(inst: HotspotInstance) -> str:
    return routeros_safe_name(f"{PREFIX}{inst.slug}")


def retention_days(tenant: Tenant | None) -> int:
    return int(((tenant.settings or {}) if tenant else {}).get("guest_retention_days") or DEFAULT_RETENTION_DAYS)


def platform_host() -> str | None:
    return urlparse(get_settings().public_url).hostname


def register_url(inst: HotspotInstance) -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/v1/portal/{inst.id}/register"


def login_url(inst: HotspotInstance, address: str | None) -> str:
    host = inst.dns_name or address or "hotspot"
    return f"http://{host}/login"


# ----------------------------------------------------------------------------- Login-Seiten
def _t(v: Any) -> str:
    """Text für die Login-Seite: HTML-escapen und ``$`` entschärfen (RouterOS ersetzt ``$(…)``)."""
    return html.escape(str(v or "")).replace("$", "&#36;")


def _page(portal: HotspotPortal, body: str, extra_js: str = "") -> str:
    d = {"primary": "#1f4e79", "background": "#f5f5f5", "text": "#1d1d1f", **(portal.design or {})}
    logo = f'<img class="logo" src="{d["logo"]}" alt="">' if d.get("logo") else ""
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="pragma" content="no-cache"><title>WLAN</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:{d["background"]};color:{d["text"]}}}
.box{{max-width:420px;margin:0 auto;padding:32px 20px}}.logo{{max-width:180px;max-height:90px;display:block;margin:0 auto 20px}}
h1{{font-size:26px;margin:0 0 8px}}p{{line-height:1.45}}label{{display:block;margin:12px 0 4px;font-size:14px}}
input[type=text],input[type=email],input[type=tel]{{width:100%;box-sizing:border-box;padding:12px;border:1px solid #bbb;border-radius:8px;font-size:16px}}
.chk{{display:flex;gap:8px;align-items:flex-start;margin:14px 0}}.chk input{{margin-top:3px}}
button{{width:100%;padding:14px;margin-top:16px;border:0;border-radius:8px;background:{d["primary"]};color:#fff;font-size:17px;cursor:pointer}}
.terms{{font-size:13px;opacity:.8;border-top:1px solid rgba(0,0,0,.1);margin-top:22px;padding-top:12px}}
.err{{background:#fde8e8;color:#8a1c1c;padding:10px;border-radius:8px}}.lang{{text-align:right;font-size:13px}}
.lang a{{color:inherit;margin-left:8px}}[data-l]{{display:none}}html[lang=de] [data-l=de],html[lang=en] [data-l=en]{{display:revert}}
</style></head><body><div class="box">
<div class="lang"><a href="#" onclick="setL('de');return false">DE</a><a href="#" onclick="setL('en');return false">EN</a></div>
{logo}{body}
</div><script>
function setL(l){{document.documentElement.lang=l}}
setL((navigator.language||'de').slice(0,2)=='de'?'de':'en');
{extra_js}
</script></body></html>
"""


def _both(portal: HotspotPortal, key: str, tag: str = "span") -> str:
    texts = portal.texts or {}
    return "".join(f'<{tag} data-l="{lang}">{_t((texts.get(lang) or {}).get(key))}</{tag}>' for lang in ("de", "en"))


def render_pages(portal: HotspotPortal, inst: HotspotInstance) -> dict[str, str]:
    """Login-Seiten für RouterOS. Eigene Dateien des Portals ersetzen die erzeugten gleichen Namens."""
    lt = portal.login_type
    terms = ""
    if portal.terms_required:
        terms = f'<div class="chk"><input type="checkbox" id="terms" required><label for="terms" style="margin:0">{_both(portal, "terms_label")}</label></div>'
    fields = ""
    if lt == "voucher":
        fields = f'<label for="code">{_both(portal, "code_label")}</label><input type="text" id="code" name="username" autocomplete="off" required>' \
                 '<input type="hidden" name="password" value="">'
    elif lt == "click":
        fields = '<input type="hidden" name="username" value="T-$(mac-esc)"><input type="hidden" name="password" value="">'
    else:
        fields = '<input type="hidden" name="username" value="T-$(mac-esc)"><input type="hidden" name="password" value="">'
        for f in portal.form_fields or []:
            req = " required" if f.get("required") else ""
            lab = f'<span data-l="de">{_t(f.get("label_de"))}</span><span data-l="en">{_t(f.get("label_en"))}</span>'
            if f["type"] == "checkbox":
                fields += f'<div class="chk"><input type="checkbox" data-f="{f["key"]}" id="f_{f["key"]}"{req}><label for="f_{f["key"]}" style="margin:0">{lab}</label></div>'
            else:
                fields += (f'<label for="f_{f["key"]}">{lab}</label><input type="{f["type"]}" data-f="{f["key"]}" id="f_{f["key"]}" '
                           f'maxlength="{int(f.get("max_len") or 100)}"{req}>')
    js = ""
    if lt == "form":
        js = f"""document.getElementById('lf').addEventListener('submit',function(ev){{
if(this.dataset.ok)return;ev.preventDefault();var f=this,d={{}};
document.querySelectorAll('[data-f]').forEach(function(e){{d[e.getAttribute('data-f')]=e.type=='checkbox'?e.checked:e.value}});
fetch({json.dumps(register_url(inst))},{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{fields:d,terms_accepted:!!(document.getElementById('terms')||{{}}).checked,lang:document.documentElement.lang}})}})
.then(function(r){{if(!r.ok)throw 0;f.dataset.ok=1;f.submit()}}).catch(function(){{document.getElementById('rerr').style.display=''}});}});"""
    login = _page(portal, f"""<h1>{_both(portal, "title")}</h1><p>{_both(portal, "welcome")}</p>
$(if error)<p class="err">$(error)</p>$(endif)
<p class="err" id="rerr" style="display:none"><span data-l="de">Anmeldung fehlgeschlagen – bitte erneut versuchen.</span><span data-l="en">Sign-in failed – please try again.</span></p>
<form id="lf" name="login" action="$(link-login-only)" method="post">
<input type="hidden" name="dst" value="$(link-orig)"><input type="hidden" name="popup" value="false">
{fields}{terms}<button type="submit">{_both(portal, "button")}</button></form>
<div class="terms">{_both(portal, "terms", "p")}</div>""", js)
    status = _page(portal, f"""<h1>{_both(portal, "title")}</h1><p>{_both(portal, "success")}</p>
<p>$(if login-by == 'trial')$(else)$(username)$(endif) · $(uptime)</p>
<form action="$(link-logout)" method="post"><button type="submit"><span data-l="de">Abmelden</span><span data-l="en">Log out</span></button></form>""")
    alogin = _page(portal, f"""<h1>{_both(portal, "title")}</h1><p>{_both(portal, "success")}</p>
$(if link-redirect)<p><a href="$(link-redirect)">$(link-redirect)</a></p>$(endif)""")
    logout = _page(portal, f"""<h1>{_both(portal, "title")}</h1>
<p><span data-l="de">Sie wurden abgemeldet.</span><span data-l="en">You have been logged out.</span></p>
<form action="$(link-login)" method="get"><button type="submit">{_both(portal, "button")}</button></form>""")
    pages = {"login.html": login, "status.html": status, "alogin.html": alogin, "logout.html": logout}
    pages.update(portal.custom_files or {})
    return pages


def preview(portal: HotspotPortal, inst: HotspotInstance | None, page: str = "login.html") -> str:
    """Vorschau: RouterOS-Variablen durch Beispielwerte ersetzen, Bedingungen entfernen."""
    dummy = inst or HotspotInstance(id=uuid.UUID(int=0), slug="preview", name="Vorschau")
    src = render_pages(portal, dummy).get(page) or ""
    src = re.sub(r"\$\(if error\).*?\$\(endif\)", "", src, flags=re.S)
    src = re.sub(r"\$\(if [^)]*\)(.*?)\$\(else\)(.*?)\$\(endif\)", r"\2", src, flags=re.S)
    src = re.sub(r"\$\(if [^)]*\)(.*?)\$\(endif\)", r"\1", src, flags=re.S)
    values = {"link-login-only": "#", "link-orig": "#", "mac-esc": "00:00:00:00:00:00", "link-logout": "#", "link-login": "#",
              "username": "GAST", "uptime": "5m", "link-redirect": "#"}
    src = re.sub(r"\$\(([a-z-]+)\)", lambda m: values.get(m.group(1), ""), src)
    # Vorschau: Formular nicht absenden
    return src.replace("<script>", "<script>document.addEventListener('submit',function(e){e.preventDefault()},true);", 1)


# ----------------------------------------------------------------------------- Router
async def _interface_address(api: DeviceAPI, iface: str) -> str | None:
    for a in await api.print("/ip/address"):
        if a.get("interface") == iface and _norm(a.get("disabled", "false")) != "true":
            return str(a.get("address", "")).split("/")[0] or None
    return None


async def upload_files(device: Device, api: DeviceAPI, directory: str, files: dict[str, str]) -> None:
    """Login-Seiten hochladen. Simulator: Tabelle ``/file``; echt: SFTP über den Tunnel.
    ANNAHME (Labor): Dateien unter ``<verzeichnis>/…`` im Wurzelverzeichnis, ``html-directory=<verzeichnis>``."""
    s = get_settings()
    if s.routeros_backend == "simulator":
        router = api.conn.router  # type: ignore[attr-defined]
        router.tables.setdefault("/file", [])
        router.tables["/file"] = [f for f in router.tables["/file"] if not str(f.get("name", "")).startswith(f"{directory}/")]
        for name, content in files.items():
            router._insert("/file", {"name": f"{directory}/{name}", "type": "file", "contents": content, "size": str(len(content))})
        return
    import asyncssh

    from app.security import decrypt_secret

    try:
        async with asyncssh.connect(device.tunnel_ip, port=s.ssh_port, username=s.routeros_api_user,
                                    password=decrypt_secret(device.api_password_enc), known_hosts=None, connect_timeout=15) as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    await sftp.mkdir(directory)
                except asyncssh.SFTPError:
                    pass  # existiert bereits
                for name, content in files.items():
                    async with sftp.open(f"{directory}/{name}", "w") as fh:
                        await fh.write(content)
    except (OSError, asyncssh.Error) as exc:
        raise RouterOSError(f"Upload der Login-Seiten fehlgeschlagen: {exc}") from exc


async def _sync_named(api: DeviceAPI, path: str, prefix: str, want: dict[str, dict[str, Any]], stats: dict[str, int], remove: bool = True) -> None:
    rows = [r for r in await api.print(path) if str(r.get("name", "")).startswith(prefix)]
    have = {str(r["name"]): r for r in rows}
    for name, attrs in want.items():
        row = have.pop(name, None)
        if row is None:
            await api.add(path, name=name, **attrs)
            stats["added"] += 1
        else:
            diff = {k: v for k, v in attrs.items() if _norm(row.get(k, "")) != _norm(v)}
            if diff:
                await api.set(path, row[".id"], **diff)
                stats["changed"] += 1
    if remove:
        for row in have.values():
            await api.remove(path, row[".id"])
            stats["removed"] += 1


async def _sync_comment(api: DeviceAPI, path: str, key_field: str, prefix: str, want: dict[str, dict[str, Any]], stats: dict[str, int]) -> None:
    rows = [r for r in await api.print(path) if str(r.get("comment", "")).startswith(prefix)]
    have = {str(r.get(key_field)): r for r in rows}
    for key, attrs in want.items():
        row = have.pop(key, None)
        if row is None:
            await api.add(path, **attrs)
            stats["added"] += 1
        else:
            diff = {k: v for k, v in attrs.items() if _norm(row.get(k, "")) != _norm(v)}
            if diff:
                await api.set(path, row[".id"], **diff)
                stats["changed"] += 1
    for row in have.values():
        await api.remove(path, row[".id"])
        stats["removed"] += 1


def _voucher_user(inst: HotspotInstance, v: Voucher, prof: VoucherProfile) -> dict[str, Any]:
    u: dict[str, Any] = {"name": v.code, "password": "", "server": base(inst), "profile": f"{base(inst)}-v-{prof.slug}",
                         "limit-uptime": f"{prof.validity_min}m", "comment": f"{COMMENT}{inst.slug}:v",
                         "disabled": "yes" if v.status == "blocked" else "no"}
    if prof.data_limit_mb:
        u["limit-bytes-total"] = str(prof.data_limit_mb * 1024 * 1024)
    return u


async def apply_instance(db: AsyncSession, inst: HotspotInstance, remove: bool = False) -> dict[str, Any]:
    """Hotspot auf dem Gerät herstellen (oder bei ``remove`` vollständig entfernen)."""
    dev = await db.get(Device, inst.device_id)
    if dev is None:
        raise HotspotError("Gerät gelöscht")
    if dev.status == DeviceStatus.offline:
        raise RouterOSError("Gerät offline")
    if not remove:  # Phase 23: Entfernen bleibt immer erlaubt
        from app.services.advisories import block_message, blocking

        advs = await blocking(db, dev, "hotspot")
        if advs:
            raise HotspotBlocked(block_message(dev, advs))
    portal = await db.get(HotspotPortal, inst.portal_id)
    b = base(inst)
    stats = {"added": 0, "changed": 0, "removed": 0}
    async with connect_device(dev) as api:
        if remove:
            for path, key, prefix in (("/ip/hotspot/user", "name", f"{COMMENT}{inst.slug}:"), ("/ip/hotspot/ip-binding", "mac-address", f"{COMMENT}{inst.slug}:"),
                                      ("/ip/hotspot/walled-garden", "dst-host", f"{COMMENT}{inst.slug}"),
                                      ("/ip/hotspot/walled-garden/ip", "dst-host", f"{COMMENT}{inst.slug}")):
                await _sync_comment(api, path, key, prefix, {}, stats)
            await _sync_named(api, "/ip/hotspot", b, {}, stats)
            await _sync_named(api, "/ip/hotspot/user/profile", f"{b}-", {}, stats)
            await _sync_named(api, "/ip/hotspot/profile", b, {}, stats)
            return {"status": "removed", "stats": stats}
        address = inst.hotspot_address or await _interface_address(api, inst.interface)
        if not address:
            raise HotspotError(f"Interface {inst.interface} hat keine IP-Adresse – Hotspot braucht eine Adresse im Gästenetz")
        await upload_files(dev, api, b, render_pages(portal, inst))
        trial = portal.login_type in ("click", "form")
        prof: dict[str, Any] = {"hotspot-address": address, "html-directory": b,
                                "login-by": "http-pap,trial" if trial else "http-pap"}
        if trial:
            prof["trial-uptime-limit"] = f"{inst.session_timeout_min}m"
            prof["trial-user-profile"] = f"{b}-trial"
        if inst.dns_name:
            prof["dns-name"] = inst.dns_name
        uprofiles: dict[str, dict[str, Any]] = {f"{b}-trial": {"rate-limit": inst.rate_limit or "", "idle-timeout": f"{inst.idle_timeout_min}m",
                                                                "shared-users": "1"}}
        vouchers = (await db.execute(select(Voucher).where(Voucher.instance_id == inst.id))).scalars().all()
        vprofiles = {p.id: p for p in (await db.execute(select(VoucherProfile).where(VoucherProfile.tenant_id == inst.tenant_id))).scalars()}
        for pid in {v.profile_id for v in vouchers}:
            p = vprofiles[pid]
            uprofiles[f"{b}-v-{p.slug}"] = {"rate-limit": p.rate_limit or inst.rate_limit or "", "idle-timeout": f"{inst.idle_timeout_min}m",
                                            "shared-users": str(p.shared_users)}
        # Reihenfolge: Profile → Hotspot → Walled Garden → Benutzer
        await _sync_named(api, "/ip/hotspot/profile", b, {b: prof}, stats)
        await _sync_named(api, "/ip/hotspot/user/profile", f"{b}-", uprofiles, stats)
        await _sync_named(api, "/ip/hotspot", b, {b: {"interface": inst.interface, "profile": b, "address-pool": "none",
                                                      "idle-timeout": f"{inst.idle_timeout_min}m", "disabled": "no" if inst.enabled else "yes"}}, stats)
        c = f"{COMMENT}{inst.slug}"
        wg = {h: {"dst-host": h, "action": "allow", "server": b, "comment": c} for h in inst.walled_garden or []}
        await _sync_comment(api, "/ip/hotspot/walled-garden", "dst-host", c, wg, stats)
        wg_ip: dict[str, dict[str, Any]] = {}
        host = platform_host()
        if portal.login_type == "form" and host:
            wg_ip[host] = {"dst-host": host, "action": "accept", "server": b, "comment": c}
        await _sync_comment(api, "/ip/hotspot/walled-garden/ip", "dst-host", c, wg_ip, stats)
        users = {v.code: _voucher_user(inst, v, vprofiles[v.profile_id]) for v in vouchers}
        await _sync_comment(api, "/ip/hotspot/user", "name", f"{COMMENT}{inst.slug}:v", users, stats)
        for v in vouchers:
            v.pushed = True
    inst.status, inst.last_error, inst.applied_at = "ok", None, utcnow()
    inst.applied_version, inst.applied_portal_version = inst.version, portal.version
    return {"status": "ok", "stats": stats, "address": address}


async def refresh_vouchers(db: AsyncSession, inst: HotspotInstance, api: DeviceAPI) -> int:
    """Voucher-Status aus ``/ip/hotspot/user`` (uptime, bytes) übernehmen."""
    rows = {str(r.get("name")): r for r in await api.print("/ip/hotspot/user") if str(r.get("comment", "")).startswith(f"{COMMENT}{inst.slug}:v")}
    vprofiles = {p.id: p for p in (await db.execute(select(VoucherProfile).where(VoucherProfile.tenant_id == inst.tenant_id))).scalars()}
    n = 0
    for v in (await db.execute(select(Voucher).where(Voucher.instance_id == inst.id))).scalars():
        r = rows.get(v.code)
        if r is None:
            continue
        up = parse_uptime(str(r.get("uptime", "0s")))
        total = int(r.get("bytes-in") or 0) + int(r.get("bytes-out") or 0)
        if up and not v.first_used_at:
            v.first_used_at = utcnow()
        v.uptime_s, v.bytes_total = up, total
        if v.status != "blocked":
            p = vprofiles.get(v.profile_id)
            exhausted = p is not None and (up >= p.validity_min * 60 or (p.data_limit_mb and total >= p.data_limit_mb * 1024 * 1024))
            v.status = "used" if exhausted else ("active" if up else "new")
        n += 1
    return n


def parse_uptime(s: str) -> int:
    """RouterOS-Dauer (``1w2d3h4m5s`` oder ``01:02:03``) → Sekunden."""
    if ":" in s:
        parts = [int(x) for x in re.findall(r"\d+", s.split("d")[-1])]
        days = int(re.match(r"(\d+)d", s).group(1)) if re.match(r"(\d+)d", s) else 0
        h, m, sec = ([0, 0, *parts])[-3:]
        return days * 86400 + h * 3600 + m * 60 + sec
    mult = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([wdhms])", s))


async def active_guests(api: DeviceAPI, inst: HotspotInstance) -> list[dict[str, Any]]:
    out = []
    for r in await api.print("/ip/hotspot/active"):
        if r.get("server") not in (None, "", base(inst)):
            continue
        out.append({"id": r.get(".id"), "user": r.get("user"), "address": r.get("address"), "mac": r.get("mac-address"),
                    "uptime": r.get("uptime"), "bytes_in": int(r.get("bytes-in") or 0), "bytes_out": int(r.get("bytes-out") or 0),
                    "trial": str(r.get("user", "")).startswith("T-")})
    return out


async def blocked_macs(api: DeviceAPI, inst: HotspotInstance) -> list[dict[str, Any]]:
    return [{"id": r.get(".id"), "mac": r.get("mac-address")} for r in await api.print("/ip/hotspot/ip-binding")
            if str(r.get("comment", "")) == f"{COMMENT}{inst.slug}:block"]


# ----------------------------------------------------------------------------- Registrierung (öffentlich)
_MEM_RATE: dict[str, list[float]] = {}


async def rate_limited(ip: str, instance_id: uuid.UUID) -> bool:
    """True, wenn die IP für dieses Portal das Limit überschritten hat (Redis, sonst im Prozess)."""
    import time

    limit, window = REGISTER_LIMIT
    key = f"sdwan:hs:reg:{instance_id}:{ip}"
    if get_settings().use_redis:
        try:
            from app.events import _get_redis

            r = _get_redis()
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, window)
            return n > limit
        except Exception:  # noqa: BLE001 - Fallback im Prozess
            pass
    now = time.time()
    hits = [t for t in _MEM_RATE.get(key, []) if now - t < window]
    hits.append(now)
    _MEM_RATE[key] = hits
    return len(hits) > limit


def clean_registration(portal: HotspotPortal, fields: Any) -> dict[str, Any]:
    """Nur definierte Felder, Typ- und Längenprüfung; unbekannte Felder werden verworfen."""
    if not isinstance(fields, dict):
        raise HotspotError("fields fehlt")
    out: dict[str, Any] = {}
    for f in portal.form_fields or []:
        v = fields.get(f["key"])
        if f["type"] == "checkbox":
            v = bool(v) if isinstance(v, bool) else False
            if f.get("required") and not v:
                raise HotspotError(f"{f['key']} erforderlich")
            out[f["key"]] = v
            continue
        v = "" if v is None else v
        if not isinstance(v, str):
            raise HotspotError(f"{f['key']}: Text erwartet")
        v = v.strip()
        if len(v) > int(f.get("max_len") or 100):
            raise HotspotError(f"{f['key']}: zu lang")
        if f.get("required") and not v:
            raise HotspotError(f"{f['key']} erforderlich")
        if v and f["type"] == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise HotspotError(f"{f['key']}: E-Mail ungültig")
        if v and f["type"] == "tel" and not re.match(r"^[0-9+()/ -]{3,40}$", v):
            raise HotspotError(f"{f['key']}: Telefonnummer ungültig")
        out[f["key"]] = v
    return out


# ----------------------------------------------------------------------------- Worker
async def hotspot_tick() -> None:
    """Alle 5 min: Voucher-Status übernehmen, noch nicht übertragene Voucher nachschieben."""
    async with system_session() as db:
        insts = (await db.execute(select(HotspotInstance).where(HotspotInstance.enabled.is_(True)))).scalars().all()
        for inst in insts:
            dev = await db.get(Device, inst.device_id)
            if dev is None or dev.status == DeviceStatus.offline or not dev.api_password_enc:
                continue
            try:
                pending = (await db.execute(select(Voucher.id).where(Voucher.instance_id == inst.id, Voucher.pushed.is_(False)).limit(1))).first()
                if pending:
                    await apply_instance(db, inst)
                async with connect_device(dev) as api:
                    await refresh_vouchers(db, inst, api)
            except (RouterOSError, HotspotError) as exc:
                inst.last_error = str(exc)
            await db.commit()


async def purge_registrations() -> int:
    """Täglich: Gäste-Registrierungen nach der Aufbewahrungsfrist des Mandanten löschen (DSGVO)."""
    total = 0
    async with system_session() as db:
        for t in (await db.execute(select(Tenant))).scalars():
            cutoff = utcnow() - dt.timedelta(days=retention_days(t))
            res = await db.execute(delete(GuestRegistration).where(GuestRegistration.tenant_id == t.id, GuestRegistration.created_at < cutoff))
            total += res.rowcount or 0
        await db.commit()
    return total
