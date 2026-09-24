"""Audit-Logging für alle schreibenden Aktionen."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def audit(
    db: AsyncSession,
    action: str,
    *,
    tenant_id: uuid.UUID | None = None,
    user: Any = None,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    success: bool = True,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=tenant_id,
        user_id=getattr(user, "id", None),
        user_email=getattr(user, "email", None),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {},
        ip_address=ip,
        success=success,
    )
    db.add(entry)
    return entry
