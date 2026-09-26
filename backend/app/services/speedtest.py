"""Speedtest je WAN (Phase 18).

RouterOS 7 misst mit ``/tool bandwidth-test`` gegen einen btest-Server (RouterOS/CHR oder kompatibel). Der Hub der
Plattform ist ein Linux-Container und KEIN btest-Server – daher muss ``SPEEDTEST_SERVER`` konfiguriert sein
(z. B. ein CHR des MSP), sonst ist die Funktion deaktiviert.

Messung je WAN: vorübergehende /32-Route zum Server über das Gateway dieses WAN (Kommentar ``sdwan:speedtest:<slot>``),
danach wird sie immer entfernt; Reste früherer Läufe werden vor jedem Test aufgeräumt.
ANNAHME (Labor): Feldnamen ``rx-total-average``/``tx-total-average`` und Richtungen ``receive``/``transmit``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, PairingStatus, SpeedtestResult, WanLink
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI

log = logging.getLogger(__name__)
_RATE = re.compile(r"^([\d.]+)\s*([kKmMgG]?)bps$")
DEFAULT_RATE_MBPS = 100.0  # Annahme für die Volumenschätzung ohne früheres Ergebnis


class SpeedtestError(Exception):
    pass


def parse_rate(v: Any) -> float | None:
    """'94.5Mbps' -> 94.5 (Mbit/s)."""
    m = _RATE.match(str(v or "").strip())
    if not m:
        return None
    return float(m.group(1)) * {"": 1e-6, "k": 1e-3, "m": 1, "g": 1e3}[m.group(2).lower()]


def enabled() -> bool:
    return bool(get_settings().speedtest_server)


async def estimate(db: AsyncSession, link: WanLink) -> dict[str, Any]:
    """Erwarteter Datenverbrauch (beide Richtungen) und Warnung bei Volumenlimit."""
    s = get_settings()
    last = (await db.execute(select(SpeedtestResult).where(SpeedtestResult.wan_link_id == link.id, SpeedtestResult.status == "ok")
                             .order_by(SpeedtestResult.created_at.desc()).limit(1))).scalar_one_or_none()
    down = (last.down_mbps if last and last.down_mbps else DEFAULT_RATE_MBPS)
    up = (last.up_mbps if last and last.up_mbps else DEFAULT_RATE_MBPS)
    est = int((down + up) * 1e6 / 8 * s.speedtest_duration_s)
    warn = None
    if link.monthly_limit_gb:
        used = (link.vol_bytes or 0) / 1e9
        warn = (f"WAN {link.name} hat ein Volumenlimit von {link.monthly_limit_gb:g} GB (verbraucht {used:.1f} GB). "
                f"Der Test verbraucht ca. {est / 1e6:.0f} MB.")
    return {"bytes_estimated": est, "duration_s": s.speedtest_duration_s, "volume_warning": warn, "based_on_last": last is not None}


async def _gateway(api: DeviceAPI, link: WanLink) -> str:
    if link.gateway.lower() != "dhcp":
        return link.gateway
    for c in await api.print("/ip/dhcp-client"):
        if c.get("interface") == link.interface and c.get("gateway"):
            return str(c["gateway"])
    raise SpeedtestError(f"Kein DHCP-Gateway auf {link.interface}")


async def _cleanup(api: DeviceAPI) -> int:
    n = 0
    for r in await api.print("/ip/route"):
        if str(r.get("comment", "")).startswith("sdwan:speedtest"):
            await api.remove("/ip/route", r[".id"])
            n += 1
    return n


async def run(db: AsyncSession, device: Device, link: WanLink, by: str | None) -> SpeedtestResult:
    s = get_settings()
    if not enabled():
        raise SpeedtestError("Kein Speedtest-Server konfiguriert (SPEEDTEST_SERVER)")
    est = await estimate(db, link)
    res = SpeedtestResult(tenant_id=device.tenant_id, device_id=device.id, wan_link_id=link.id, slot=link.slot, status="running",
                          duration_s=s.speedtest_duration_s, bytes_estimated=est["bytes_estimated"], triggered_by=by)
    db.add(res)
    await db.flush()
    server = s.speedtest_server
    try:
        import ipaddress

        ipaddress.ip_address(server)
    except ValueError as exc:
        raise SpeedtestError("SPEEDTEST_SERVER muss eine IP-Adresse sein (für die Route über das gewählte WAN)") from exc
    auth = {"user": s.speedtest_user, "password": s.speedtest_password} if s.speedtest_user else {}
    try:
        async with connect_device(device) as api:
            await _cleanup(api)
            gw = await _gateway(api, link)
            await api.add("/ip/route", **{"dst-address": f"{server}/32", "gateway": gw, "distance": "1", "comment": f"sdwan:speedtest:{link.slot}"})
            try:
                rates = {}
                for direction, key in (("receive", "rx-total-average"), ("transmit", "tx-total-average")):
                    rows = await api.call("/tool/bandwidth-test", address=server, direction=direction, protocol="tcp",
                                          duration=f"{s.speedtest_duration_s}s", **auth)
                    last = rows[-1] if rows else {}
                    if str(last.get("status", "")).lower() not in ("done testing", "running", ""):
                        raise SpeedtestError(f"bandwidth-test: {last.get('status')}")
                    rates[direction] = parse_rate(last.get(key))
                res.down_mbps, res.up_mbps = rates.get("receive"), rates.get("transmit")
                res.status = "ok" if res.down_mbps is not None else "failed"
                if res.status == "failed":
                    res.error = "Keine Messwerte vom Router erhalten"
            finally:
                await _cleanup(api)
    except (RouterOSError, SpeedtestError) as exc:
        res.status, res.error = "failed", str(exc)[:500]
    res.finished_at = utcnow()
    return res


async def speedtest_tick() -> None:
    """Worker-Job (täglich): geplante Tests (opt-in je WAN, wöchentlich)."""
    if not enabled():
        return
    now = utcnow()
    async with system_session() as db:
        links = (await db.execute(select(WanLink).where(WanLink.speedtest_weekly.is_(True), WanLink.enabled.is_(True)))).scalars().all()
        for link in links:
            last = (await db.execute(select(SpeedtestResult).where(SpeedtestResult.wan_link_id == link.id)
                                     .order_by(SpeedtestResult.created_at.desc()).limit(1))).scalar_one_or_none()
            if last and (now - last.created_at).days < 7:
                continue
            dev = await db.get(Device, link.device_id)
            if dev and dev.pairing_status == PairingStatus.paired and dev.status == DeviceStatus.online:
                await run(db, dev, link, "geplant")
                await db.commit()
