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

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="Bearer",
    description="Anmelde-Token (JWT aus /auth/login) oder API-Token `sdw_…` (Profil → API-Tokens). API-Tokens: Rechte der Rolle "
    "oder nur lesend, mit Ablauf; nicht erlaubt sind Vor-Ort-Passwörter (Anzeige/Export), 2FA- und Token-Verwaltung. "
    "Schreibende Aufrufe per Token werden im Audit-Log als `api_token.use` protokolliert.",
)


@dataclass
class Ctx:
    """Request-Kontext: wer handelt in welchem Tenant mit welcher Rolle."""

    user: User
    tenant_id: uuid.UUID | None  # None = Superuser ohne gewählten Tenant (alle Tenants)
    db: AsyncSession
    ip: str | None
    api_token: Any = None  # ApiToken, wenn per API-Token angemeldet

    @property
    def role(self) -> Role:
        if self.api_token is not None and self.api_token.scope == "read":
            return Role.readonly  # Token-Rechte: höchstens die Rolle, bei „read“ nur lesend
        return Role.admin if self.user.is_superuser else self.user.role

    @property
    def via_token(self) -> bool:
        return self.api_token is not None

    def forbid_token(self, what: str) -> None:
        """Aktionen, die nie per API-Token erlaubt sind (Vor-Ort-Passwörter, 2FA, Token-Verwaltung)."""
        if self.api_token is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{what} ist mit einem API-Token nicht erlaubt – bitte im Browser anmelden")

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


API_TOKEN_PREFIX = "sdw_"
_WRITE = ("POST", "PUT", "PATCH", "DELETE")


async def _user_from_api_token(token: str, db: AsyncSession, request: Request | None) -> User:
    """API-Token ``sdw_…``: nur der sha256-Hash ist gespeichert; abgelaufen/widerrufen → 401."""
    from sqlalchemy import select

    from app.db import utcnow
    from app.models import ApiToken
    from app.security import hash_token

    row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))).scalar_one_or_none()
    now = utcnow()
    if row is None or row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API-Token ungültig, abgelaufen oder widerrufen")
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Benutzer inaktiv oder unbekannt")
    ip = request.client.host if request is not None and request.client else None
    if row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60 or row.last_used_ip != ip:
        row.last_used_at, row.last_used_ip = now, ip
        await db.commit()
    if request is not None:
        request.state.via_api_token = True
        request.state.api_token = row
    return user


async def _user_from_token(token: str, db: AsyncSession, request: Request | None = None) -> User:
    if token.startswith(API_TOKEN_PREFIX):
        return await _user_from_api_token(token, db, request)
    try:
        payload = decode_token(token)
        if payload.get("typ") != "access":
            raise ValueError("wrong token type")
        user_id = uuid.UUID(payload["sub"])
        version = int(payload.get("tv", 0))  # Tokens von vor AUDIT-029 ohne Claim = Version 0
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ungültiges Token") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Benutzer inaktiv oder unbekannt")
    if version != (user.token_version or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Anmeldung abgelaufen (Passwort oder Rechte geändert) – bitte neu anmelden")
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
    user = await _user_from_token(creds.credentials, db, request)
    ctx = await resolve_ctx(user, db, x_tenant_id, request.client.host if request.client else None)
    tok = getattr(request.state, "api_token", None)
    if tok is not None:
        ctx.api_token = tok
        if request.method in _WRITE:
            if tok.scope == "read":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "API-Token ist nur lesend")
            await ctx.audit("api_token.use", target_type="api_token", target_id=tok.id,
                            details={"name": tok.name, "prefix": tok.prefix, "method": request.method, "path": request.url.path})
    return ctx


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
