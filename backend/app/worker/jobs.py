from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services import backup, firmware, mesh, metrics, poller, remote

log = logging.getLogger(__name__)


def _safe(fn):
    async def wrapper():
        try:
            await fn()
        except Exception:  # noqa: BLE001 - Job darf den Scheduler nie beenden
            log.exception("Job %s fehlgeschlagen", fn.__name__)

    wrapper.__name__ = wrapper.__qualname__ = fn.__name__
    return wrapper


async def live_poll() -> None:
    await poller.poll_all(only=await metrics.live_device_ids())


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    scheduler.add_job(_safe(poller.poll_all), "interval", seconds=s.poll_interval_seconds, id="poll_devices",
                      next_run_time=now + dt.timedelta(seconds=5), max_instances=1, coalesce=True)
    scheduler.add_job(_safe(mesh.auto_apply_all), "interval", minutes=5, id="mesh_auto_apply",
                      next_run_time=now + dt.timedelta(seconds=30), max_instances=1, coalesce=True)
    scheduler.add_job(_safe(live_poll), "interval", seconds=5, id="live_poll", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(remote.expire_sessions), "interval", seconds=30, id="remote_expire", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(backup.backup_all), "cron", hour=s.backup_hour_utc, minute=0, id="daily_backup", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(firmware.firmware_tick), "interval", seconds=15, id="firmware_tick", max_instances=1, coalesce=True)
