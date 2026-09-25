"""Erzeugt die provisionierten Grafana-Dashboards (Flux/InfluxDB).

    python deploy/grafana/generate_dashboards.py
"""

import json
from pathlib import Path

DS = {"type": "influxdb", "uid": "sdwan-influx"}
OUT = Path(__file__).parent / "dashboards"


def var(name, label, tag, flt=""):
    q = f'import "influxdata/influxdb/schema"\nschema.tagValues(bucket: v.defaultBucket, tag: "{tag}"{flt})'
    return {"name": name, "label": label, "type": "query", "datasource": DS, "query": q, "refresh": 2,
            "includeAll": name != "device", "multi": name != "device", "current": {}, "sort": 1}


def flt(extra: str) -> str:
    return (
        '  |> filter(fn: (r) => contains(value: r.tenant_id, set: ${tenant:json}))\n'
        + extra
    )


def ts_panel(pid, title, flux, unit, x, y, w=12, h=8, stack=False):
    return {
        "id": pid, "type": "timeseries", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 15, "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
        "targets": [{"refId": "A", "datasource": DS, "query": flux}],
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]}},
    }


def stat_panel(pid, title, flux, unit, x, y, w=6, h=4, thresholds=None):
    return {
        "id": pid, "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": []},
        "targets": [{"refId": "A", "datasource": DS, "query": flux}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
    }


def q(measurement, field, scope, group, fn="mean"):
    return (
        f'from(bucket: v.defaultBucket)\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")\n'
        f"{scope}"
        f'  |> group(columns: {json.dumps(group)})\n'
        f'  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)'
    )


def dashboard(uid, title, variables, panels):
    return {
        "uid": uid, "title": title, "tags": ["sdwan"], "timezone": "browser", "schemaVersion": 39, "version": 1,
        "refresh": "1m", "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": variables}, "panels": panels,
    }


tenant_var = var("tenant", "Mandant (ID)", "tenant_id")
site_var = var("site", "Standort", "site_id", ', predicate: (r) => contains(value: r.tenant_id, set: ${tenant:json})')
device_var = var("device", "Gerät", "device_id", ', predicate: (r) => contains(value: r.tenant_id, set: ${tenant:json})')

scope_t = flt("")
scope_s = flt('  |> filter(fn: (r) => contains(value: r.site_id, set: ${site:json}))\n')
scope_d = '  |> filter(fn: (r) => r.device_id == "${device}")\n'

fleet = dashboard("sdwan-tenant", "MikroTik-Flotte · Mandant", [tenant_var], [
    stat_panel(1, "Geräte mit Daten", q("system", "cpu_load", scope_t, []).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", 'last() |> group() |> count()'), "none", 0, 0),
    stat_panel(2, "WAN-Links down", q("wan", "up", scope_t, ["device", "wan"]).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", 'last() |> filter(fn: (r) => r._value == 0) |> group() |> count()'), "none", 6, 0,
               {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]}),
    stat_panel(3, "Mesh-Tunnel down", q("mesh", "up", scope_t, ["device", "peer"]).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", 'last() |> filter(fn: (r) => r._value == 0) |> group() |> count()'), "none", 12, 0,
               {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]}),
    stat_panel(4, "Ø CPU", q("system", "cpu_load", scope_t, []).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", "mean()"), "percent", 18, 0),
    ts_panel(5, "CPU je Gerät", q("system", "cpu_load", scope_t, ["device"]), "percent", 0, 4),
    ts_panel(6, "Management-Latenz je Gerät", q("system", "mgmt_rtt_ms", scope_t, ["device"]), "ms", 12, 4),
    ts_panel(7, "WAN-Latenz", q("wan", "rtt_ms", scope_t, ["device", "wan"]), "ms", 0, 12),
    ts_panel(8, "Durchsatz RX je Standort", q("interface", "rx_bps", scope_t + '  |> filter(fn: (r) => r.interface !~ /^sdwan-/)\n', ["site"], "sum"), "bps", 12, 12, stack=True),
])

site = dashboard("sdwan-site", "MikroTik-Flotte · Standort", [tenant_var, site_var], [
    ts_panel(1, "CPU", q("system", "cpu_load", scope_s, ["device"]), "percent", 0, 0),
    ts_panel(2, "Speicher belegt", q("system", "mem_used", scope_s, ["device"]), "bytes", 12, 0),
    ts_panel(3, "WAN-Latenz", q("wan", "rtt_ms", scope_s, ["device", "wan"]), "ms", 0, 8),
    ts_panel(4, "WAN-Paketverlust", q("wan", "loss_pct", scope_s, ["device", "wan"]), "percent", 12, 8),
    ts_panel(5, "Durchsatz RX", q("interface", "rx_bps", scope_s, ["device", "interface"]), "bps", 0, 16),
    ts_panel(6, "Durchsatz TX", q("interface", "tx_bps", scope_s, ["device", "interface"]), "bps", 12, 16),
])

device = dashboard("sdwan-device", "MikroTik-Flotte · Gerät", [tenant_var, device_var], [
    stat_panel(1, "CPU", q("system", "cpu_load", scope_d, []).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", "last()"), "percent", 0, 0,
               {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 70}, {"color": "red", "value": 90}]}),
    stat_panel(2, "Management-Latenz", q("system", "mgmt_rtt_ms", scope_d, []).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", "last()"), "ms", 6, 0),
    stat_panel(3, "Uptime", q("system", "uptime_s", scope_d, []).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", "last()"), "s", 12, 0),
    stat_panel(4, "Aktive WANs", q("wan", "up", scope_d, ["wan"]).replace("aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)", "last() |> group() |> sum()"), "none", 18, 0),
    ts_panel(5, "CPU", q("system", "cpu_load", scope_d, []), "percent", 0, 4),
    ts_panel(6, "Speicher", q("system", "mem_used", scope_d, []), "bytes", 12, 4),
    ts_panel(7, "Interface RX", q("interface", "rx_bps", scope_d, ["interface"]), "bps", 0, 12),
    ts_panel(8, "Interface TX", q("interface", "tx_bps", scope_d, ["interface"]), "bps", 12, 12),
    ts_panel(9, "WAN-Latenz", q("wan", "rtt_ms", scope_d, ["wan"]), "ms", 0, 20),
    ts_panel(10, "WAN-Verfügbarkeit (1 = up)", q("wan", "up", scope_d, ["wan"], "min"), "none", 12, 20),
])

OUT.mkdir(exist_ok=True)
for d in (fleet, site, device):
    (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    print("wrote", d["uid"])
