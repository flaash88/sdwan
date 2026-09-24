from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.v1.common import apply_update, get_or_404
from app.deps import AdminCtx, Ctx
from app.models import User
from app.schemas import UserCreate, UserOut, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/users", tags=["users"])


def _check_access(ctx: Ctx, user: User) -> None:
    if ctx.is_superuser:
        if ctx.tenant_id and user.tenant_id != ctx.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Benutzer nicht gefunden")
        return
    if user.tenant_id != ctx.tenant_id or user.is_superuser:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benutzer nicht gefunden")


@router.get("", response_model=list[UserOut])
async def list_users(ctx: Ctx = AdminCtx) -> list[User]:
    q = select(User).order_by(User.email)
    if ctx.tenant_id:
        q = q.where(User.tenant_id == ctx.tenant_id)
    return list((await ctx.db.execute(q)).scalars())


@router.post("", response_model=UserOut, status_code=201)
async def create_user(data: UserCreate, ctx: Ctx = AdminCtx) -> User:
    if data.is_superuser and not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur MSP-Admins dürfen MSP-Admins anlegen")
    tenant_id = None if data.is_superuser else (ctx.tenant_id if not ctx.is_superuser else (data.tenant_id or ctx.tenant_id))
    if not data.is_superuser and tenant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tenant_id erforderlich")
    if (await ctx.db.execute(select(User).where(User.email == data.email.lower()))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "E-Mail bereits vergeben")
    user = User(
        email=data.email.lower(),
        full_name=data.full_name,
        role=data.role,
        tenant_id=tenant_id,
        is_superuser=data.is_superuser,
        password_hash=hash_password(data.password),
    )
    ctx.db.add(user)
    await ctx.db.flush()
    await ctx.audit("user.create", tenant_id=tenant_id, target_type="user", target_id=user.id,
                    details={"email": user.email, "role": user.role.value, "superuser": user.is_superuser})
    await ctx.db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, data: UserUpdate, ctx: Ctx = AdminCtx) -> User:
    user = await get_or_404(ctx.db, User, user_id, "Benutzer")
    _check_access(ctx, user)
    payload = data.model_dump(exclude_unset=True)
    pw = payload.pop("password", None)
    changed = apply_update(user, payload)
    if pw:
        user.password_hash = hash_password(pw)
        changed["password"] = "***"
    await ctx.audit("user.update", tenant_id=user.tenant_id, target_type="user", target_id=user.id, details=changed)
    await ctx.db.commit()
    return user


@router.delete("/{user_id}", status_code=204, response_model=None)
async def delete_user(user_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    user = await get_or_404(ctx.db, User, user_id, "Benutzer")
    _check_access(ctx, user)
    if user.id == ctx.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Eigenen Benutzer nicht löschbar")
    await ctx.audit("user.delete", tenant_id=user.tenant_id, target_type="user", target_id=user.id, details={"email": user.email})
    await ctx.db.delete(user)
    await ctx.db.commit()
