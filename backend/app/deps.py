"""FastAPI-Dependencies: Authentifizierung, Tenant-Kontext, RBAC."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import ALL_TENANTS, get_db
from app.models import ROLE_RANK, Role, Tenant, User
from app.security import decode_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class Ctx:
    """Request-Kontext: wer handelt in welchem Tenant mit welcher Rolle."""

    user: User
    tenant_id: uuid.UUID | None  # None = Superuser ohne gewählten Tenant (alle Tenants)
    db: AsyncSession
    ip: str | None

    @property
    def role(self) -> Role:
        return Role.admin if self.user.is_superuser else self.user.role

    @property
    def is_superuser(self) -> bool:
        return self.user.is_superuser

    def require_tenant(self) -> uuid.UUID:
        if self.tenant_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Kein Tenant gewählt – als MSP-Admin bitte X-Tenant-ID Header setzen",
            )
        return self.tenant_id

    async def audit(self, action: str, **kw: Any) -> None:
        kw.setdefault("tenant_id", self.tenant_id)
        await audit(self.db, action, user=self.user, ip=self.ip, **kw)


async def _user_from_token(token: str, db: AsyncSession) -> User:
    try:
        payload = decode_token(token)
        if payload.get("typ") != "access":
            raise ValueError("wrong token type")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ungültiges Token") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Benutzer inaktiv oder unbekannt")
    return user


async def resolve_ctx(user: User, db: AsyncSession, tenant_header: str | None, ip: str | None) -> Ctx:
    if user.is_superuser:
        tenant_id: uuid.UUID | None = None
        if tenant_header:
            try:
                tenant_id = uuid.UUID(tenant_header)
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungültige X-Tenant-ID") from exc
            if await db.get(Tenant, tenant_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant nicht gefunden")
        db.info["tenant_scope"] = tenant_id if tenant_id else ALL_TENANTS
    else:
        if user.tenant_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Benutzer ist keinem Tenant zugeordnet")
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Tenant deaktiviert")
        tenant_id = user.tenant_id
        db.info["tenant_scope"] = tenant_id
    return Ctx(user=user, tenant_id=tenant_id, db=db, ip=ip)


async def get_ctx(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_tenant_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Ctx:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    user = await _user_from_token(creds.credentials, db)
    return await resolve_ctx(user, db, x_tenant_id, request.client.host if request.client else None)


def require_role(min_role: Role):
    async def _dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if ROLE_RANK[ctx.role] < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Rolle '{min_role.value}' erforderlich")
        return ctx

    return _dep


async def require_superuser(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur für MSP-Administratoren")
    return ctx


ReadCtx = Depends(require_role(Role.readonly))
TechCtx = Depends(require_role(Role.technician))
AdminCtx = Depends(require_role(Role.admin))
SuperCtx = Depends(require_superuser)
