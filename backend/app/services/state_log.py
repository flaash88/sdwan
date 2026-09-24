"""Statuswechsel-Protokoll (Basis für SLA-Reports, Phase 10)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import Device, DeviceStatus, StatusEvent


async def record_state_change(db: AsyncSession, device: Device, old: DeviceStatus, new: DeviceStatus) -> None:
    if new == DeviceStatus.unknown:
        return
    db.add(StatusEvent(tenant_id=device.tenant_id, device_id=device.id, subject="device", status=new.value, at=utcnow()))


async def record_subject_change(db: AsyncSession, tenant_id: uuid.UUID, device_id: uuid.UUID, subject: str, status: str) -> None:
    db.add(StatusEvent(tenant_id=tenant_id, device_id=device_id, subject=subject, status=status, at=utcnow()))
