"""Metriken (Phase 4): Erfassung per Poll-Hook, Speicherung in InfluxDB, Live-Events.

Measurements (Tags: tenant_id, tenant, site_id, site, device_id, device):
* ``system``    cpu_load, mem_used, mem_total, uptime_s, mgmt_rtt_ms
* ``interface`` rx_bps, tx_bps, rx_bytes, tx_bytes, running   (+ Tag ``interface``)
* ``wan``       up, active, rtt_ms, loss_pct                    (+ Tags ``wan``, ``slot``)
* ``mesh``      up, rx_bytes, tx_bytes                          (+ Tag ``peer``)
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import deque
from typing import Any

from app import events
from app.config import get_settings
from app.models import Device
from app.routeros.client import DeviceAPI
from app.routeros.util import parse_duration

log = logging.getLogger(__name__)

MEASUREMENTS = ("system", "interface", "wan", "mesh")
RANGES = {"15m": ("-15m", "30s"), "1h": ("-1h", "1m"), "6h": ("-6h", "5m"), "24h": ("-24h", "15m"), "7d": ("-7d", "1h"), "30d": ("-30d", "6h")}


# ----------------------------------------------------------------------------- Sinks
class MemorySink:
    """Test-/Fallback-Sink: hält die letzten Punkte im Speicher."""

    def __init__(self) -> None:
        self.points: deque[dict[str, Any]] = deque(maxlen=20000)

    async def write(self, points: list[dict[str, Any]]) -> None:
        self.points.extend(points)

    async def query(self, measurement: str, device_id: uuid.UUID, rng: str) -> list[dict[str, Any]]:
        start = time.time() - (parse_duration(RANGES[rng][0].lstrip("-")) or 3600)
        out = []
        for p in self.points:
            if p["measurement"] == measurement and p["tags"].get("device_id") == str(device_id) and p["time"] >= start:
                out.append({"time": p["time"], **{k: v for k, v in p["tags"].items() if k in ("interface", "wan", "peer")}, **p["fields"]})
        return out


class InfluxSink:
    def __init__(self) -> None:
        from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

        s = get_settings()
        self.bucket, self.org = s.influx_bucket, s.influx_org
        self.client = InfluxDBClientAsync(url=s.influx_url, token=s.influx_token, org=s.influx_org, timeout=10_000)

    async def write(self, points: list[dict[str, Any]]) -> None:
        from influxdb_client import Point, WritePrecision

        recs = []
        for p in points:
            pt = Point(p["measurement"]).time(int(p["time"]), WritePrecision.S)
            for k, v in p["tags"].items():
                if v is not None:
                    pt = pt.tag(k, str(v))
            for k, v in p["fields"].items():
                if v is not None:
                    pt = pt.field(k, float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
            recs.append(pt)
        try:
            await self.client.write_api().write(bucket=self.bucket, record=recs)
        except Exception as exc:  # noqa: BLE001 - Metriken dürfen Polling nie blockieren
            log.warning("Influx-Write fehlgeschlagen: %s", exc)

    async def query(self, measurement: str, device_id: uuid.UUID, rng: str) -> list[dict[str, Any]]:
        start, every = RANGES[rng]
        # measurement & device_id sind validiert (Whitelist/UUID) -> keine Flux-Injection möglich
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.device_id == "{device_id}")
  |> filter(fn: (r) => r._field != "running")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
        tables = await self.client.query_api().query(flux, org=self.org)
        out = []
        for table in tables:
            for rec in table.records:
                v = rec.values
                row = {"time": rec.get_time().timestamp()}
                for k in ("interface", "wan", "peer"):
                    if k in v:
                        row[k] = v[k]
                row.update({k: v[k] for k in v if not k.startswith("_") and k not in ("result", "table", "tenant_id", "tenant", "site_id", "site", "device_id", "device", "interface", "wan", "peer", "slot")})
                out.append(row)
        out.sort(key=lambda r: r["time"])
        return out


_sink: MemorySink | InfluxSink | None = None


def get_sink() -> MemorySink | InfluxSink:
    global _sink
    if _sink is None:
        _sink = InfluxSink() if get_settings().influx_enabled else MemorySink()
    return _sink


def reset_sink() -> None:
    global _sink
    _sink = None


# ----------------------------------------------------------------------------- Erfassung
_IGNORE_IFACE = re.compile(r"^(lo|<.*>)$")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def collect(device: Device, api: DeviceAPI, res: dict[str, Any]) -> dict[str, Any]:
    """Liest Interface-Zähler und berechnet Raten aus der Differenz zum letzten Poll."""
    now = time.time()
    prev = (device.facts or {}).get("_counters") or {}
    ifaces: dict[str, Any] = {}
    counters: dict[str, list[float]] = {}
    for row in await api.print("/interface"):
        name = str(row.get("name", ""))
        if not name or _IGNORE_IFACE.match(name):
            continue
        rx, tx = _num(row.get("rx-byte")), _num(row.get("tx-byte"))
        if rx is None or tx is None:
            continue
        counters[name] = [rx, tx, now]
        rx_bps = tx_bps = None
        if name in prev:
            prx, ptx, pts = prev[name]
            dt_ = now - pts
            if dt_ > 0 and rx >= prx and tx >= ptx:  # Zähler-Reset (Reboot) ignorieren
                rx_bps, tx_bps = (rx - prx) * 8 / dt_, (tx - ptx) * 8 / dt_
        ifaces[name] = {
            "rx_bps": rx_bps, "tx_bps": tx_bps, "rx_bytes": rx, "tx_bytes": tx,
            "running": str(row.get("running", "")).lower() in ("true", "yes"),
            "type": row.get("type"),
            "comment": str(row.get("comment") or "") or None,
            "default_name": str(row.get("default-name") or "") or None,
        }
    return {"_counters": counters, "interfaces": ifaces}


def _tags(device: Device, tenant_name: str | None, site_name: str | None) -> dict[str, Any]:
    return {
        "tenant_id": str(device.tenant_id), "tenant": tenant_name, "site_id": str(device.site_id) if device.site_id else "none",
        "site": site_name or "none", "device_id": str(device.id), "device": device.name,
    }


def build_points(device: Device, tenant_name: str | None, site_name: str | None, ts: float) -> list[dict[str, Any]]:
    f = device.facts or {}
    tags = _tags(device, tenant_name, site_name)
    mem_total, mem_free = _num(f.get("total_memory")), _num(f.get("free_memory"))
    points: list[dict[str, Any]] = [{
        "measurement": "system", "tags": tags, "time": ts,
        "fields": {
            "cpu_load": _num(f.get("cpu_load")),
            "mem_total": mem_total,
            "mem_used": (mem_total - mem_free) if mem_total is not None and mem_free is not None else None,
            "uptime_s": parse_duration(device.uptime),
            "mgmt_rtt_ms": _num(f.get("mgmt_rtt_ms")),
        },
    }]
    for name, i in (f.get("interfaces") or {}).items():
        points.append({"measurement": "interface", "tags": {**tags, "interface": name}, "time": ts,
                       "fields": {k: i.get(k) for k in ("rx_bps", "tx_bps", "rx_bytes", "tx_bytes")} | {"running": 1 if i.get("running") else 0}})
    for slot, w in (f.get("wan") or {}).items():
        points.append({"measurement": "wan", "tags": {**tags, "slot": slot, "wan": f"WAN{slot}"}, "time": ts,
                       "fields": {"up": 1 if w.get("status") == "up" else 0, "active": 1 if w.get("active") else 0,
                                  "rtt_ms": w.get("rtt_ms"), "loss_pct": w.get("loss_pct")}})
    for peer, m in (f.get("mesh_peers") or {}).items():
        hs = m.get("handshake_s")
        points.append({"measurement": "mesh", "tags": {**tags, "peer": peer}, "time": ts,
                       "fields": {"up": 1 if hs is not None and hs <= 180 else 0, "rx_bytes": m.get("rx"), "tx_bytes": m.get("tx")}})
    for p in points:
        p["fields"] = {k: v for k, v in p["fields"].items() if v is not None}
    return [p for p in points if p["fields"]]


def live_payload(device: Device) -> dict[str, Any]:
    f = device.facts or {}
    ifaces = f.get("interfaces") or {}
    return {
        "id": str(device.id),
        "cpu_load": f.get("cpu_load"),
        "mem_used": (f.get("total_memory") or 0) - (f.get("free_memory") or 0) if f.get("total_memory") else None,
        "mem_total": f.get("total_memory"),
        "mgmt_rtt_ms": f.get("mgmt_rtt_ms"),
        "uptime": device.uptime,
        "rx_bps": sum(i.get("rx_bps") or 0 for n, i in ifaces.items() if i.get("type") in ("ether", "pppoe-out", "lte", "vlan", None) and not n.startswith("sdwan-")),
        "tx_bps": sum(i.get("tx_bps") or 0 for n, i in ifaces.items() if i.get("type") in ("ether", "pppoe-out", "lte", "vlan", None) and not n.startswith("sdwan-")),
        "interfaces": {n: {k: i.get(k) for k in ("rx_bps", "tx_bps", "running", "comment", "default_name")} for n, i in ifaces.items()},
        "wan": f.get("wan") or {},
    }


async def store_metrics(db: Any, devices: list[Device]) -> None:
    """Post-Poll: Punkte schreiben + Live-Event pro Gerät."""
    from sqlalchemy import select

    from app.models import DeviceStatus, Site, Tenant

    online = [d for d in devices if d.status == DeviceStatus.online]
    if not online:
        return
    tenants = {t.id: t.name for t in (await db.execute(select(Tenant))).scalars()}
    sites = {s.id: s.name for s in (await db.execute(select(Site))).scalars()}
    ts = time.time()
    points: list[dict[str, Any]] = []
    for d in online:
        points += build_points(d, tenants.get(d.tenant_id), sites.get(d.site_id) if d.site_id else None, ts)
        await events.publish(d.tenant_id, "device.metrics", live_payload(d))
    await get_sink().write(points)


# ----------------------------------------------------------------------------- Live-Modus
_live_memory: dict[str, float] = {}
LIVE_TTL = 90


async def request_live(device_id: uuid.UUID) -> None:
    """Ein Client beobachtet das Gerät -> schnelles Polling für LIVE_TTL Sekunden."""
    if get_settings().use_redis:
        try:
            await events._get_redis().set(f"sdwan:live:{device_id}", "1", ex=LIVE_TTL)
            return
        except Exception:  # noqa: BLE001
            pass
    _live_memory[str(device_id)] = time.time() + LIVE_TTL


async def live_device_ids() -> set[str]:
    if get_settings().use_redis:
        try:
            r = events._get_redis()
            return {k.rsplit(":", 1)[1] async for k in r.scan_iter("sdwan:live:*")}
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    return {k for k, v in _live_memory.items() if v > now}
