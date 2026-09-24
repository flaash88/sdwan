"""Content-Filtering über NextDNS (Phase 7).

* Profile werden in der DB gepflegt und per API zu NextDNS synchronisiert.
* Zuweisung: Standort -> Profil, sonst Mandanten-Standard (``tenant.settings.default_filter_profile``).
* Router: DNS-over-HTTPS (``/ip dns use-doh-server``) auf ``https://dns.nextdns.io/<profil>/<gerät>``,
  Bootstrap-Einträge für ``dns.nextdns.io``, CA-Vertrauen, Cache-Flush und optional
  NAT-Redirect aller DNS-Anfragen aus den LANs auf den Router (kein Umgehen per eigenem DNS).
  RouterOS unterstützt DoT nicht als Client – DoH ist daher der verwendete verschlüsselte Transport.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import ContentFilterProfile, Device, Site, Tenant
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.security import decrypt_secret
from app.services.nextdns import NextDNSClient, NextDNSError

log = logging.getLogger(__name__)

NEXTDNS_BOOTSTRAP = ["45.90.28.0", "45.90.30.0"]
CA_BUNDLE_URL = "https://curl.se/ca/cacert.pem"
_SLUG = re.compile(r"[^A-Za-z0-9\-]")


def _client_for(tenant: Tenant) -> NextDNSClient:
    enc = (tenant.settings or {}).get("nextdns_api_key_enc")
    return NextDNSClient(decrypt_secret(enc) if enc else None)


async def sync_profile(db: AsyncSession, profile: ContentFilterProfile) -> None:
    tenant = await db.get(Tenant, profile.tenant_id)
    assert tenant is not None
    try:
        client = _client_for(tenant)
        if not profile.nextdns_profile_id:
            profile.nextdns_profile_id = await client.create_profile(f"{tenant.slug}-{profile.name}")
        await client.apply(profile.nextdns_profile_id, {
            "name": f"{tenant.slug}-{profile.name}",
            "security": profile.security or {},
            "categories": profile.categories or [],
            "services": profile.services or [],
            "blocklists": profile.blocklists or [],
            "denylist": profile.denylist or [],
            "allowlist": profile.allowlist or [],
            "safe_search": profile.safe_search,
            "youtube_restricted": profile.youtube_restricted,
            "block_bypass": profile.block_bypass,
        })
        profile.sync_status, profile.last_error, profile.synced_at = "synced", None, utcnow()
    except NextDNSError as exc:
        profile.sync_status, profile.last_error = "error", str(exc)
        raise


async def profile_for_device(db: AsyncSession, device: Device) -> ContentFilterProfile | None:
    pid: Any = None
    if device.site_id:
        site = await db.get(Site, device.site_id)
        pid = site.content_filter_profile_id if site else None
    if pid is None:
        tenant = await db.get(Tenant, device.tenant_id)
        pid = (tenant.settings or {}).get("default_filter_profile") if tenant else None
    if not pid:
        return None
    prof = await db.get(ContentFilterProfile, uuid.UUID(str(pid)))
    return prof if prof and prof.nextdns_profile_id else None


def doh_url(profile: ContentFilterProfile, device: Device) -> str:
    return f"https://dns.nextdns.io/{profile.nextdns_profile_id}/{_SLUG.sub('-', device.name)[:40]}"


async def _ensure_ca(api: DeviceAPI, device: Device) -> str:
    try:
        await api.call("/certificate/settings/set", **{"builtin-trust-anchors": "trusted"})
        return "builtin"
    except RouterOSError:
        pass  # RouterOS < 7.19: CA-Bundle importieren
    if (device.facts or {}).get("doh_ca_imported"):
        return "imported"
    await api.call("/tool/fetch", url=CA_BUNDLE_URL, **{"dst-path": "sdwan-cacert.pem"})
    await api.call("/certificate/import", **{"file-name": "sdwan-cacert.pem", "passphrase": ""})
    device.facts = {**(device.facts or {}), "doh_ca_imported": True}
    return "imported"


async def apply_dns(db: AsyncSession, device: Device) -> dict[str, Any]:
    profile = await profile_for_device(db, device)
    site = await db.get(Site, device.site_id) if device.site_id else None
    async with connect_device(device) as api:
        if profile is None:
            return await remove_dns(api, device)
        dns = (await api.call("/ip/dns/print") or [{}])[0]
        facts = dict(device.facts or {})
        if "dns_backup" not in facts:
            facts["dns_backup"] = {"servers": str(dns.get("servers", "")), "use-doh-server": str(dns.get("use-doh-server", ""))}
        ca = await _ensure_ca(api, device)
        await api.sync_managed("/ip/dns/static", "dns:", [
            {"name": "dns.nextdns.io", "address": ip, "type": "A", "comment": f"sdwan:dns:boot{i}"}
            for i, ip in enumerate(NEXTDNS_BOOTSTRAP)
        ])
        url = doh_url(profile, device)
        await api.call("/ip/dns/set", **{"use-doh-server": url, "verify-doh-cert": "yes", "servers": "", "allow-remote-requests": "yes"})
        await api.call("/ip/dns/cache/flush")
        nat: list[dict[str, Any]] = []
        if profile.force_dns and site and site.lan_subnets:
            for i, net in enumerate(site.lan_subnets):
                for proto in ("udp", "tcp"):
                    nat.append({"chain": "dstnat", "src-address": net, "dst-address-type": "!local", "protocol": proto,
                                "dst-port": "53", "action": "redirect", "to-ports": "53", "comment": f"sdwan:dns:force:{i}:{proto}"})
        await api.sync_managed("/ip/firewall/nat", "dns:", nat, ordered=True, place_first=True)
    facts["dns_filter"] = {"profile_id": str(profile.id), "profile": profile.name, "doh": url, "ca": ca, "forced": bool(nat), "at": utcnow().isoformat()}
    device.facts = {**(device.facts or {}), **facts}
    return {"ok": True, "profile": profile.name, "doh": url, "forced": bool(nat)}


async def remove_dns(api: DeviceAPI, device: Device) -> dict[str, Any]:
    facts = dict(device.facts or {})
    if "dns_filter" not in facts:
        return {"ok": True, "profile": None}
    backup = facts.pop("dns_backup", {"servers": "", "use-doh-server": ""})
    await api.sync_managed("/ip/firewall/nat", "dns:", [], ordered=True)
    await api.call("/ip/dns/set", **{"use-doh-server": backup.get("use-doh-server", ""), "servers": backup.get("servers", "")})
    await api.sync_managed("/ip/dns/static", "dns:", [])
    await api.call("/ip/dns/cache/flush")
    facts.pop("dns_filter", None)
    device.facts = facts
    return {"ok": True, "profile": None, "removed": True}


async def apply_tenant(db: AsyncSession, tenant_id: uuid.UUID, device_ids: list[uuid.UUID] | None = None) -> dict[str, Any]:
    q = select(Device).where(Device.tenant_id == tenant_id, Device.pairing_status == "paired")
    if device_ids:
        q = q.where(Device.id.in_(device_ids))
    report: dict[str, Any] = {}
    for dev in (await db.execute(q)).scalars():
        try:
            report[str(dev.id)] = {"name": dev.name, **await apply_dns(db, dev)}
        except RouterOSError as exc:
            report[str(dev.id)] = {"name": dev.name, "ok": False, "error": str(exc)}
    await db.flush()
    return report
