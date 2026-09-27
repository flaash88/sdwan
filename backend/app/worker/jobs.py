from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services import alerts, backup, feeds, firmware, hotspot, offboarding, mesh, metrics, poller, remote, scripts, sla, speedtest, syslog, wlan

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
    scheduler.add_job(_safe(feeds.feeds_tick), "interval", minutes=5, id="threat_feeds", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(speedtest.speedtest_tick), "cron", hour=3, minute=30, id="speedtest_weekly", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(hotspot.hotspot_tick), "interval", minutes=5, id="hotspot_tick", max_instances=1, coalesce=True)
    from app import platform_backup

    scheduler.add_job(_safe(platform_backup.backup_job), "cron", hour=s.platform_backup_hour_utc, minute=10, id="platform_backup",
                      max_instances=1, coalesce=True)
    scheduler.add_job(_safe(platform_backup.queue_job), "interval", minutes=1, id="platform_backup_queue", max_instances=1, coalesce=True)
    from app.services import local_access

    scheduler.add_job(_safe(local_access.rotation_tick), "cron", minute=35, id="local_access_rotation", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(offboarding.purge_archives), "cron", hour=4, minute=25, id="offboarding_archives", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(hotspot.purge_registrations), "cron", hour=4, minute=20, id="guest_retention", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(wlan.rotation_tick), "cron", hour=4, minute=45, id="wlan_psk_rotation", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(syslog.purge_old), "cron", hour=4, minute=15, id="syslog_retention", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(scripts.script_tick), "interval", seconds=15, id="script_tick", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(firmware.firmware_tick), "interval", seconds=15, id="firmware_tick", max_instances=1, coalesce=True)
    scheduler.add_job(_safe(alerts.evaluate_all), "interval", seconds=60, id="alerts", next_run_time=now + dt.timedelta(seconds=20),
                      max_instances=1, coalesce=True)
    scheduler.add_job(_safe(sla.monthly_reports), "cron", day=1, hour=6, minute=0, id="monthly_sla", max_instances=1, coalesce=True)
