"""WebSocket für Live-Daten. Auth per ``?token=<jwt>&tenant=<uuid>`` (Browser-WS kann keine Header setzen)."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app import events
from app.db import sessionmaker
from app.deps import _user_from_token, resolve_ctx

router = APIRouter(tags=["live"])


@router.websocket("/ws")
async def live(ws: WebSocket, token: str = "", tenant: str | None = None) -> None:
    async with sessionmaker()() as db:
        try:
            user = await _user_from_token(token, db)
            ctx = await resolve_ctx(user, db, tenant, None)
        except HTTPException:
            await ws.close(code=4401)
            return
        allowed = str(ctx.tenant_id) if ctx.tenant_id else None
        is_super = ctx.is_superuser
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

    task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # Keepalive/Ping vom Client
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
