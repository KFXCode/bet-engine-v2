#!/usr/bin/env python3
"""
scheduler.py
=============
Runs run_daily.main() automatically every day at
config.DAILY_RUN_HOUR:config.DAILY_RUN_MINUTE (config.TIMEZONE). Leave this
running in a terminal (or as a background service / systemd unit / launchd
job) instead of remembering to run run_daily.py by hand every morning.

    python scheduler.py

Ctrl+C to stop. For a single immediate run without waiting for the
schedule, just run `python run_daily.py` directly instead.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import run_daily

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scheduler")


def job():
    logger.info("Scheduled daily run starting...")
    try:
        run_daily.main([])
    except Exception:
        logger.exception("Daily run failed -- will try again tomorrow.")


def main():
    scheduler = BlockingScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        job, CronTrigger(hour=config.DAILY_RUN_HOUR, minute=config.DAILY_RUN_MINUTE),
        id="daily_mlb_run", misfire_grace_time=3600,
    )
    logger.info("Scheduler started. Daily run at %02d:%02d %s. Ctrl+C to stop.",
                config.DAILY_RUN_HOUR, config.DAILY_RUN_MINUTE, config.TIMEZONE)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
