"""Event-Bus für Live-Daten (Worker -> API -> WebSocket-Clients).

Produktion: Redis Pub/Sub (Worker und API sind getrennte Prozesse).
Tests/Single-Process: In-Memory-Fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)
CHANNEL_PREFIX = "sdwan:events:"


class _MemoryBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    async def publish(self, channel: str, msg: str) -> None:
        for q in list(self._subs):
            q.put_nowait((channel, msg))

    async def listen(self) -> AsyncIterator[tuple[str, str]]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)


_memory = _MemoryBus()
_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def publish(tenant_id: uuid.UUID | str | None, event_type: str, data: dict[str, Any]) -> None:
    channel = CHANNEL_PREFIX + (str(tenant_id) if tenant_id else "system")
    msg = json.dumps({"type": event_type, "tenant_id": str(tenant_id) if tenant_id else None, "data": data}, default=str)
    if get_settings().use_redis:
        try:
            await _get_redis().publish(channel, msg)
            return
        except Exception as exc:  # pragma: no cover - nur bei Redis-Ausfall
            log.warning("Redis publish failed (%s), fallback to memory", exc)
    await _memory.publish(channel, msg)


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Liefert alle Events (Filterung nach Tenant erfolgt im WebSocket-Handler)."""
    if get_settings().use_redis:
        pubsub = _get_redis().pubsub()
        await pubsub.psubscribe(CHANNEL_PREFIX + "*")
        try:
            async for m in pubsub.listen():
                if m.get("type") == "pmessage":
                    yield json.loads(m["data"])
        finally:
            await pubsub.aclose()
    else:
        async for _channel, msg in _memory.listen():
            yield json.loads(msg)
