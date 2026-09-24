"""Konfigurations-Backups (Phase 9).

* Export per SSH-Exec ``/export terse`` über den Management-Tunnel (die RouterOS-API kann ``/export``
  nicht zuverlässig liefern). ``terse`` = eine Zeile pro Objekt -> saubere Zeilen-Diffs.
* RouterOS 7 blendet Secrets im Export standardmäßig aus – Backups enthalten keine Passwörter/Keys.
* Deduplizierung per SHA-256 (ohne Zeitstempel-Kopfzeile); Diff zum Vorgänger wird als JSON gespeichert.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.db import system_session
from app.models import ConfigBackup, Device, DeviceStatus, PairingStatus
from app.routeros import RouterOSError, connect_device
from app.routeros.client import assert_tunnel_address
from app.security import decrypt_secret

log = logging.getLogger(__name__)
_HEADER = re.compile(r"^# (\d{4}-\d\d-\d\d|[a-z]{3}/\d\d/\d{4}) .* by RouterOS")


class BackupError(Exception):
    pass


def normalize(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(ln for ln in lines if not _HEADER.match(ln)).strip() + "\n"


async def export_config(device: Device) -> str:
    s = get_settings()
    if s.routeros_backend == "simulator":
        async with connect_device(device) as api:
            rows = await api.call("/export")
        return str(rows[0].get("ret", "")) if rows else ""
    if not device.api_password_enc:
        raise BackupError("Gerät nicht gepairt")
    assert_tunnel_address(device.tunnel_ip)
    import asyncssh

    try:
        async with asyncssh.connect(
            device.tunnel_ip, port=s.ssh_port, username=s.routeros_api_user, password=decrypt_secret(device.api_password_enc),
            known_hosts=None, connect_timeout=15,
        ) as conn:
            res = await asyncio.wait_for(conn.run("/export terse", check=False), timeout=120)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise BackupError(f"SSH-Export fehlgeschlagen: {exc}") from exc
    if res.exit_status not in (0, None):
        raise BackupError(f"Export-Fehler: {res.stderr}")
    return str(res.stdout)


def make_diff(old: str, new: str, context: int = 2) -> dict[str, Any]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "vorher", "nachher", n=context, lineterm=""))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return {"added": added, "removed": removed, "lines": lines[:5000]}


async def take_backup(
    db: AsyncSession, device: Device, trigger: str = "scheduled", note: str | None = None, raw: str | None = None
) -> tuple[ConfigBackup, bool]:
    """Erstellt ein Backup. Rückgabe (backup, neu?) – bei unveränderter Konfig wird kein neues angelegt
    (außer bei manuellen/pre-update-Backups, die immer gespeichert werden). ``raw`` = bereits geholter Export."""
    if raw is None:
        raw = await export_config(device)
    content = normalize(raw)
    digest = hashlib.sha256(content.encode()).hexdigest()
    prev = (
        await db.execute(select(ConfigBackup).where(ConfigBackup.device_id == device.id).order_by(ConfigBackup.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if prev is not None and prev.sha256 == digest and trigger == "scheduled":
        return prev, False
    diff = make_diff(prev.content, content) if prev else {"added": content.count("\n"), "removed": 0, "lines": []}
    diff["previous_id"] = str(prev.id) if prev else None
    b = ConfigBackup(tenant_id=device.tenant_id, device_id=device.id, trigger=trigger, routeros_version=device.routeros_version,
                     content=content, sha256=digest, size=len(content), diff=diff, pinned=trigger != "scheduled", note=note)
    db.add(b)
    await db.flush()
    await _retention(db, device)
    if prev is not None and prev.sha256 != digest:
        await events.publish(device.tenant_id, "backup.changed", {"device_id": str(device.id), "device": device.name,
                                                                  "added": diff["added"], "removed": diff["removed"]})
    return b, True


async def _retention(db: AsyncSession, device: Device) -> None:
    keep = get_settings().backup_retention
    ids = (
        await db.execute(
            select(ConfigBackup.id).where(ConfigBackup.device_id == device.id, ConfigBackup.pinned.is_(False))
            .order_by(ConfigBackup.created_at.desc()).offset(keep)
        )
    ).scalars().all()
    if ids:
        await db.execute(delete(ConfigBackup).where(ConfigBackup.id.in_(ids)))


async def backup_all() -> dict[str, int]:
    """Worker-Job (täglich): Backup aller erreichbaren Geräte."""
    stats = {"new": 0, "unchanged": 0, "failed": 0}
    async with system_session() as db:
        devices = (await db.execute(select(Device).where(Device.pairing_status == PairingStatus.paired, Device.status == DeviceStatus.online))).scalars().all()
        sem = asyncio.Semaphore(10)
        exports: dict[Any, str | Exception] = {}

        async def fetch(dev: Device) -> None:
            async with sem:
                try:
                    exports[dev.id] = await export_config(dev)
                except (BackupError, RouterOSError) as exc:
                    exports[dev.id] = exc

        await asyncio.gather(*(fetch(d) for d in devices))
        for dev in devices:
            res = exports.get(dev.id)
            if isinstance(res, Exception) or res is None:
                stats["failed"] += 1
                log.warning("Backup %s fehlgeschlagen: %s", dev.name, res)
                continue
            _b, new = await take_backup(db, dev, "scheduled", raw=res)
            stats["new" if new else "unchanged"] += 1
        await db.commit()
    log.info("Backups: %s", stats)
    return stats

