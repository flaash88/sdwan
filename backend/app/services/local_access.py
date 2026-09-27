"""Vor-Ort-Zugang / Break-Glass (Phase 24).

Auf jedem verwalteten Router ein lokaler Notfall-Benutzer mit **eigenem** Zufallspasswort (24 Zeichen, ohne 0/O/l/1/I),
nur lokal nutzbar:

* Benutzer ``<name>`` (Mandanten-Einstellung, Default ``localadmin``) in Gruppe ``sdwan-local`` (volle Rechte),
  Kommentar ``sdwan:local``. ``address=`` = lokale Netze (Einstellung „Adressbeschränkung“, Default an).
* Interface-Liste ``sdwan-local-access``: Interfaces der Zonen Management/LAN bzw. der defconf-Liste ``LAN``
  (nie WAN). Die Firewall-Grundregel ``base:local-access`` (Phase 14) erlaubt WinBox/SSH aus dieser Liste – auch bei
  Default-Drop. MAC-WinBox wird auf dieselbe Liste beschränkt (Vorzustand gemerkt).
* Adressbeschränkung aus: ``address=`` leer; stattdessen werden ``/ip service`` winbox/ssh auf lokale Netze + Tunnel
  beschränkt (Vorzustand gemerkt). Aus WAN bleibt der Zugang über Firewall, MAC-Server-Liste und Dienst-Adressen gesperrt.
* Optionaler Service-Port: Ethernet-Port aus der Bridge, eigenes kleines Netz (Default 192.168.254.0/29) mit DHCP;
  sein Netz ist immer Teil von ``address=``.
* Lassen sich keine lokalen Netze ermitteln (und sind keine manuell angegeben): Status ``not_created`` mit Grund –
  es wird nichts angelegt.

ANNAHME (Labor): Gruppen-Policies, ``/tool/mac-server/mac-winbox allowed-interface-list``, Login per MAC-WinBox mit
address-beschränktem Benutzer, DHCP-Server-Felder, ``/interface/bridge/port``.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import ipaddress
import json
import logging
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, DeviceZoneMember, FwZone, LocalAccess, PairingStatus, Tenant, WanLink
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI, _norm
from app.routeros.schema import LOCAL_GROUP, LOCAL_POLICIES
from app.security import decrypt_secret, encrypt_secret
from app.services.fw_compile import LOCAL_ACCESS_LIST

log = logging.getLogger(__name__)

GROUP = LOCAL_GROUP
COMMENT = "sdwan:local"
SP_COMMENT = "sdwan:local:sp"
DEFAULT_NAME = "localadmin"
DEFAULT_SP_NETWORK = "192.168.254.0/29"  # Default, je Gerät änderbar
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # ohne 0/O/l/1/I
PASSWORD_LENGTH = 24
# ANNAHME (Labor): Gruppe höchstens mit den Rechten der API-Gruppe anlegbar (siehe schema.LOCAL_POLICIES)
GROUP_POLICIES = ",".join(LOCAL_POLICIES)
ROTATE_AFTER_VIEW = dt.timedelta(hours=4)
_NAME_RE = r"^[a-z][a-z0-9_-]{2,31}$"


class LocalAccessError(ValueError):
    pass


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


# ----------------------------------------------------------------------------- Mandanten-Einstellungen
def tenant_settings(t: Tenant | None) -> dict[str, Any]:
    s = (t.settings or {}) if t else {}
    return {
        "local_admin_name": s.get("local_admin_name") or DEFAULT_NAME,
        "local_admin_address_restrict": s.get("local_admin_address_restrict", True),
        "local_access_auto": s.get("local_access_auto", True),
        "local_access_rotate_days": s.get("local_access_rotate_days"),
        "local_access_rotate_after_view": bool(s.get("local_access_rotate_after_view", False)),
        "local_access_age_recipient": s.get("local_access_age_recipient") or "",
        "local_access_webhook": bool(s.get("local_access_webhook_url_enc")),
    }


# ----------------------------------------------------------------------------- Netze / Interfaces
async def wan_interfaces(db: AsyncSession, device: Device, api: DeviceAPI) -> set[str]:
    """Alles, was nach WAN aussieht: WAN-Konfiguration der Plattform, Listen sdwan-wan/WAN, DHCP-Clients."""
    out = {lk.interface for lk in (await db.execute(select(WanLink).where(WanLink.device_id == device.id))).scalars()}
    for m in await api.print("/interface/list/member"):
        if m.get("list") in ("sdwan-wan", "WAN"):
            out.add(str(m.get("interface")))
    for c in await api.print("/ip/dhcp-client"):
        out.add(str(c.get("interface")))
    return {x for x in out if x and x != "None"}


async def local_interfaces(db: AsyncSession, device: Device, api: DeviceAPI) -> list[str]:
    """Interfaces der Zonen Management/LAN (Plattform), sonst Mitglieder der defconf-Liste LAN."""
    zones = {z.id: z for z in (await db.execute(select(FwZone).execution_options(skip_tenant_filter=True))).scalars()}
    members = (await db.execute(select(DeviceZoneMember).where(DeviceZoneMember.device_id == device.id))).scalars().all()
    ifaces = [m.interface for m in members if (z := zones.get(m.zone_id)) and (z.management or z.slug == "lan")]
    if not ifaces:
        ifaces = [str(m.get("interface")) for m in await api.print("/interface/list/member") if m.get("list") == "LAN"]
    return sorted(set(ifaces))


def _networks_of(addresses: list[dict[str, Any]], ifaces: set[str]) -> list[str]:
    out = []
    for a in addresses:
        if a.get("interface") in ifaces and _norm(a.get("disabled", "false")) != "true" and a.get("address"):
            try:
                out.append(str(ipaddress.ip_interface(str(a["address"])).network))
            except ValueError:
                continue
    return sorted(set(out))


def validate_manual(networks: list[str], wan_nets: list[str]) -> list[str]:
    """Manuell erlaubte Netze: gültige CIDR, kein 0.0.0.0/0, keine Überschneidung mit WAN-Netzen."""
    out = []
    for n in networks:
        try:
            net = ipaddress.ip_network(str(n).strip(), strict=False)
        except ValueError as exc:
            raise LocalAccessError(f"Ungültiges Netz: {n}") from exc
        if net.prefixlen == 0:
            raise LocalAccessError("0.0.0.0/0 ist nicht erlaubt")
        for w in wan_nets:
            if net.overlaps(ipaddress.ip_network(w, strict=False)):
                raise LocalAccessError(f"{net} überschneidet sich mit dem WAN-Netz {w}")
        out.append(str(net))
    return sorted(set(out))


def sp_plan(network: str) -> dict[str, str]:
    net = ipaddress.ip_network(network, strict=True)
    hosts = list(net.hosts())
    if len(hosts) < 2:
        raise LocalAccessError("Service-Port-Netz zu klein (mindestens /30)")
    return {"address": f"{hosts[0]}/{net.prefixlen}", "gateway": str(hosts[0]), "pool": f"{hosts[1]}-{hosts[-1]}", "network": str(net)}


# ----------------------------------------------------------------------------- Router abgleichen
async def _set_one(api: DeviceAPI, path: str, match: dict[str, Any], want: dict[str, Any]) -> str:
    rows = [r for r in await api.print(path) if all(str(r.get(k)) == str(v) for k, v in match.items())]
    if rows:
        diff = {k: v for k, v in want.items() if _norm(rows[0].get(k, "")) != _norm(v)}
        if diff:
            await api.set(path, rows[0][".id"], **diff)
        return "updated" if diff else "unchanged"
    await api.add(path, **{**match, **want})
    return "added"


async def apply(db: AsyncSession, la: LocalAccess, device: Device, rotate_password: str | None = None) -> dict[str, Any]:
    """Vor-Ort-Zugang herstellen. Setzt Status/Grund; wirft nicht bei fachlichen Gründen (not_created)."""
    tenant = await db.get(Tenant, device.tenant_id)
    ts = tenant_settings(tenant)
    s = get_settings()
    async with connect_device(device) as api:
        addrs = await api.print("/ip/address")
        wan = await wan_interfaces(db, device, api)
        wan_nets = _networks_of(addrs, wan)
        ifaces = [i for i in await local_interfaces(db, device, api) if i not in wan]
        sp = dict(la.service_port or {})
        if sp.get("enabled"):
            if sp.get("interface") in wan:
                raise LocalAccessError(f"Service-Port {sp['interface']} ist ein WAN-Interface")
            ifaces = sorted(set(ifaces) | {sp["interface"]})
        networks = _networks_of(addrs, set(ifaces))
        if la.manual_networks:
            manual = validate_manual(la.manual_networks, wan_nets)
            networks = sorted(set(networks) | set(manual))
            for a in addrs:  # Interfaces, deren Adresse in einem manuellen Netz liegt, gehören ebenfalls in die Liste
                try:
                    ip = ipaddress.ip_interface(str(a.get("address"))).ip
                except ValueError:
                    continue
                if a.get("interface") not in wan and any(ip in ipaddress.ip_network(m) for m in manual):
                    ifaces = sorted(set(ifaces) | {str(a.get("interface"))})
        if sp.get("enabled"):
            networks = sorted(set(networks) | {sp_plan(sp.get("network") or DEFAULT_SP_NETWORK)["network"]})
        if not networks:
            la.status = "not_created"
            la.reason = ("Keine lokalen Netze ermittelbar (keine Zone Management/LAN mit Adresse, keine defconf-Liste LAN). "
                         "Erlaubte Netze manuell angeben.")
            return {"status": la.status, "reason": la.reason}
        if sp.get("enabled"):
            await _service_port(api, la, sp)
        password = rotate_password or (decrypt_secret(la.password_enc) if la.password_enc else generate_password())
        restrict = ts["local_admin_address_restrict"]
        # Reihenfolge: Liste (+Mitglieder) → Gruppe → Benutzer → MAC-WinBox → ggf. Dienst-Adressen
        await _set_one(api, "/interface/list", {"name": LOCAL_ACCESS_LIST}, {"comment": COMMENT})
        have = {str(m.get("interface")): m for m in await api.print("/interface/list/member") if m.get("list") == LOCAL_ACCESS_LIST}
        for i in ifaces:
            if i not in have:
                await api.add("/interface/list/member", list=LOCAL_ACCESS_LIST, interface=i, comment=f"{COMMENT}:{i}")
        for i, m in have.items():
            if i not in ifaces and str(m.get("comment", "")).startswith(COMMENT):
                await api.remove("/interface/list/member", m[".id"])
        await _set_one(api, "/user/group", {"name": GROUP}, {"policy": GROUP_POLICIES, "comment": COMMENT})
        users = [u for u in await api.print("/user") if u.get("name") == la.username]
        if users and not str(users[0].get("comment", "")).startswith(COMMENT):
            raise LocalAccessError(f"Auf dem Router gibt es bereits einen fremden Benutzer „{la.username}“ – anderen Namen wählen")
        attrs = {"group": GROUP, "address": ",".join(networks) if restrict else "", "comment": COMMENT}
        if users:
            await api.set("/user", users[0][".id"], **attrs, password=password)
        else:
            await api.add("/user", name=la.username, password=password, **attrs)
        mw = (await api.print("/tool/mac-server/mac-winbox") or [{}])[0]
        if la.mac_winbox_before is None:
            la.mac_winbox_before = {"allowed-interface-list": mw.get("allowed-interface-list", "")}
        if mw.get("allowed-interface-list") != LOCAL_ACCESS_LIST:
            await api.call("/tool/mac-server/mac-winbox/set", **{"allowed-interface-list": LOCAL_ACCESS_LIST})
        if not restrict:
            await _restrict_services(api, la, networks + [f"{s.wg_hub_ip}/32"])
        elif la.services_before:
            await _restore_services(api, la)
    if not la.password_enc or rotate_password:
        la.password_enc, la.password_set_at = encrypt_secret(password), utcnow()
    la.networks, la.interfaces, la.status, la.reason, la.applied_at = networks, ifaces, "active", None, utcnow()
    return {"status": "active", "networks": networks, "interfaces": ifaces}


async def _restrict_services(api: DeviceAPI, la: LocalAccess, allowed: list[str]) -> None:
    rows = {str(r.get("name")): r for r in await api.print("/ip/service")}
    if la.services_before is None:
        la.services_before = {n: {"address": rows[n].get("address") or ""} for n in ("winbox", "ssh") if n in rows}
    for n in ("winbox", "ssh"):
        if n in rows:
            await api.set("/ip/service", rows[n][".id"], address=",".join(allowed))


async def _restore_services(api: DeviceAPI, la: LocalAccess) -> None:
    rows = {str(r.get("name")): r for r in await api.print("/ip/service")}
    for n, st in (la.services_before or {}).items():
        if n in rows:
            await api.set("/ip/service", rows[n][".id"], address=st.get("address", ""))
    la.services_before = None


async def _service_port(api: DeviceAPI, la: LocalAccess, sp: dict[str, Any]) -> None:
    """Port aus der Bridge nehmen (Vorzustand merken), Adresse, Pool, DHCP-Server, DHCP-Netz. Alles ``sdwan:local:sp``."""
    iface = sp["interface"]
    plan = sp_plan(sp.get("network") or DEFAULT_SP_NETWORK)
    for bp in await api.print("/interface/bridge/port"):
        if bp.get("interface") == iface:
            if not sp.get("bridge_before"):
                sp["bridge_before"] = {k: v for k, v in bp.items() if k in ("bridge", "interface", "pvid", "comment")}
            await api.remove("/interface/bridge/port", bp[".id"])
    await _set_one(api, "/ip/address", {"comment": SP_COMMENT}, {"address": plan["address"], "interface": iface})
    await _set_one(api, "/ip/pool", {"name": "sdwan-local-sp"}, {"ranges": plan["pool"], "comment": SP_COMMENT})
    await _set_one(api, "/ip/dhcp-server", {"name": "sdwan-local-sp"}, {"interface": iface, "address-pool": "sdwan-local-sp",
                                                                      "disabled": "no", "comment": SP_COMMENT})
    await _set_one(api, "/ip/dhcp-server/network", {"comment": SP_COMMENT}, {"address": plan["network"], "gateway": plan["gateway"]})
    la.service_port = {**sp, "network": plan["network"]}


async def remove_service_port(api: DeviceAPI, la: LocalAccess) -> None:
    for path in ("/ip/dhcp-server/network", "/ip/dhcp-server", "/ip/pool", "/ip/address"):
        for r in await api.print(path):
            if str(r.get("comment", "")) == SP_COMMENT:
                await api.remove(path, r[".id"])
    before = (la.service_port or {}).get("bridge_before")
    if before and not any(bp.get("interface") == before.get("interface") for bp in await api.print("/interface/bridge/port")):
        await api.add("/interface/bridge/port", **{k: v for k, v in before.items() if k in ("bridge", "interface", "pvid", "comment")})
    la.service_port = {**(la.service_port or {}), "enabled": False, "bridge_before": None}


async def disable(db: AsyncSession, la: LocalAccess, device: Device) -> None:
    """Vor-Ort-Zugang entfernen und Vorzustände zurückstellen (nur, was die Plattform angelegt/geändert hat)."""
    async with connect_device(device) as api:
        if (la.service_port or {}).get("enabled") or (la.service_port or {}).get("bridge_before"):
            await remove_service_port(api, la)
        if la.mac_winbox_before is not None:
            await api.call("/tool/mac-server/mac-winbox/set", **la.mac_winbox_before)
            la.mac_winbox_before = None
        if la.services_before:
            await _restore_services(api, la)
        for path in ("/user", "/interface/list/member", "/user/group"):
            for r in await api.print(path):
                if str(r.get("comment", "")).startswith(COMMENT):
                    await api.remove(path, r[".id"])
        # Liste bleibt (leer) bestehen: die Firewall-Grundregel verweist darauf
    la.enabled, la.status, la.interfaces = False, "disabled", []


# ----------------------------------------------------------------------------- Lebenszyklus
async def ensure_record(db: AsyncSession, device: Device) -> LocalAccess:
    la = (await db.execute(select(LocalAccess).where(LocalAccess.device_id == device.id))).scalar_one_or_none()
    if la is None:
        tenant = await db.get(Tenant, device.tenant_id)
        la = LocalAccess(tenant_id=device.tenant_id, device_id=device.id, username=tenant_settings(tenant)["local_admin_name"],
                         status="pending", enabled=True)
        db.add(la)
        await db.flush()
    return la


async def apply_safe(db: AsyncSession, la: LocalAccess, device: Device) -> dict[str, Any]:
    try:
        return await apply(db, la, device)
    except (RouterOSError, LocalAccessError) as exc:
        la.status, la.reason = "error", str(exc)
        return {"status": "error", "reason": str(exc)}


async def post_poll(db: AsyncSession, devices: list[Device]) -> None:
    """Post-Poll-Hook: ausstehende Vor-Ort-Zugänge (nach Onboarding/ZTP) auf erreichbaren Geräten anlegen."""
    ids = [d.id for d in devices if d.pairing_status == PairingStatus.paired and d.status != DeviceStatus.offline]
    if not ids:
        return
    pending = (await db.execute(select(LocalAccess).where(LocalAccess.device_id.in_(ids), LocalAccess.status == "pending",
                                                          LocalAccess.enabled.is_(True)))).scalars().all()
    by_id = {d.id: d for d in devices}
    for la in pending:
        res = await apply_safe(db, la, by_id[la.device_id])
        if res["status"] == "active":
            await after_change(db, la.tenant_id, "created")


async def on_paired(db: AsyncSession, device: Device) -> None:
    """Beim Onboarding: Datensatz anlegen (Status pending) – angelegt wird beim ersten Poll über die API."""
    tenant = await db.get(Tenant, device.tenant_id)
    if tenant_settings(tenant)["local_access_auto"]:
        await ensure_record(db, device)


async def rotate(db: AsyncSession, la: LocalAccess, device: Device) -> dict[str, Any]:
    """Neues Passwort nur für diesen Benutzer. Bei einem Router-Fehler bleibt das alte Passwort gespeichert und gültig."""
    if la.status != "active":
        raise LocalAccessError("Vor-Ort-Zugang ist nicht aktiv")
    new = generate_password()
    try:
        async with connect_device(device) as api:
            users = [u for u in await api.print("/user") if u.get("name") == la.username and str(u.get("comment", "")).startswith(COMMENT)]
            if not users:
                raise LocalAccessError("Vor-Ort-Benutzer auf dem Router nicht gefunden")
            await api.set("/user", users[0][".id"], password=new)
    except (RouterOSError, LocalAccessError) as exc:
        la.reason = f"Rotation fehlgeschlagen – altes Passwort bleibt gültig: {exc}"
        return {"ok": False, "error": str(exc)}
    la.password_enc, la.password_set_at, la.rotate_due_at, la.reason = encrypt_secret(new), utcnow(), None, None
    await after_change(db, la.tenant_id, "rotated")
    return {"ok": True}


async def rotation_tick() -> None:
    """Worker (stündlich): „nach Anzeige rotieren“ und Intervall-Rotation je Mandant."""
    from app.audit import audit

    async with system_session() as db:
        rows = (await db.execute(select(LocalAccess).where(LocalAccess.status == "active", LocalAccess.enabled.is_(True)))).scalars().all()
        tenants: dict[Any, dict[str, Any]] = {}
        for la in rows:
            if la.tenant_id not in tenants:
                tenants[la.tenant_id] = tenant_settings(await db.get(Tenant, la.tenant_id))
            ts = tenants[la.tenant_id]
            due = la.rotate_due_at is not None and la.rotate_due_at <= utcnow()
            days = ts["local_access_rotate_days"]
            if days and la.password_set_at and utcnow() - la.password_set_at >= dt.timedelta(days=int(days)):
                due = True
            if not due:
                continue
            dev = await db.get(Device, la.device_id)
            if dev is None or dev.status == DeviceStatus.offline:
                continue
            res = await rotate(db, la, dev)
            await audit(db, "local_access.rotate", tenant_id=la.tenant_id, target_type="device", target_id=dev.id, success=res["ok"],
                        details={"auto": True, "error": res.get("error")})
            await db.commit()


# ----------------------------------------------------------------------------- Export / Webhook
async def export_rows(db: AsyncSession, tenant_id: Any) -> list[dict[str, str]]:
    rows = (await db.execute(select(LocalAccess, Device).join(Device, Device.id == LocalAccess.device_id)
                             .where(LocalAccess.tenant_id == tenant_id, LocalAccess.password_enc.is_not(None)))).all()
    tenant = await db.get(Tenant, tenant_id)
    out = []
    for la, d in sorted(rows, key=lambda x: x[1].name):
        out.append({"Group": f"Vor-Ort-Zugang/{tenant.name if tenant else ''}", "Title": d.name, "Username": la.username,
                    "Password": decrypt_secret(la.password_enc), "URL": f"winbox://{(la.networks or [''])[0]}",
                    "Notes": f"Seriennr. {d.serial or '-'} · Modell {d.model or '-'} · nur lokal (LAN/Management/Service-Port) · "
                             f"Stand {(la.password_set_at or utcnow()).strftime('%Y-%m-%d %H:%M')} UTC"})
    return out


def keepass_csv(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["Group", "Title", "Username", "Password", "URL", "Notes"], quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def encrypt_age(data: bytes, recipient: str) -> bytes:
    import pyrage
    from pyrage import x25519

    try:
        rcpt = x25519.Recipient.from_str(recipient.strip())
    except Exception as exc:  # noqa: BLE001
        raise LocalAccessError(f"age-Empfänger ungültig: {exc}") from exc
    return pyrage.encrypt(data, [rcpt])


def encrypt_zip(data: bytes, password: str, name: str = "vor-ort-zugang.csv") -> bytes:
    import pyzipper

    if len(password) < 12:
        raise LocalAccessError("ZIP-Passwort: mindestens 12 Zeichen")
    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(password.encode())
        z.writestr(name, data)
    return buf.getvalue()


async def after_change(db: AsyncSession, tenant_id: Any, event: str) -> bool:
    """Nach Anlegen/Rotation: automatischer Export per Webhook – nur age-verschlüsselt, nie Klartext."""
    from app.services import webhook

    tenant = await db.get(Tenant, tenant_id)
    st = (tenant.settings or {}) if tenant else {}
    url_enc, rcpt = st.get("local_access_webhook_url_enc"), st.get("local_access_age_recipient")
    if not url_enc or not rcpt:
        return False
    try:
        await db.flush()
        data = encrypt_age(keepass_csv(await export_rows(db, tenant_id)), rcpt)
        url = decrypt_secret(url_enc)
        webhook.validate_url(url)
        return await webhook.send(url, {"source": "sdwan", "type": "local_access_export", "event": event, "tenant": tenant.slug,
                                        "filename": f"vor-ort-zugang-{tenant.slug}.csv.age", "encoding": "age+base64",
                                        "content": base64.b64encode(data).decode(), "at": utcnow().isoformat()})
    except Exception as exc:  # noqa: BLE001 - Export darf die Aktion nicht scheitern lassen
        log.warning("Vor-Ort-Export per Webhook: %s", exc)
        return False


def out(la: LocalAccess | None) -> dict[str, Any] | None:
    if la is None:
        return None
    return {"id": str(la.id), "device_id": str(la.device_id), "enabled": la.enabled, "status": la.status, "reason": la.reason,
            "username": la.username, "has_password": bool(la.password_enc), "password_set_at": la.password_set_at,
            "viewed_at": la.viewed_at, "rotate_due_at": la.rotate_due_at, "networks": la.networks or [], "interfaces": la.interfaces or [],
            "manual_networks": la.manual_networks or [], "service_port": {k: v for k, v in (la.service_port or {}).items() if k != "bridge_before"},
            "applied_at": la.applied_at}


_ = json  # (für Typprüfer)


KEPT_COMMENT = "lokaler Zugang (ehemals verwaltet)"


async def offboard_step(api: DeviceAPI, la: LocalAccess | None, keep: bool) -> dict[str, Any]:
    """Offboarding (vor dem Router-Script aus Schritt 6).

    * behalten: Benutzer, Gruppe, Liste, Mitglieder und Service-Port-Objekte bekommen einen Kommentar ohne ``sdwan:``
      (das Script entfernt sie dann nicht). Der Service-Port wird zusätzlich in die defconf-Liste ``LAN`` aufgenommen
      (falls vorhanden), damit die Werks-Firewall ihn nicht verwirft. Bei „Adressbeschränkung aus“ bleiben winbox/ssh
      auf die lokalen Netze beschränkt (ohne Tunnel-Adresse).
    * entfernen: MAC-WinBox, Dienst-Adressen und Bridge-Port zurückstellen; die Objekte entfernt das Script (``sdwan:``).
    """
    if la is None or la.status not in ("active", "error"):
        return {"local_access": "keiner"}
    if not keep:
        if (la.service_port or {}).get("enabled") or (la.service_port or {}).get("bridge_before"):
            await remove_service_port(api, la)
        if la.mac_winbox_before is not None:
            await api.call("/tool/mac-server/mac-winbox/set", **la.mac_winbox_before)
        if la.services_before:
            await _restore_services(api, la)
        return {"local_access": "entfernt"}
    n = 0
    for path in ("/user", "/user/group", "/interface/list", "/interface/list/member", "/ip/address", "/ip/pool", "/ip/dhcp-server",
                 "/ip/dhcp-server/network"):
        for r in await api.print(path):
            if str(r.get("comment", "")).startswith(COMMENT):
                await api.set(path, r[".id"], comment=KEPT_COMMENT)
                n += 1
    sp = la.service_port or {}
    if sp.get("enabled") and any(x.get("name") == "LAN" for x in await api.print("/interface/list")):
        if not any(m.get("list") == "LAN" and m.get("interface") == sp["interface"] for m in await api.print("/interface/list/member")):
            await api.add("/interface/list/member", list="LAN", interface=sp["interface"], comment=KEPT_COMMENT)
    if la.services_before:
        rows = {str(r.get("name")): r for r in await api.print("/ip/service")}
        for name in ("winbox", "ssh"):
            if name in rows:
                await api.set("/ip/service", rows[name][".id"], address=",".join(la.networks or []))
    return {"local_access": "behalten", "objekte": n, "benutzer": la.username}
