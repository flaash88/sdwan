"""Übersicht: IP-Adressen je Interface und Sensorwerte aus /system/health.

Sensorwerte gibt es nur, wenn das Modell sie liefert (CHR/x86 und manche Boards liefern nichts) –
die Oberfläche blendet die Kacheln dann aus. Es gibt bewusst keine Alarmregel darauf.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from app.models import Device
from app.routeros.client import DeviceAPI, RouterOSError

_NUM = re.compile(r"^-?\d+(?:\.\d+)?")
# Einheiten der flachen Darstellung (ältere Firmware liefert eine Zeile mit Feldern statt name/value/type)
_FLAT_UNITS = {"temperature": "C", "voltage": "V", "current": "A", "power-consumption": "W"}


def _flag(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def parse_health(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """RouterOS 7 (``name``/``value``/``type``) und flaches Format → [{name, value, unit}], nur Zahlenwerte."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if "name" in r and "value" in r:
            items = [(str(r["name"]), r.get("value"), str(r.get("type") or ""))]
        else:
            items = [(k, v, next((u for s, u in _FLAT_UNITS.items() if k.endswith(s)), "")) for k, v in r.items() if not k.startswith(".")]
        for name, value, unit in items:
            m = _NUM.match(str(value or "").strip())
            unit = unit.replace("°", "")
            if m and unit in ("C", "V", "A", "W"):  # Lüfter (RPM) und Zustände (ok/fail) bleiben außen vor
                out.append({"name": name, "value": float(m.group()), "unit": unit})
    return out


def pick_health(sensors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Wert für die Kacheln: Temperatur (CPU bevorzugt) und Spannung."""
    def first(pred) -> dict[str, Any] | None:
        return next((s for s in sensors if pred(s)), None)

    temp = first(lambda s: s["name"] == "cpu-temperature") or first(lambda s: s["name"] == "temperature") \
        or first(lambda s: s["unit"] == "C" and "temperature" in s["name"])
    volt = first(lambda s: s["name"] == "voltage") or first(lambda s: s["unit"] == "V")
    return {k: v for k, v in (("temperature", temp), ("voltage", volt)) if v}


def build_addresses(addr_rows: list[dict[str, Any]], dhcp_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dhcp_ifaces = {str(d.get("interface")) for d in dhcp_rows if not _flag(d.get("disabled"))}
    out = []
    for a in addr_rows:
        iface = str(a.get("interface", ""))
        dynamic = _flag(a.get("dynamic"))
        comment = str(a.get("comment") or "") or None
        network = a.get("network")
        if not network:
            try:
                network = str(ipaddress.ip_interface(str(a.get("address", ""))).network.network_address)
            except ValueError:
                network = None
        out.append({
            "address": str(a.get("address", "")), "network": network, "interface": iface,
            "dynamic": dynamic, "dhcp": dynamic and iface in dhcp_ifaces,
            "disabled": _flag(a.get("disabled")), "invalid": _flag(a.get("invalid")),
            "comment": comment, "managed": bool(comment and comment.startswith("sdwan:")),
        })
    return sorted(out, key=lambda x: (x["interface"], x["address"]))


async def read_addresses(api: DeviceAPI) -> list[dict[str, Any]]:
    return build_addresses(await api.print("/ip/address"), await api.print("/ip/dhcp-client"))


async def info_poll_hook(device: Device, api: DeviceAPI, _res: dict[str, Any]) -> dict[str, Any] | None:
    out: dict[str, Any] = {"addresses": await read_addresses(api)}
    try:
        out["health"] = parse_health(await api.call("/system/health/print"))
    except RouterOSError:  # manche Plattformen kennen den Befehl nicht
        out["health"] = []
    return out
