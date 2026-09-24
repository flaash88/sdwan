"""Zero-Touch-Provisioning (Phase 6). Bis dahin: keine Zusatzkonfiguration."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device


async def ztp_script_for_device(db: AsyncSession, device: Device) -> str:
    return ""
