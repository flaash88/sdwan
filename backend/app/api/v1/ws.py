"""WebSocket für Live-Daten.

Anmeldung per Einmal-Ticket ``?ticket=…`` (``POST /auth/ws-ticket``, 30 s gültig, einmalig – AUDIT-011). Übergangsweise wird
noch ``?token=<jwt>&tenant=<uuid>`` angenommen (ältere Oberflächen im Browser-Cache); entfällt in einem späteren Release.
Die Berechtigung wird alle ``RECHECK_S`` Sekunden erneut geprüft (Benutzer aktiv, Token-Version, Mandant aktiv) – sonst
schließt der Server mit 4401.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app import events, ws_tickets
from app.db import sessionmaker
from app.deps import _user_from_token, resolve_ctx
from app.models import User

router = APIRouter(tags=["live"])
RECHECK_S = 60


async def _still_allowed(user_id: uuid.UUID, tenant: str | None, token_version: int) -> bool:
    async with sessionmaker()() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active or (user.token_version or 0) != token_version:
            return False
        try:
            await resolve_ctx(user, db, tenant, None)
        except HTTPException:
            return False
    return True


@router.websocket("/ws")
async def live(ws: WebSocket, ticket: str = "", token: str = "", tenant: str | None = None) -> None:
    async with sessionmaker()() as db:
        try:
            if ticket:
                data = await ws_tickets.redeem(ticket)
                if data is None:
                    raise HTTPException(401, "Ticket ungültig")
                user = await db.get(User, uuid.UUID(data["user_id"]))
                if user is None or not user.is_active or (user.token_version or 0) != data["tv"]:
                    raise HTTPException(401, "Benutzer inaktiv")
                tenant = data["tenant"]
            else:
                user = await _user_from_token(token, db)
            ctx = await resolve_ctx(user, db, tenant, None)
        except HTTPException:
            await ws.close(code=4401)
            return
        allowed = str(ctx.tenant_id) if ctx.tenant_id else None
        is_super = ctx.is_superuser
        user_id, version = user.id, user.token_version or 0
    await ws.accept()
    await ws.send_json({"type": "hello", "tenant_id": allowed})

    async def pump() -> None:
        async for evt in events.subscribe():
            tid = evt.get("tenant_id")
            if allowed is not None and tid != allowed:
                continue
            if allowed is None and not is_super:
                continue
            await ws.send_json(evt)

    async def recheck() -> None:
        while True:
            await asyncio.sleep(RECHECK_S)
            if not await _still_allowed(user_id, tenant, version):
                await ws.close(code=4401)
                return

    tasks = [asyncio.create_task(pump()), asyncio.create_task(recheck())]
    try:
        while True:
            await ws.receive_text()  # Keepalive/Ping vom Client
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
