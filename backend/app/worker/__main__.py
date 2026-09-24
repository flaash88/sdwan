"""Hintergrund-Worker: periodische Jobs (Polling, Backups, Alerts, ...).

Start: ``python -m app.worker``
"""

from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import create_all
from app.worker.jobs import register_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


async def main() -> None:
    s = get_settings()
    if s.db_auto_create:
        await create_all()
    scheduler = AsyncIOScheduler(timezone="UTC")
    register_jobs(scheduler)
    scheduler.start()
    log.info("Worker gestartet, Jobs: %s", [j.id for j in scheduler.get_jobs()])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
