"""Hintergrund-Worker: periodische Jobs (Polling, Backups, Alerts, ...).

Start: ``python -m app.worker``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import locks
from app.config import get_settings
from app.db import create_all
from app.secrets_check import enforce as enforce_secrets
from app.worker.jobs import register_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


LEADER_KEY = "worker-leader"
STANDBY_RETRY_S = 15


async def _run_scheduler(stop: asyncio.Event) -> None:
    from app import platform_backup
    from app.services.policy import abort_stale_deployments

    # Aufräumen nach einem Neustart (AUDIT-016/017)
    for fn in (abort_stale_deployments, platform_backup.fail_stale_running):
        try:
            n = await fn()
            if n:
                log.warning("%s: %s Einträge als abgebrochen markiert", fn.__name__, n)
        except Exception:  # noqa: BLE001
            log.exception("Aufräumen %s fehlgeschlagen", fn.__name__)
    scheduler = AsyncIOScheduler(timezone="UTC")
    register_jobs(scheduler)
    scheduler.start()
    log.info("Worker gestartet (Leader), Jobs: %s", [j.id for j in scheduler.get_jobs()])
    await stop.wait()
    scheduler.shutdown(wait=False)


async def main() -> None:
    s = get_settings()
    enforce_secrets(s)  # AUDIT-006: kein Start mit Standard-Geheimnissen in Produktion
    if s.db_auto_create:
        await create_all()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # AUDIT-043: Jobs laufen nur in EINEM Worker (Leader-Sperre in Redis, verlängert sich selbst). Ein weiterer Worker
    # wartet als Standby und übernimmt, sobald die Sperre frei wird (spätestens ttl nach Ausfall des Leaders).
    while not stop.is_set():
        async with locks.hold(LEADER_KEY, f"worker:{socket.gethostname()}", ttl=60) as leader:
            if leader:
                await _run_scheduler(stop)
                return
        log.info("Anderer Worker ist aktiv – Standby, nächster Versuch in %s s", STANDBY_RETRY_S)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), STANDBY_RETRY_S)


if __name__ == "__main__":
    asyncio.run(main())
