"""Einmal-Tickets für den WebSocket (AUDIT-011).

Browser-WebSockets können keine Header setzen; bisher stand deshalb das JWT (12 h gültig) in der URL und damit in
Zugriffslogs. Jetzt holt der Client per ``POST /auth/ws-ticket`` (normal authentifiziert) ein Ticket: zufällig,
30 s gültig, genau einmal einlösbar. Gespeichert wird nur der sha256-Hash – mit Redis prozessübergreifend, sonst im Prozess.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.config import get_settings
from app.security import generate_token, hash_token

TTL_S = 30
_MEM: dict[str, tuple[float, dict[str, Any]]] = {}


def _key(ticket: str) -> str:
    return f"sdwan:wsticket:{hash_token(ticket)}"


async def issue(user_id: uuid.UUID, tenant: str | None, token_version: int) -> str:
    ticket = generate_token(24)
    data = {"user_id": str(user_id), "tenant": tenant, "tv": token_version}
    if get_settings().use_redis:
        try:
            from app.events import _get_redis

            await _get_redis().set(_key(ticket), json.dumps(data), ex=TTL_S)
            return ticket
        except Exception:  # noqa: BLE001 - Fallback im Prozess
            pass
    now = time.time()
    for k in [k for k, (exp, _d) in _MEM.items() if exp < now]:
        _MEM.pop(k, None)
    _MEM[_key(ticket)] = (now + TTL_S, data)
    return ticket


async def redeem(ticket: str) -> dict[str, Any] | None:
    """Ticket einlösen (danach ungültig). ``None`` = unbekannt, abgelaufen oder bereits benutzt."""
    if not ticket:
        return None
    key = _key(ticket)
    if get_settings().use_redis:
        try:
            from app.events import _get_redis

            raw = await _get_redis().getdel(key)
            if raw is not None:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    item = _MEM.pop(key, None)
    if item is None or item[0] < time.time():
        return None
    return item[1]
