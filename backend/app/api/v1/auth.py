from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ratelimit, totp
from app.audit import audit
from app.config import get_settings
from app.db import get_db, utcnow
from app.deps import Ctx, bearer, get_ctx
from app.models import Tenant, User
from app.schemas import LoginIn, TenantOut, TokenOut, UserOut
from app.security import create_access_token, decode_token, decrypt_secret, encrypt_secret, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

MFA_TTL = dt.timedelta(minutes=5)
_DUMMY_HASH = hash_password("dummy-password-for-timing")  # nur für den Zeitausgleich bei unbekannten Konten
SETUP_TTL = dt.timedelta(minutes=15)


# ----------------------------------------------------------------------------- Hilfen
def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _step_token(user: User, typ: str, ttl: dt.timedelta) -> str:
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    return jwt.encode({"sub": str(user.id), "typ": typ, "iat": now, "exp": now + ttl}, s.secret_key, algorithm=s.jwt_algorithm)


async def _user_from_step(db: AsyncSession, token: str, typ: str) -> User:
    try:
        payload = decode_token(token)
        if payload.get("typ") != typ:
            raise ValueError("falscher Tokentyp")
        user = await db.get(User, uuid.UUID(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Anmeldeschritt abgelaufen – bitte neu anmelden") from exc
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Benutzer inaktiv oder unbekannt")
    return user


async def mfa_required_for(db: AsyncSession, user: User) -> bool:
    """2FA-Pflicht: MSP-Admins immer (Einstellung), sonst je Mandant ``settings.require_2fa``."""
    if user.is_superuser:
        return get_settings().mfa_enforce_superuser
    if user.tenant_id:
        t = await db.get(Tenant, user.tenant_id)
        return bool(t and (t.settings or {}).get("require_2fa"))
    return False


def _locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > utcnow()


async def _fail(db: AsyncSession, user: User | None, ip: str | None, action: str, email: str | None = None) -> None:
    """Fehlversuch zählen; ab ``login_max_failures`` Sperre für ``login_lock_minutes``."""
    s = get_settings()
    await ratelimit.hit(f"login:{ip}", s.login_ip_limit, s.login_ip_window_s)
    await audit(db, action, details={"email": email or (user.email if user else None)}, ip=ip, success=False,
                tenant_id=user.tenant_id if user else None)
    if user is not None:
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = utcnow() + dt.timedelta(minutes=s.login_lock_minutes)
            user.failed_logins = 0
            await audit(db, "auth.locked", user=user, tenant_id=user.tenant_id, ip=ip, success=False,
                        details={"until": user.locked_until.isoformat()})
    await db.commit()


async def _ip_limit(request: Request) -> None:
    """Fehlversuche je IP (gezählt in ``_fail``); über dem Limit wird gar nicht mehr geprüft."""
    s = get_settings()
    if await ratelimit.over(f"login:{_ip(request)}", s.login_ip_limit, s.login_ip_window_s):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Zu viele Anmeldeversuche – bitte später erneut versuchen")


async def _success(db: AsyncSession, user: User, ip: str | None, method: str) -> TokenOut:
    user.last_login_at = utcnow()
    user.failed_logins = 0
    await audit(db, "auth.login", user=user, tenant_id=user.tenant_id, ip=ip, details={"method": method})
    await db.commit()
    return TokenOut(access_token=create_access_token(user.id, {"tv": user.token_version or 0}), user=UserOut.model_validate(user))


def _check_code(user: User, code: str) -> str | None:
    """TOTP oder Wiederherstellungscode prüfen → 'totp' | 'recovery' | None (setzt verbrauchte Werte)."""
    if not user.totp_secret_enc:
        return None
    step = totp.verify(decrypt_secret(user.totp_secret_enc), code, user.totp_last_step)
    if step is not None:
        user.totp_last_step = step
        return "totp"
    h = totp.hash_recovery(code)
    if h in (user.recovery_codes or []):
        user.recovery_codes = [c for c in user.recovery_codes if c != h]
        return "recovery"
    return None


# ----------------------------------------------------------------------------- Anmeldung
@router.post("/login", response_model=TokenOut, response_model_exclude_none=True)
async def login(data: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    ip = _ip(request)
    await _ip_limit(request)
    user = (await db.execute(select(User).where(User.email == data.email.lower()))).scalar_one_or_none()
    if user is not None and _locked(user):
        await audit(db, "auth.login_blocked", user=user, tenant_id=user.tenant_id, ip=ip, success=False)
        await db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Konto nach zu vielen Fehlversuchen vorübergehend gesperrt")
    # Unbekanntes Konto: trotzdem bcrypt rechnen, damit die Antwortzeit keine Konten verrät (AUDIT-024)
    ok = verify_password(data.password, user.password_hash if user is not None else _DUMMY_HASH)
    if user is None or not user.is_active or not ok:
        await _fail(db, user if user and user.is_active else None, ip, "auth.login_failed", data.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-Mail oder Passwort falsch")
    if user.totp_enabled:
        await db.commit()
        return TokenOut(mfa_required=True, mfa_token=_step_token(user, "mfa", MFA_TTL))
    if await mfa_required_for(db, user):
        await audit(db, "auth.2fa_setup_required", user=user, tenant_id=user.tenant_id, ip=ip)
        await db.commit()
        return TokenOut(mfa_setup_required=True, setup_token=_step_token(user, "mfa_setup", SETUP_TTL))
    return await _success(db, user, ip, "password")


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=20)
    mfa_token: str | None = None
    setup_token: str | None = None


@router.post("/login/2fa", response_model=TokenOut, response_model_exclude_none=True)
async def login_2fa(data: CodeIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    ip = _ip(request)
    await _ip_limit(request)
    user = await _user_from_step(db, data.mfa_token or "", "mfa")
    if _locked(user):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Konto nach zu vielen Fehlversuchen vorübergehend gesperrt")
    method = _check_code(user, data.code)
    if method is None:
        await _fail(db, user, ip, "auth.2fa_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code ungültig")
    if method == "recovery":
        await audit(db, "auth.recovery_code_used", user=user, tenant_id=user.tenant_id, ip=ip,
                    details={"remaining": len(user.recovery_codes or [])})
    return await _success(db, user, ip, method)


# ----------------------------------------------------------------------------- Einrichtung / Verwaltung
async def _setup_user(request: Request, db: AsyncSession, setup_token: str | None) -> tuple[User, bool]:
    """Einrichtung entweder mit Setup-Token (erzwungene Einrichtung bei der Anmeldung) oder angemeldet."""
    if setup_token:
        return await _user_from_step(db, setup_token, "mfa_setup"), True
    creds: HTTPAuthorizationCredentials | None = await bearer(request)
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    from app.deps import _user_from_token

    user = await _user_from_token(creds.credentials, db, request)
    if getattr(request.state, "via_api_token", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "2FA kann nicht mit einem API-Token verwaltet werden")
    return user, False


class SetupIn(BaseModel):
    setup_token: str | None = None


@router.post("/2fa/setup")
async def setup(data: SetupIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Neues (noch nicht aktives) Geheimnis erzeugen; aktiv erst nach /2fa/enable mit gültigem Code."""
    from app.services.wlan import qr_svg

    user, _forced = await _setup_user(request, db, data.setup_token)
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "2FA ist bereits eingerichtet")
    secret = totp.generate_secret()
    user.totp_secret_enc = encrypt_secret(secret)
    user.totp_last_step = None
    await db.commit()
    uri = totp.otpauth_uri(secret, user.email, get_settings().product_name)
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": qr_svg(uri)}


@router.post("/2fa/enable", response_model_exclude_none=True)
async def enable(data: CodeIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    user, forced = await _setup_user(request, db, data.setup_token)
    if user.totp_enabled or not user.totp_secret_enc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Zuerst /2fa/setup aufrufen")
    step = totp.verify(decrypt_secret(user.totp_secret_enc), data.code, None)
    if step is None:
        await _fail(db, user, _ip(request), "auth.2fa_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code ungültig – Uhrzeit des Handys prüfen")
    codes = totp.generate_recovery_codes()
    user.totp_enabled, user.totp_last_step = True, step
    user.recovery_codes = [totp.hash_recovery(c) for c in codes]
    await audit(db, "auth.2fa_enabled", user=user, tenant_id=user.tenant_id, ip=_ip(request))
    out: dict[str, Any] = {"recovery_codes": codes}
    if forced:  # erzwungene Einrichtung: Anmeldung damit abschließen
        tok = await _success(db, user, _ip(request), "totp_setup")
        out.update(tok.model_dump(mode="json", exclude_none=True))
    else:
        await db.commit()
    return out


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=20)


@router.post("/2fa/disable")
async def disable(data: ConfirmIn, request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.user
    if getattr(request.state, "via_api_token", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "2FA kann nicht mit einem API-Token verwaltet werden")
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "2FA ist nicht aktiv")
    if await mfa_required_for(ctx.db, user):
        raise HTTPException(status.HTTP_409_CONFLICT, "2FA ist für dieses Konto Pflicht und kann nicht deaktiviert werden")
    if _check_code(user, data.code) is None:
        await _fail(ctx.db, user, ctx.ip, "auth.2fa_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code ungültig")
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step, user.recovery_codes = False, None, None, []
    await ctx.audit("auth.2fa_disabled", target_type="user", target_id=user.id)
    await ctx.db.commit()
    return {"totp_enabled": False}


@router.post("/2fa/recovery-codes")
async def regenerate_codes(data: ConfirmIn, request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.user
    if getattr(request.state, "via_api_token", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "2FA kann nicht mit einem API-Token verwaltet werden")
    if not user.totp_enabled or _check_code(user, data.code) != "totp":
        await _fail(ctx.db, user, ctx.ip, "auth.2fa_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Gültigen Code aus der Authenticator-App angeben")
    codes = totp.generate_recovery_codes()
    user.recovery_codes = [totp.hash_recovery(c) for c in codes]
    await ctx.audit("auth.recovery_codes_regenerated", target_type="user", target_id=user.id)
    await ctx.db.commit()
    return {"recovery_codes": codes}


@router.post("/ws-ticket")
async def ws_ticket(ctx: Ctx = Depends(get_ctx)) -> dict:
    """Einmal-Ticket (30 s) für die WebSocket-Verbindung – statt des JWT in der URL (AUDIT-011)."""
    from app import ws_tickets

    tenant = str(ctx.tenant_id) if ctx.is_superuser and ctx.tenant_id else None
    ticket = await ws_tickets.issue(ctx.user.id, tenant, ctx.user.token_version or 0)
    return {"ticket": ticket, "expires_in": ws_tickets.TTL_S}


@router.get("/me")
async def me(ctx: Ctx = Depends(get_ctx)) -> dict:
    tenants: list[Tenant]
    if ctx.is_superuser:
        tenants = list((await ctx.db.execute(select(Tenant).order_by(Tenant.name))).scalars())
    else:
        t = await ctx.db.get(Tenant, ctx.tenant_id)
        tenants = [t] if t else []
    return {
        "user": UserOut.model_validate(ctx.user).model_dump(mode="json"),
        "active_tenant_id": str(ctx.tenant_id) if ctx.tenant_id else None,
        "role": ctx.role.value,
        "tenants": [TenantOut.model_validate(t).model_dump(mode="json") for t in tenants],
        "mfa_required": await mfa_required_for(ctx.db, ctx.user),
        "recovery_codes_left": len(ctx.user.recovery_codes or []) if ctx.user.totp_enabled else None,
    }
