"""Fleet-Firmware-Updates mit Warteschlange/Batching (Phase 9).

Ein Job verteilt die Geräte auf Batches (``batch_size``). Der Worker (``firmware_tick``, alle 15 s)
bearbeitet immer nur den aktuellen Batch:

1. Pre-Update-Backup, Update-Kanal setzen, ``check-for-updates``.
2. Bereits aktuell -> ``skipped``; sonst ``install`` (Router lädt, installiert, rebootet) -> ``rebooting``.
3. Folgende Ticks prüfen, ob das Gerät wieder erreichbar ist und die neue Version meldet -> ``success``,
   optional danach RouterBOARD-Firmware-Upgrade + Reboot. Timeout -> ``failed``.
4. Ist der Batch fertig: mehr Fehler als ``max_failures`` -> Job ``paused`` (manuell fortsetzen),
   sonst nächster Batch nach ``batch_interval_s``.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.audit import audit
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, FirmwareJob, FirmwareJobItem
from app.routeros import RouterOSError, connect_device

log = logging.getLogger(__name__)
FINAL = {"success", "skipped", "failed", "cancelled"}
CHANNELS = ("stable", "long-term", "testing", "development")


def _ver(v: Any) -> str:
    return str(v or "").split(" ")[0]


async def check_updates(device: Device, channel: str | None = None) -> dict[str, Any]:
    async with connect_device(device) as api:
        if channel:
            await api.call("/system/package/update/set", channel=channel)
        rows = await api.call("/system/package/update/check-for-updates")
        info = rows[-1] if rows else {}
        if not info.get("latest-version"):
            info = (await api.call("/system/package/update/print") or [{}])[0]
    result = {
        "channel": info.get("channel", channel), "installed": _ver(info.get("installed-version")),
        "latest": _ver(info.get("latest-version")), "status": info.get("status"), "checked_at": utcnow().isoformat(),
    }
    result["update_available"] = bool(result["latest"]) and result["latest"] != result["installed"]
    return result


async def _start_item(db: AsyncSession, job: FirmwareJob, item: FirmwareJobItem, dev: Device) -> None:
    from app.services.backup import BackupError, take_backup

    item.started_at = utcnow()
    item.status = "updating"
    try:
        try:
            await take_backup(db, dev, "pre-update", note=f"vor Firmware-Job {job.name}", created_by=job.created_by)
        except BackupError as exc:
            log.warning("Pre-Update-Backup %s fehlgeschlagen: %s", dev.name, exc)
        info = await check_updates(dev, job.channel)
        item.from_version = info["installed"]
        item.to_version = info["latest"]
        dev.facts = {**(dev.facts or {}), "update": info}
        if not info["update_available"]:
            item.status, item.finished_at = "skipped", utcnow()
            return
        async with connect_device(dev) as api:
            try:
                await api.call("/system/package/update/install")
            except RouterOSError as exc:
                # Verbindung bricht beim Reboot ab – das ist erwartbar
                log.info("install %s: %s (Reboot?)", dev.name, exc)
        item.status = "rebooting"
    except RouterOSError as exc:
        item.status, item.error, item.finished_at = "failed", str(exc), utcnow()


async def _verify_item(db: AsyncSession, job: FirmwareJob, item: FirmwareJobItem, dev: Device) -> None:
    timeout = dt.timedelta(seconds=get_settings().firmware_reboot_timeout_s)
    try:
        async with connect_device(dev) as api:
            res = await api.resource()
            version = _ver(res.get("version"))
            if item.to_version and version == item.to_version:
                if job.upgrade_routerboard:
                    try:
                        rb = (await api.call("/system/routerboard/print") or [{}])[0]
                        if rb.get("upgrade-firmware") and rb.get("upgrade-firmware") != rb.get("current-firmware"):
                            await api.call("/system/routerboard/upgrade")
                            await api.call("/system/reboot")
                    except RouterOSError as exc:
                        log.info("RouterBOARD-Upgrade %s: %s", dev.name, exc)
                item.status, item.finished_at = "success", utcnow()
                dev.routeros_version = str(res.get("version"))
                return
    except RouterOSError:
        pass  # noch im Reboot
    if item.started_at and utcnow() - item.started_at > timeout:
        item.status, item.finished_at = "failed", utcnow()
        item.error = f"Gerät nach {timeout.seconds // 60} min nicht mit Version {item.to_version} zurück"


async def tick_job(db: AsyncSession, job: FirmwareJob) -> None:
    items = (await db.execute(select(FirmwareJobItem).where(FirmwareJobItem.job_id == job.id).order_by(FirmwareJobItem.batch_no))).scalars().all()
    if not items:
        job.status, job.finished_at = "completed", utcnow()
        return
    devices = {d.id: d for d in (await db.execute(select(Device).where(Device.id.in_([i.device_id for i in items])))).scalars()}
    open_batches = sorted({i.batch_no for i in items if i.status not in FINAL})
    if not open_batches:
        failed = sum(1 for i in items if i.status == "failed")
        job.status = "failed" if failed and failed == len(items) else "completed"
        job.finished_at = utcnow()
        await events.publish(job.tenant_id, "firmware.job", {"id": str(job.id), "status": job.status})
        return
    batch = open_batches[0]
    if batch != job.current_batch:
        # Vorheriger Batch fertig -> Fehlerschwelle prüfen, Pause einhalten
        prev_failed = sum(1 for i in items if i.batch_no < batch and i.status == "failed")
        if job.max_failures > 0 and prev_failed >= job.max_failures:
            job.status, job.last_error = "paused", f"{prev_failed} fehlgeschlagene Geräte – Job pausiert"
            await events.publish(job.tenant_id, "firmware.job", {"id": str(job.id), "status": job.status})
            return
        if job.next_batch_at is None:
            job.next_batch_at = utcnow() + dt.timedelta(seconds=job.batch_interval_s)
        if utcnow() < job.next_batch_at:
            return
        job.current_batch, job.next_batch_at = batch, None
    for item in (i for i in items if i.batch_no == batch):
        dev = devices.get(item.device_id)
        if dev is None:
            item.status, item.error = "failed", "Gerät gelöscht"
            continue
        if item.status == "queued":
            await _start_item(db, job, item, dev)
        elif item.status == "rebooting":
            await _verify_item(db, job, item, dev)
        if item.status in FINAL:
            await events.publish(item.tenant_id, "firmware.item", {"job_id": str(job.id), "device_id": str(dev.id), "status": item.status})


async def firmware_tick() -> None:
    async with system_session() as db:
        jobs = (await db.execute(select(FirmwareJob).where(FirmwareJob.status == "running"))).scalars().all()
        for job in jobs:
            try:
                await tick_job(db, job)
            except Exception as exc:  # noqa: BLE001
                log.exception("Firmware-Job %s", job.id)
                job.last_error = str(exc)
            if job.status in ("completed", "failed", "paused"):
                await audit(db, f"firmware.job.{job.status}", tenant_id=job.tenant_id, target_type="firmware_job", target_id=job.id,
                            success=job.status == "completed", details={"name": job.name, "error": job.last_error})
        await db.commit()


def plan_batches(device_ids: list[Any], batch_size: int) -> list[tuple[Any, int]]:
    return [(d, i // max(batch_size, 1)) for i, d in enumerate(device_ids)]
