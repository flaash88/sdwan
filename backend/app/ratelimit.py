"""Einfaches Fenster-Rate-Limit: Redis (INCR + EXPIRE), ohne Redis im Prozess (wie beim Hotspot-Endpunkt)."""

from __future__ import annotations

import time

from app.config import get_settings

_MEM: dict[str, list[float]] = {}


async def hit(key: str, limit: int, window_s: int) -> bool:
    """Zählt einen Versuch; True, wenn das Limit überschritten ist."""
    if get_settings().use_redis:
        try:
            from app.events import _get_redis

            r = _get_redis()
            n = await r.incr(f"sdwan:rl:{key}")
            if n == 1:
                await r.expire(f"sdwan:rl:{key}", window_s)
            return n > limit
        except Exception:  # noqa: BLE001 - Fallback im Prozess
            pass
    now = time.time()
    hits = [t for t in _MEM.get(key, []) if now - t < window_s]
    hits.append(now)
    _MEM[key] = hits
    return len(hits) > limit


async def over(key: str, limit: int, window_s: int) -> bool:
    """Nur prüfen (ohne zu zählen), ob das Limit bereits überschritten ist."""
    if get_settings().use_redis:
        try:
            from app.events import _get_redis

            v = await _get_redis().get(f"sdwan:rl:{key}")
            return int(v or 0) >= limit
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    return len([t for t in _MEM.get(key, []) if now - t < window_s]) >= limit


def reset() -> None:
    _MEM.clear()
