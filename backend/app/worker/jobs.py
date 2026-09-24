from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services import poller

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
