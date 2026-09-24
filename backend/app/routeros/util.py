"""Hilfsfunktionen für RouterOS-Werte."""

from __future__ import annotations

import re

_DUR = re.compile(r"(\d+)(w|d|h|ms|m|s)")
_MULT = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def parse_duration(value: object) -> float | None:
    """'1w2d3h4m5s', '45s', '850ms', '00:01:23' -> Sekunden. None bei leer/unbekannt."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip()
    if not v:
        return None
    if ":" in v:
        days = 0.0
        if "d" in v:  # z. B. '2d03:04:05'
            d, v = v.split("d", 1)
            days = float(d) * 86400
        parts = [float(p) for p in v.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return days + parts[0] * 3600 + parts[1] * 60 + parts[2]
    matches = _DUR.findall(v)
    if not matches:
        return None
    return sum(float(n) * _MULT[u] for n, u in matches)


def parse_ms(value: object) -> float | None:
    """Latenz '12.3ms' / '1s200ms' / '850us' -> Millisekunden."""
    if value is None:
        return None
    v = str(value).strip()
    if v.endswith("us") and v[:-2].replace(".", "").isdigit():
        return float(v[:-2]) / 1000
    if v.endswith("ms") and v[:-2].replace(".", "", 1).isdigit():
        return float(v[:-2])
    secs = parse_duration(v)
    return secs * 1000 if secs is not None else None


def parse_rate(value: object) -> int:
    """'12.5Mbps' / '800kbps' / 1234 -> bit/s."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"^([\d.]+)\s*([kMG]?)(bps)?$", str(value).strip())
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)])
