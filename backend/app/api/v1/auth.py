from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db, utcnow
from app.deps import Ctx, get_ctx
from app.models import Tenant, User
from app.schemas import LoginIn, TenantOut, TokenOut, UserOut
from app.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    user = (await db.execute(select(User).where(User.email == data.email.lower()))).scalar_one_or_none()
    ip = request.client.host if request.client else None
    if user is None or not user.is_active or not verify_password(data.password, user.password_hash):
        await audit(db, "auth.login_failed", details={"email": data.email}, ip=ip, success=False,
                    tenant_id=user.tenant_id if user else None)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-Mail oder Passwort falsch")
    user.last_login_at = utcnow()
    await audit(db, "auth.login", user=user, tenant_id=user.tenant_id, ip=ip)
    await db.commit()
    return TokenOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


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
    }
