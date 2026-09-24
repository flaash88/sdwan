from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import AdminCtx, Ctx
from app.models import AuditLog
from app.schemas import AuditOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditOut])
async def list_audit(
    ctx: Ctx = AdminCtx,
    action: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
) -> list[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    # AuditLog ist bewusst nicht TenantScoped (MSP-Einträge ohne Tenant) -> expliziter Filter
    if ctx.tenant_id:
        q = q.where(AuditLog.tenant_id == ctx.tenant_id)
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if target_id:
        q = q.where(AuditLog.target_id == target_id)
    return list((await ctx.db.execute(q)).scalars())
