"""Pairing-Logik (Token-Vergabe, Registrierung des Router-Public-Keys)."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.audit import audit
from app.config import get_settings
from app.db import utcnow
from app.models import Device, PairingStatus
from app.schemas import PairIn, PairingInfo
from app.security import encrypt_secret, generate_password, generate_token, hash_token
from app.services.onboarding import onboarding_command, pair_response_script
from app.services.wireguard import get_hub_public_key, is_valid_wg_key

log = logging.getLogger(__name__)


class PairingError(Exception):
    pass


def issue_pairing_token(device: Device, ttl_hours: int | None = None) -> PairingInfo:
    token = generate_token(24)
    ttl = ttl_hours if ttl_hours is not None else get_settings().pairing_token_ttl_hours
    device.pairing_token_hash = hash_token(token)
    device.pairing_expires_at = utcnow() + dt.timedelta(hours=ttl)
    base = get_settings().public_url.rstrip("/")
    return PairingInfo(
        token=token,
        expires_at=device.pairing_expires_at,
        command=onboarding_command(token),
        script_url=f"{base}/api/v1/onboard/{token}.rsc",
    )


async def find_device_by_token(db: AsyncSession, token: str) -> Device:
    dev = (
        await db.execute(
            select(Device).where(Device.pairing_token_hash == hash_token(token)).execution_options(skip_tenant_filter=True)
        )
    ).scalar_one_or_none()
    if dev is None:
        raise PairingError("Pairing-Token ungültig oder bereits verwendet")
    if dev.pairing_status == PairingStatus.revoked:
        raise PairingError("Gerät ist gesperrt")
    if dev.pairing_expires_at and dev.pairing_expires_at < utcnow():
        raise PairingError("Pairing-Token abgelaufen – bitte im Dashboard neu erzeugen")
    return dev


async def complete_pairing(db: AsyncSession, data: PairIn, ip: str | None = None, extra_script: str | None = None) -> tuple[Device, str]:
    """Registriert den Public-Key des Routers und liefert das Antwort-Script."""
    device = await find_device_by_token(db, data.token)
    if not is_valid_wg_key(data.public_key):
        raise PairingError("Ungültiger WireGuard-Public-Key")
    clash = (
        await db.execute(
            select(Device).where(Device.wg_public_key == data.public_key, Device.id != device.id).execution_options(skip_tenant_filter=True)
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise PairingError("Public-Key ist bereits einem anderen Gerät zugeordnet")
    if device.serial and data.serial and device.serial.upper() != data.serial.upper():
        # Zero-Touch: Token ist an eine Seriennummer gebunden
        raise PairingError("Seriennummer passt nicht zum Pairing-Token")
    hub_key = await get_hub_public_key(db)
    if not hub_key:
        raise PairingError("WireGuard-Hub noch nicht registriert – bitte später erneut versuchen")

    password = generate_password()
    device.wg_public_key = data.public_key
    device.api_password_enc = encrypt_secret(password)
    device.serial = data.serial or device.serial
    device.model = data.model
    device.architecture = data.architecture
    device.identity = data.identity
    device.routeros_version = data.routeros_version
    device.pairing_status = PairingStatus.paired
    device.paired_at = utcnow()
    device.pairing_token_hash = None  # Single-Use
    device.pairing_expires_at = None

    if extra_script is None:
        from app.services.ztp import ztp_script_for_device

        extra_script = await ztp_script_for_device(db, device)
    script = pair_response_script(device, hub_key, password, extra=extra_script or "")
    await audit(db, "device.paired", tenant_id=device.tenant_id, target_type="device", target_id=device.id, ip=ip,
                details={"serial": device.serial, "model": device.model, "version": device.routeros_version})
    await db.commit()
    await events.publish(device.tenant_id, "device.paired", {"id": str(device.id), "name": device.name})
    return device, script
