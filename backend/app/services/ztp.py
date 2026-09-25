"""Zero-Touch-Provisioning (Phase 6).

Ablauf:
1. **Staging** (MSP/Lager): Gerät mit Seriennummer + Template anlegen. Es entsteht ein langlebiger,
   an die Seriennummer gebundener Pairing-Token und ein Bootstrap-Script. Das Script wird vorab
   importiert (oder per Netinstall ``-s`` als Default-Konfiguration eingespielt).
2. **Erster Boot beim Kunden:** Der Bootstrap-Scheduler holt alle 60 s per DHCP-WAN das
   Onboarding-Script, bis das Pairing gelingt, und entfernt sich dann selbst.
3. **Pairing-Antwort** enthält zusätzlich die Basiskonfiguration aus dem Template
   (Identity, Zeitzone, NTP, DNS, LAN-Bridge, LAN-IP, DHCP-Server).
4. **Nach dem ersten erfolgreichen Poll** pusht die Control-Plane WAN-Konfiguration, weist die
   Template-Policies zu und deployt sie, und stößt das Mesh an.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.db import utcnow
from app.models import Device, DeviceStatus, FirewallPolicy, PolicyAssignment, PolicyDeployment, ProvisioningTemplate, Site, Tenant, WanLink
from app.routeros import RouterOSError
from app.services.onboarding import _q

log = logging.getLogger(__name__)

_IFACE = re.compile(r"^[A-Za-z0-9._\-/]{1,64}$")
_HOST = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
_TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_\-+]+){0,2}$")
_IDENT = re.compile(r"[^A-Za-z0-9._\-]")
ZTP_TOKEN_TTL_HOURS = 24 * 180


class TemplateError(ValueError):
    pass


def validate_template(c: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["identity_pattern"] = str(c.get("identity_pattern") or "{name}")
    if not re.fullmatch(r"[A-Za-z0-9._\-{}]{1,100}", out["identity_pattern"]):
        raise TemplateError("identity_pattern: nur Buchstaben, Ziffern, . _ - und {tenant} {site} {name} {serial}")
    tz = c.get("timezone") or "Europe/Vienna"
    if not _TZ.match(tz):
        raise TemplateError("Ungültige Zeitzone")
    out["timezone"] = tz
    for key in ("ntp_servers", "dns_servers"):
        vals = [str(v) for v in c.get(key) or []]
        for v in vals:
            if not _HOST.match(v):
                raise TemplateError(f"{key}: ungültiger Eintrag {v!r}")
        out[key] = vals
    wan_if = c.get("wan_interface") or "ether1"
    if not _IFACE.match(wan_if):
        raise TemplateError("wan_interface ungültig")
    out["wan_interface"] = wan_if
    lan = c.get("lan") or {}
    ports = [str(p) for p in lan.get("bridge_ports") or []]
    for p in ports:
        if not _IFACE.match(p) or p == wan_if:
            raise TemplateError(f"LAN-Port {p!r} ungültig (oder identisch mit WAN)")
    cidr = lan.get("cidr")
    if cidr:
        try:
            ipaddress.ip_interface(cidr)
        except ValueError as exc:
            raise TemplateError(f"LAN-CIDR ungültig: {cidr}") from exc
    out["lan"] = {"enabled": bool(lan.get("enabled", True)), "bridge_ports": ports, "cidr": cidr, "dhcp": bool(lan.get("dhcp", True))}
    wan = c.get("wan") or {}
    if wan:
        from app.api.v1.wan import WanConfigIn  # Validierung wiederverwenden

        try:
            parsed = WanConfigIn.model_validate({**wan, "push": False})
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"WAN-Vorlage ungültig: {exc}") from exc
        out["wan"] = parsed.model_dump(exclude={"push"})
    out["policy_ids"] = [str(p) for p in c.get("policy_ids") or []]
    vrrp = c.get("vrrp") or []
    if vrrp:
        from app.services.vrrp import VrrpError, validate_set

        try:
            out["vrrp"] = [{k: v for k, v in i.items() if k not in ("id", "state", "last_change_at")} for i in validate_set(list(vrrp))]
        except (VrrpError, TypeError, ValueError) as exc:
            raise TemplateError(f"VRRP-Vorlage ungültig: {exc}") from exc
    return out


def render_identity(pattern: str, device: Device, tenant: Tenant | None, site: Site | None) -> str:
    ident = pattern.format(
        name=device.name, serial=device.serial or "", tenant=tenant.slug if tenant else "", site=site.name if site else ""
    ) if "{" in pattern else pattern
    return _IDENT.sub("-", ident).strip("-")[:64] or device.name


def _lan_iface(t: dict[str, Any], site: Site | None) -> ipaddress.IPv4Interface | None:
    lan = t.get("lan") or {}
    if not lan.get("enabled", True):
        return None
    if lan.get("cidr"):
        return ipaddress.ip_interface(lan["cidr"])
    if site and site.lan_subnets:
        net = ipaddress.ip_network(site.lan_subnets[0])
        return ipaddress.ip_interface(f"{next(net.hosts())}/{net.prefixlen}")
    return None


def base_config_script(device: Device, template: ProvisioningTemplate, tenant: Tenant | None, site: Site | None) -> str:
    t = template.content or {}
    lines = [f"# --- SD-WAN Zero-Touch Basiskonfiguration (Template: {_IDENT.sub('-', template.name)}) ---"]
    lines.append(f"/system identity set name={_q(render_identity(t.get('identity_pattern', '{name}'), device, tenant, site))}")
    lines.append(f"/system clock set time-zone-name={t.get('timezone', 'Europe/Vienna')}")
    if t.get("ntp_servers"):
        lines.append(f"/system ntp client set enabled=yes servers={_q(','.join(t['ntp_servers']))}")
    if t.get("dns_servers"):
        lines.append(f"/ip dns set servers={_q(','.join(t['dns_servers']))} allow-remote-requests=yes")
    lan = t.get("lan") or {}
    iface = _lan_iface(t, site)
    if iface is not None:
        lines += [
            ':if ([:len [/interface bridge find name="bridge"]] = 0) do={ /interface bridge add name=bridge comment="sdwan:ztp" }',
        ]
        for port in lan.get("bridge_ports") or []:
            lines.append(f':do {{ /interface bridge port add bridge=bridge interface={_q(port)} comment="sdwan:ztp" }} on-error={{}}')
        lines += [
            '/ip address remove [find comment="sdwan:ztp:lan"]',
            f'/ip address add address={iface.with_prefixlen} interface=bridge comment="sdwan:ztp:lan"',
        ]
        if lan.get("dhcp", True):
            hosts = list(iface.network.hosts())
            pool_hosts = [h for h in hosts if h != iface.ip]
            start = pool_hosts[min(9, len(pool_hosts) - 1)]
            end = pool_hosts[-1]
            dns = iface.ip if t.get("dns_servers") else None
            lines += [
                '/ip dhcp-server remove [find name="sdwan-lan"]',
                '/ip dhcp-server network remove [find comment="sdwan:ztp:lan"]',
                '/ip pool remove [find name="sdwan-lan"]',
                f'/ip pool add name=sdwan-lan ranges={start}-{end} comment="sdwan:ztp:lan"',
                '/ip dhcp-server add name=sdwan-lan interface=bridge address-pool=sdwan-lan lease-time=8h disabled=no comment="sdwan:ztp:lan"',
                f'/ip dhcp-server network add address={iface.network} gateway={iface.ip}'
                + (f" dns-server={dns}" if dns else "")
                + ' comment="sdwan:ztp:lan"',
            ]
    lines.append(':log info "SD-WAN: Zero-Touch-Basiskonfiguration angewendet"')
    return "\n".join(lines)


def bootstrap_script(token: str, device: Device, template: ProvisioningTemplate | None) -> str:
    """Einmalig vorab zu importierendes Script (oder Netinstall-Default-Konfiguration)."""
    s = get_settings()
    url = f"{s.public_url.rstrip('/')}/api/v1/onboard/{token}.rsc"
    wan_if = ((template.content or {}).get("wan_interface") if template else None) or "ether1"
    return f"""# ==========================================================
# SD-WAN Zero-Touch Bootstrap – {_IDENT.sub('-', device.name)} (Serial {device.serial})
# Einmalig importieren: /import sdwan-ztp.rsc   oder   netinstall -s sdwan-ztp.rsc
# Der Router meldet sich beim ersten Boot mit Internet automatisch bei der Cloud an.
# ==========================================================
:if ([:len [/ip dhcp-client find interface={_q(wan_if)}]] = 0) do={{
  /ip dhcp-client add interface={_q(wan_if)} disabled=no comment="sdwan:ztp:wan"
}}
/system script remove [find name="sdwan-ztp"]
/system scheduler remove [find name="sdwan-ztp"]
/system script add name=sdwan-ztp policy=read,write,policy,test,sensitive,ftp source={{
  :do {{
    /tool fetch url="{url}" dst-path=sdwan-onboard.rsc
    :delay 2s
    /import file-name=sdwan-onboard.rsc
    :if ([:len [/interface wireguard peers find comment="sdwan:hub"]] > 0) do={{
      /system scheduler remove [find name="sdwan-ztp"]
      /system script remove [find name="sdwan-ztp"]
      :log info "SD-WAN: Zero-Touch-Onboarding erfolgreich"
    }}
  }} on-error={{ :log warning "SD-WAN: Cloud noch nicht erreichbar – neuer Versuch in 60s" }}
}}
/system scheduler add name=sdwan-ztp start-time=startup interval=1m on-event="/system script run sdwan-ztp" policy=read,write,policy,test,sensitive,ftp
:put "SD-WAN: Zero-Touch-Bootstrap installiert"
"""


async def ztp_script_for_device(db: AsyncSession, device: Device) -> str:
    """Hook in der Pairing-Antwort: Basiskonfiguration anhängen, falls Template zugewiesen."""
    if not device.ztp_template_id:
        return ""
    template = await db.get(ProvisioningTemplate, device.ztp_template_id)
    if template is None:
        return ""
    tenant = await db.get(Tenant, device.tenant_id)
    site = await db.get(Site, device.site_id) if device.site_id else None
    device.ztp_state = "paired"
    _log(device, "paired", "Pairing erfolgreich, Basiskonfiguration übertragen")
    return base_config_script(device, template, tenant, site)


def _log(device: Device, state: str, msg: str) -> None:
    device.ztp_log = [*(device.ztp_log or []), {"at": utcnow().isoformat(), "state": state, "msg": msg}][-50:]


async def provision_device(db: AsyncSession, device: Device) -> list[str]:
    """Stufe 2: WAN, Policies, Mesh. Liefert die IDs gestarteter Policy-Deployments."""
    from app.services.wan import WanError, apply_wan

    template = await db.get(ProvisioningTemplate, device.ztp_template_id) if device.ztp_template_id else None
    if template is None:
        device.ztp_state = "provisioned"
        return []
    t = template.content or {}
    device.ztp_state = "provisioning"
    errors: list[str] = []
    wan = t.get("wan")
    if wan and wan.get("links"):
        has = (await db.execute(select(WanLink).where(WanLink.device_id == device.id).limit(1))).first()
        if not has:
            for i, lk in enumerate(wan["links"], start=1):
                fields = {k: v for k, v in lk.items() if k != "slot"}
                db.add(WanLink(tenant_id=device.tenant_id, device_id=device.id, slot=lk.get("slot") or i, **fields))
            device.wan_mode = wan.get("mode", "failover")
            device.wan_options = {**(device.wan_options or {}), "recovery_delay_s": wan.get("recovery_delay_s", 30),
                                  "flush_connections": wan.get("flush_connections", True)}
            await db.flush()
        try:
            await apply_wan(db, device)
            _log(device, "provisioning", "WAN-Konfiguration angewendet")
        except (RouterOSError, WanError) as exc:
            errors.append(f"WAN: {exc}")
    vrrp_tpl = t.get("vrrp") or []
    if vrrp_tpl:
        from app.models import VrrpInstance
        from app.services.vrrp import VrrpError, apply_vrrp, validate_set

        has = (await db.execute(select(VrrpInstance).where(VrrpInstance.device_id == device.id).limit(1))).first()
        if not has:
            override = (device.facts or {}).get("ztp_vrrp_local_address")
            try:
                items = validate_set([{**i, "local_address": i.get("local_address") or override} for i in vrrp_tpl])
                for i in items:
                    db.add(VrrpInstance(tenant_id=device.tenant_id, device_id=device.id,
                                        **{k: v for k, v in i.items() if k in ("name", "interface", "vrid", "priority", "interval_ms", "preemption",
                                                                               "version", "vip", "local_address", "linked_wan_slot", "enabled")}))
                await db.flush()
            except VrrpError as exc:
                errors.append(f"VRRP: {exc}")
        if not any(e.startswith("VRRP") for e in errors):
            try:
                await apply_vrrp(db, device)
                _log(device, "provisioning", f"{len(vrrp_tpl)} VRRP-Instanz(en) angewendet")
            except RouterOSError as exc:
                errors.append(f"VRRP: {exc}")
    deployments: list[str] = []
    pol_ids = t.get("policy_ids") or []
    if pol_ids:
        pols = (await db.execute(select(FirewallPolicy).where(FirewallPolicy.id.in_([uuid.UUID(p) for p in pol_ids])))).scalars().all()
        existing = {a.policy_id for a in (await db.execute(select(PolicyAssignment).where(PolicyAssignment.device_id == device.id))).scalars()}
        for pos, p in enumerate(pols):
            if p.tenant_id not in (None, device.tenant_id):
                continue
            if p.id not in existing:
                db.add(PolicyAssignment(tenant_id=device.tenant_id, policy_id=p.id, device_id=device.id, position=100 + pos))
        dep = PolicyDeployment(tenant_id=device.tenant_id, policy_id=None, started_by="zero-touch")
        db.add(dep)
        await db.flush()
        deployments.append(str(dep.id))
        _log(device, "provisioning", f"{len(pols)} Policies zugewiesen, Push gestartet")
    if errors:
        device.ztp_state = "failed"
        _log(device, "failed", "; ".join(errors))
    else:
        device.ztp_state = "provisioned"
        _log(device, "provisioned", "Zero-Touch-Provisioning abgeschlossen")
    await events.publish(device.tenant_id, "device.ztp", {"id": str(device.id), "state": device.ztp_state})
    return deployments


async def ztp_post_poll(db: AsyncSession, devices: list[Device]) -> None:
    """Post-Poll-Hook: frisch gepairte ZTP-Geräte beim ersten Online-Poll provisionieren."""
    from app.services.policy import run_deployment

    todo = [d for d in devices if d.ztp_state == "paired" and d.status == DeviceStatus.online]
    started: list[tuple[str, uuid.UUID]] = []
    for dev in todo:
        try:
            for dep_id in await provision_device(db, dev):
                started.append((dep_id, dev.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("ZTP für %s fehlgeschlagen", dev.name)
            dev.ztp_state = "failed"
            _log(dev, "failed", str(exc))
    if started:
        await db.commit()
        for dep_id, dev_id in started:
            await run_deployment(uuid.UUID(dep_id), [dev_id])
    tenants = {d.tenant_id for d in todo if d.ztp_state == "provisioned" and d.site_id}
    for tid in tenants:
        tenant = await db.get(Tenant, tid)
        if tenant and tenant.mesh_topology != "none":
            from app.services.mesh import MeshError, apply_mesh

            try:
                await apply_mesh(db, tenant)
            except (MeshError, RouterOSError) as exc:
                log.info("Mesh nach ZTP für %s: %s", tenant.slug, exc)

