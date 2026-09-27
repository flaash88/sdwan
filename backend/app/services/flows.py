"""Top-Verbraucher (Phase 25): IPFIX von den WAN-Interfaces an den Plattform-Collector, 5-Minuten-Aggregate.

* Opt-in je Gerät. Der Router exportiert per ``/ip/traffic-flow`` (Interfaces = WAN) an ``<Hub-IP>:FLOW_PORT``
  (``/ip/traffic-flow/target``, Kommentar ``sdwan:flow``, version=ipfix). Der Vorzustand von ``/ip/traffic-flow``
  wird gemerkt und beim Abschalten zurückgestellt.
* Der Collector (``app/flow_collector.py``, eigener Container im Netz-Namespace des Hubs) wertet IPFIX aus (eigener
  Parser, Templates je Quelle/Domain) und speichert **nur Aggregate**: je 5 min, WAN, lokaler Host, Gegenstelle –
  Bytes und Pakete. Keine Ports, keine Einzel-Flows. Je Gerät und Intervall höchstens ``TOP_PER_BUCKET`` Paare,
  der Rest wird als Gegenstelle ``andere`` zusammengefasst.
* Aufbewahrung je Mandant (``tenant.settings.flow_retention_days``, Default 7 Tage).

ANNAHME (Labor): Feldnamen von ``/ip/traffic-flow`` (``enabled``, ``interfaces``) und ``/ip/traffic-flow/target``
(``dst-address``, ``port``, ``version``); IPFIX-Informationselemente 1/2/8/12/10/14; Interface-Index im IPFIX
entspricht der Nummer in der ``.id`` von ``/interface``.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import struct
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceFlow, FlowAggregate, Tenant, WanLink
from app.routeros import connect_device
from app.routeros.client import DeviceAPI

log = logging.getLogger(__name__)

COMMENT = "sdwan:flow"
BUCKET_S = 300
TOP_PER_BUCKET = 200
DEFAULT_RETENTION_DAYS = 7
OTHER = "andere"
# IPFIX-Informationselemente
IE_OCTETS, IE_PACKETS, IE_SRC4, IE_DST4, IE_IN_IF, IE_OUT_IF, IE_SRC6, IE_DST6 = 1, 2, 8, 12, 10, 14, 27, 28


class FlowError(ValueError):
    pass


# ----------------------------------------------------------------------------- IPFIX-Parser
class IpfixParser:
    """Minimaler IPFIX-Parser (RFC 7011): Template-Sets (ID 2) und Daten-Sets (ID ≥ 256). Options-Templates (ID 3)
    werden übersprungen. Templates gelten je (Quelle, Observation Domain, Template-ID)."""

    def __init__(self) -> None:
        self.templates: dict[tuple[str, int, int], list[tuple[int, int]]] = {}

    def parse(self, src: str, data: bytes) -> list[dict[int, Any]]:
        if len(data) < 16:
            return []
        version, length, _export, _seq, domain = struct.unpack("!HHIII", data[:16])
        if version != 10:
            return []
        data = data[:length]
        records: list[dict[int, Any]] = []
        off = 16
        while off + 4 <= len(data):
            set_id, set_len = struct.unpack("!HH", data[off:off + 4])
            if set_len < 4:
                break
            body = data[off + 4:off + set_len]
            if set_id == 2:
                self._templates(src, domain, body)
            elif set_id >= 256:
                tpl = self.templates.get((src, domain, set_id))
                if tpl:
                    records += self._data(tpl, body)
            off += set_len
        return records

    def _templates(self, src: str, domain: int, body: bytes) -> None:
        off = 0
        while off + 4 <= len(body):
            tid, count = struct.unpack("!HH", body[off:off + 4])
            off += 4
            fields = []
            for _ in range(count):
                if off + 4 > len(body):
                    return
                ie, ln = struct.unpack("!HH", body[off:off + 4])
                off += 4
                if ie & 0x8000:  # Enterprise-Feld: 4 Byte Enterprise-Nummer überspringen
                    off += 4
                    ie = 0
                fields.append((ie, ln))
            if tid >= 256:
                self.templates[(src, domain, tid)] = fields

    @staticmethod
    def _data(tpl: list[tuple[int, int]], body: bytes) -> list[dict[int, Any]]:
        size = sum(ln for _, ln in tpl)
        if size == 0 or any(ln == 0xFFFF for _, ln in tpl):
            return []  # variable Länge wird nicht unterstützt
        out = []
        off = 0
        while off + size <= len(body):
            rec: dict[int, Any] = {}
            for ie, ln in tpl:
                raw = body[off:off + ln]
                off += ln
                if ie in (IE_SRC4, IE_DST4) and ln == 4:
                    rec[ie] = str(ipaddress.IPv4Address(raw))
                elif ie in (IE_SRC6, IE_DST6) and ln == 16:
                    rec[ie] = str(ipaddress.IPv6Address(raw))
                elif ie in (IE_OCTETS, IE_PACKETS, IE_IN_IF, IE_OUT_IF):
                    rec[ie] = int.from_bytes(raw, "big")
            out.append(rec)
        return out


def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_link_local or (a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"))


def orient(rec: dict[int, Any]) -> tuple[str, str, int | None] | None:
    """(lokaler Host, Gegenstelle, WAN-Interface-Index) – lokal ist die private Seite."""
    src, dst = rec.get(IE_SRC4) or rec.get(IE_SRC6), rec.get(IE_DST4) or rec.get(IE_DST6)
    if not src or not dst:
        return None
    if _private(src) and not _private(dst):
        return src, dst, rec.get(IE_OUT_IF)  # Upload: raus über das WAN
    if _private(dst) and not _private(src):
        return dst, src, rec.get(IE_IN_IF)  # Download: rein über das WAN
    return None  # intern↔intern oder öffentlich↔öffentlich (z. B. Router selbst) – nicht gezählt


class Aggregator:
    """Summen je (Gerät, 5-min-Bucket, WAN-Index, Host, Gegenstelle)."""

    def __init__(self) -> None:
        self.sums: dict[tuple[Any, dt.datetime, int | None, str, str], list[int]] = defaultdict(lambda: [0, 0])

    def add(self, device_id: Any, at: dt.datetime, records: list[dict[int, Any]]) -> None:
        bucket = at.replace(second=0, microsecond=0, minute=at.minute - at.minute % (BUCKET_S // 60))
        for r in records:
            o = orient(r)
            if o is None:
                continue
            s = self.sums[(device_id, bucket, o[2], o[0], o[1])]
            s[0] += int(r.get(IE_OCTETS) or 0)
            s[1] += int(r.get(IE_PACKETS) or 0)

    def take(self) -> dict[tuple[Any, dt.datetime, int | None, str, str], list[int]]:
        out, self.sums = self.sums, defaultdict(lambda: [0, 0])
        return out


class DeviceMap:
    """Quell-IP (Tunnel-IP) → (device_id, tenant_id, ifindex-Map) für Geräte mit aktivem Export."""

    def __init__(self) -> None:
        self.by_ip: dict[str, tuple[Any, Any, dict[str, str]]] = {}
        self.loaded_at: dt.datetime | None = None

    async def refresh(self, db: AsyncSession) -> None:
        flows = {f.device_id: f for f in (await db.execute(select(DeviceFlow).where(DeviceFlow.enabled.is_(True)))).scalars()}
        self.by_ip = {d.tunnel_ip: (d.id, d.tenant_id, flows[d.id].ifindex or {}) for d in (await db.execute(select(Device))).scalars()
                      if d.tunnel_ip and d.id in flows}
        self.loaded_at = utcnow()


async def store(db: AsyncSession, dmap: DeviceMap, sums: dict[tuple[Any, dt.datetime, int | None, str, str], list[int]]) -> int:
    """Aggregate speichern: je Gerät und Bucket die Top-Paare, Rest als ``andere``."""
    tenants = {v[0]: (v[1], v[2]) for v in dmap.by_ip.values()}
    groups: dict[tuple[Any, dt.datetime], list[tuple[int | None, str, str, list[int]]]] = defaultdict(list)
    for (dev, bucket, ifx, host, peer), v in sums.items():
        groups[(dev, bucket)].append((ifx, host, peer, v))
    n = 0
    for (dev, bucket), items in groups.items():
        if dev not in tenants:
            continue
        tenant_id, ifmap = tenants[dev]
        items.sort(key=lambda x: -x[3][0])
        rest: dict[tuple[int | None, str], list[int]] = defaultdict(lambda: [0, 0])
        for i, (ifx, host, peer, v) in enumerate(items):
            if i >= TOP_PER_BUCKET:
                r = rest[(ifx, host)]
                r[0] += v[0]
                r[1] += v[1]
                continue
            db.add(FlowAggregate(tenant_id=tenant_id, device_id=dev, bucket=bucket, wan=ifmap.get(str(ifx)) if ifx is not None else None,
                                 host=host, peer=peer, bytes=v[0], packets=v[1]))
            n += 1
        for (ifx, host), v in rest.items():
            db.add(FlowAggregate(tenant_id=tenant_id, device_id=dev, bucket=bucket, wan=ifmap.get(str(ifx)) if ifx is not None else None,
                                 host=host, peer=OTHER, bytes=v[0], packets=v[1]))
            n += 1
    return n


# ----------------------------------------------------------------------------- Router
async def wan_interfaces(db: AsyncSession, device: Device) -> list[str]:
    return sorted({lk.interface for lk in (await db.execute(select(WanLink).where(WanLink.device_id == device.id))).scalars() if lk.interface})


async def ifindex_map(api: DeviceAPI) -> dict[str, str]:
    """ANNAHME (Labor): IPFIX-Interface-Index = Hex-Nummer der ``.id`` von ``/interface``."""
    out = {}
    for r in await api.print("/interface"):
        rid = str(r.get(".id") or "")
        try:
            out[str(int(rid.lstrip("*"), 16))] = str(r.get("name"))
        except ValueError:
            continue
    return out


async def enable(db: AsyncSession, device: Device, fl: DeviceFlow, interfaces: list[str] | None = None) -> None:
    s = get_settings()
    ifaces = interfaces or await wan_interfaces(db, device)
    if not ifaces:
        raise FlowError("Keine WAN-Interfaces bekannt – zuerst WAN konfigurieren oder Interfaces angeben")
    async with connect_device(device) as api:
        cur = (await api.call("/ip/traffic-flow/print") or [{}])[0]
        if fl.before is None:
            fl.before = {"enabled": cur.get("enabled", "no"), "interfaces": cur.get("interfaces", "all")}
        await api.call("/ip/traffic-flow/set", enabled="yes", interfaces=",".join(ifaces))
        targets = [t for t in await api.print("/ip/traffic-flow/target") if str(t.get("comment", "")).startswith(COMMENT)]
        want = {"dst-address": s.wg_hub_ip, "port": str(s.flow_port), "version": "ipfix", "comment": COMMENT}
        if targets:
            await api.set("/ip/traffic-flow/target", targets[0][".id"], **{k: v for k, v in want.items() if k != "comment"})
        else:
            await api.add("/ip/traffic-flow/target", **want)
        fl.ifindex = await ifindex_map(api)
    fl.enabled, fl.interfaces, fl.status, fl.error, fl.updated_at = True, ifaces, "active", None, utcnow()


async def disable(device: Device, fl: DeviceFlow) -> None:
    async with connect_device(device) as api:
        for t in await api.print("/ip/traffic-flow/target"):
            if str(t.get("comment", "")).startswith(COMMENT):
                await api.remove("/ip/traffic-flow/target", t[".id"])
        if fl.before is not None:
            await api.call("/ip/traffic-flow/set", **fl.before)
    fl.enabled, fl.status, fl.before, fl.updated_at = False, "off", None, utcnow()


# ----------------------------------------------------------------------------- Auswertung
PERIODS = {"1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7)}


async def top(db: AsyncSession, device_id: Any, period: str = "24h", wan: str | None = None, limit: int = 20) -> dict[str, Any]:
    since = utcnow() - PERIODS.get(period, PERIODS["24h"])
    base = [FlowAggregate.device_id == device_id, FlowAggregate.bucket >= since]
    if wan:
        base.append(FlowAggregate.wan == wan)

    async def by(col: Any) -> list[dict[str, Any]]:
        q = (select(col, func.sum(FlowAggregate.bytes), func.sum(FlowAggregate.packets)).where(*base)
             .group_by(col).order_by(func.sum(FlowAggregate.bytes).desc()).limit(limit))
        return [{"address": a, "bytes": int(b or 0), "packets": int(p or 0)} for a, b, p in (await db.execute(q)).all()]

    wans = [w for (w,) in (await db.execute(select(FlowAggregate.wan).where(*base[:2]).distinct())).all() if w]
    total = (await db.execute(select(func.sum(FlowAggregate.bytes)).where(*base))).scalar() or 0
    return {"period": period, "wan": wan, "wans": sorted(wans), "total_bytes": int(total), "hosts": await by(FlowAggregate.host),
            "destinations": [d for d in await by(FlowAggregate.peer)]}


async def purge_old() -> int:
    n = 0
    async with system_session() as db:
        for t in (await db.execute(select(Tenant))).scalars():
            days = int((t.settings or {}).get("flow_retention_days") or DEFAULT_RETENTION_DAYS)
            res = await db.execute(delete(FlowAggregate).where(FlowAggregate.tenant_id == t.id,
                                                               FlowAggregate.bucket < utcnow() - dt.timedelta(days=days)))
            n += res.rowcount or 0
        await db.commit()
    return n
