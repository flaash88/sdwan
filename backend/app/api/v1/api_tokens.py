"""API-Tokens (Phase 24): je Benutzer, Rechte = Rolle oder nur lesend, mit Ablauf, gehasht, widerrufbar.

Der Klartext (``sdw_…``) wird nur bei der Erstellung einmal ausgegeben. Tokens können keine Tokens verwalten, keine 2FA
ändern und keine Vor-Ort-Passwörter anzeigen oder exportieren.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import utcnow
from app.deps import API_TOKEN_PREFIX, AdminCtx, Ctx, get_ctx
from app.models import ROLE_RANK, ApiToken, Role, User
from app.security import hash_token

router = APIRouter(tags=["api-tokens"])
MAX_DAYS = 365


def _out(t: ApiToken, email: str | None = None) -> dict[str, Any]:
    now = utcnow()
    state = "revoked" if t.revoked_at else "expired" if t.expires_at and t.expires_at <= now else "active"
    return {"id": str(t.id), "name": t.name, "prefix": t.prefix, "scope": t.scope, "expires_at": t.expires_at, "created_at": t.created_at,
            "last_used_at": t.last_used_at, "last_used_ip": t.last_used_ip, "revoked_at": t.revoked_at, "state": state, "user": email}


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["role", "read"] = "read"
    expires_in_days: int = Field(default=90, ge=1, le=MAX_DAYS)


@router.get("/auth/api-tokens")
async def my_tokens(ctx: Ctx = Depends(get_ctx)) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(ApiToken).where(ApiToken.user_id == ctx.user.id).order_by(ApiToken.created_at.desc()))).scalars()
    return [_out(t) for t in rows]


@router.post("/auth/api-tokens", status_code=201)
async def create_token(data: TokenIn, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.forbid_token("Das Erstellen von API-Tokens")
    if data.scope == "role" and ROLE_RANK[ctx.role] <= ROLE_RANK[Role.readonly] and not ctx.is_superuser:
        data.scope = "read"
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = ApiToken(tenant_id=ctx.user.tenant_id, user_id=ctx.user.id, name=data.name, prefix=token[:12], token_hash=hash_token(token),
                 scope=data.scope, expires_at=utcnow() + dt.timedelta(days=data.expires_in_days))
    ctx.db.add(t)
    await ctx.db.flush()
    await ctx.audit("api_token.create", target_type="api_token", target_id=t.id,
                    details={"name": t.name, "scope": t.scope, "prefix": t.prefix, "expires_at": t.expires_at.isoformat()})
    await ctx.db.commit()
    return {**_out(t), "token": token}


async def _revoke(ctx: Ctx, t: ApiToken) -> dict[str, Any]:
    if t.revoked_at is None:
        t.revoked_at = utcnow()
        await ctx.audit("api_token.revoke", target_type="api_token", target_id=t.id, details={"name": t.name, "prefix": t.prefix})
        await ctx.db.commit()
    return _out(t)


@router.delete("/auth/api-tokens/{token_id}")
async def revoke_own(token_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.forbid_token("Die Verwaltung von API-Tokens")
    t = await ctx.db.get(ApiToken, token_id)
    if t is None or t.user_id != ctx.user.id:
        raise HTTPException(404, "Token nicht gefunden")
    return await _revoke(ctx, t)


@router.get("/api-tokens")
async def tenant_tokens(ctx: Ctx = AdminCtx) -> list[dict[str, Any]]:
    """Admin: Tokens aller Benutzer des Mandanten (MSP-Admin ohne Mandant: alle)."""
    q = select(ApiToken, User.email).join(User, User.id == ApiToken.user_id).order_by(ApiToken.created_at.desc())
    if ctx.tenant_id is not None:
        q = q.where(ApiToken.tenant_id == ctx.tenant_id)
    elif not ctx.is_superuser:
        return []
    return [_out(t, email) for t, email in (await ctx.db.execute(q)).all()]


@router.delete("/api-tokens/{token_id}")
async def revoke_any(token_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    ctx.forbid_token("Die Verwaltung von API-Tokens")
    t = await ctx.db.get(ApiToken, token_id)
    if t is None or (not ctx.is_superuser and t.tenant_id != ctx.tenant_id) or (ctx.tenant_id is not None and t.tenant_id != ctx.tenant_id):
        raise HTTPException(404, "Token nicht gefunden")
    return await _revoke(ctx, t)
