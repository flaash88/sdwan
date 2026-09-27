"""Sperren mit Ablauf (AUDIT-014/016/043): je Gerät, je Deployment, Worker-Leader.

* Mit Redis (``USE_REDIS``): ``SET key owner NX PX ttl`` – prozessübergreifend (API und Worker). Freigabe und Verlängerung
  nur durch den Eigentümer (Lua-Vergleich). Ohne Redis: Sperre im Prozess (Tests, Einzelprozess).
* ``hold()`` verlängert die Sperre im Hintergrund alle ``ttl/3`` – auch lange Vorgänge verlieren sie nicht. Stirbt der
  Prozess, läuft sie nach ``ttl`` ab.
* Reentrant im selben asyncio-Kontext: Wer die Sperre schon hält (z. B. Deployment → ZTP-Provisionierung), bekommt sie
  sofort erneut, ohne sich selbst zu blockieren.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
import uuid
from collections.abc import AsyncIterator

from app.config import get_settings

log = logging.getLogger(__name__)

PREFIX = "sdwan:lock:"
_held: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("sdwan_locks_held", default=frozenset())
_MEM: dict[str, tuple[str, float]] = {}  # key -> (owner, Ablauf monotonic)

_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_EXTEND = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"


def device_key(device_id: uuid.UUID | str) -> str:
    return f"device:{device_id}"


def _redis():
    if not get_settings().use_redis:
        return None
    try:
        from app.events import _get_redis

        return _get_redis()
    except Exception:  # noqa: BLE001
        return None


async def _try(key: str, owner: str, ttl: float) -> bool:
    r = _redis()
    if r is not None:
        try:
            return bool(await r.set(PREFIX + key, owner, nx=True, px=int(ttl * 1000)))
        except Exception as exc:  # noqa: BLE001 - Redis weg: im Prozess weiter
            log.warning("Redis-Sperre %s nicht möglich (%s) – Sperre nur im Prozess", key, exc)
    now = time.monotonic()
    cur = _MEM.get(key)
    if cur is not None and cur[1] > now and cur[0] != owner:
        return False
    _MEM[key] = (owner, now + ttl)
    return True


async def _release(key: str, owner: str) -> None:
    r = _redis()
    if r is not None:
        with contextlib.suppress(Exception):
            await r.eval(_RELEASE, 1, PREFIX + key, owner)
    cur = _MEM.get(key)
    if cur is not None and cur[0] == owner:
        _MEM.pop(key, None)


async def _extend(key: str, owner: str, ttl: float) -> None:
    r = _redis()
    if r is not None:
        with contextlib.suppress(Exception):
            await r.eval(_EXTEND, 1, PREFIX + key, owner, int(ttl * 1000))
    cur = _MEM.get(key)
    if cur is not None and cur[0] == owner:
        _MEM[key] = (owner, time.monotonic() + ttl)


async def owner_of(key: str) -> str | None:
    """Aktueller Eigentümer ``<zweck>#<zufall>`` (für Anzeige/Erkennung hängender Vorgänge)."""
    r = _redis()
    if r is not None:
        try:
            v = await r.get(PREFIX + key)
            return v.decode() if isinstance(v, bytes) else v
        except Exception:  # noqa: BLE001
            pass
    cur = _MEM.get(key)
    return cur[0] if cur is not None and cur[1] > time.monotonic() else None


def held(key: str) -> bool:
    return key in _held.get()


@contextlib.asynccontextmanager
async def hold(key: str, owner: str, *, ttl: float = 120, wait: float = 0) -> AsyncIterator[bool]:
    """Sperre ``key`` halten. Liefert ``True`` (gehalten) oder ``False`` (nach ``wait`` Sekunden nicht bekommen) –
    der Aufrufer entscheidet, was dann passiert (überspringen, 409, Fehlermeldung)."""
    if held(key):
        yield True
        return
    owner = f"{owner}#{uuid.uuid4().hex[:8]}"  # eindeutig je Aufruf (gleicher Zweck ≠ gleicher Halter)
    deadline = time.monotonic() + wait
    got = await _try(key, owner, ttl)
    while not got and time.monotonic() < deadline:
        await asyncio.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        got = await _try(key, owner, ttl)
    if not got:
        yield False
        return

    async def renew() -> None:
        while True:
            await asyncio.sleep(ttl / 3)
            await _extend(key, owner, ttl)

    renewer = asyncio.create_task(renew())
    token = _held.set(_held.get() | {key})
    try:
        yield True
    finally:
        _held.reset(token)
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer
        await _release(key, owner)


def device(device_id: uuid.UUID | str, purpose: str, *, ttl: float = 120, wait: float = 0):
    """Sperre je Gerät für Router-ändernde Vorgänge (Deploy, Post-Poll-Hooks, Firmware, Scripts, Offboarding)."""
    return hold(device_key(device_id), purpose, ttl=ttl, wait=wait)


def reset() -> None:
    _MEM.clear()
