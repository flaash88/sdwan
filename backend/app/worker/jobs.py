from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services import mesh, poller

log = logging.getLogger(__name__)


def _safe(fn):
    async def wrapper():
        try:
            await fn()
        except Exception:  # noqa: BLE001 - Job darf den Scheduler nie beenden
            log.exception("Job %s fehlgeschlagen", fn.__name__)

    wrapper.__name__ = wrapper.__qualname__ = fn.__name__
    return wrapper


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    scheduler.add_job(_safe(poller.poll_all), "interval", seconds=s.poll_interval_seconds, id="poll_devices",
                      next_run_time=now + dt.timedelta(seconds=5), max_instances=1, coalesce=True)
    scheduler.add_job(_safe(mesh.auto_apply_all), "interval", minutes=5, id="mesh_auto_apply",
                      next_run_time=now + dt.timedelta(seconds=30), max_instances=1, coalesce=True)
