"""Statuswechsel-Protokoll (Basis für SLA-Reports in Phase 10)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceStatus


async def record_state_change(db: AsyncSession, device: Device, old: DeviceStatus, new: DeviceStatus) -> None:
    return None
