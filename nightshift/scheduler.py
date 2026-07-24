"""Internal scheduler – replaces system cron (Docker-friendly)."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import cfg
from .jobs import enqueue
from .nightly import run_nightly


def _timezone():
    """Resolve the scheduler timezone: TZ env var → system → UTC fallback."""
    tz_name = (os.environ.get("TZ") or "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:
        return ZoneInfo("UTC")


def _enqueue_nightly():
    """Scheduled runs go through the same serial queue as manual jobs."""
    enqueue(None, lambda _job_id: run_nightly(), label="Nightly")

_scheduler: BackgroundScheduler | None = None


def start():
    """Starts the nightly job according to cfg.nightly.schedule (cron syntax)."""
    global _scheduler
    if _scheduler:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=_timezone())
    _scheduler.add_job(
        _enqueue_nightly,
        CronTrigger.from_crontab(cfg.nightly.schedule),
        id="nightly",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    return _scheduler


def reschedule():
    """Re-applies the cron trigger after a config change (settings page)."""
    if _scheduler:
        _scheduler.reschedule_job(
            "nightly", trigger=CronTrigger.from_crontab(cfg.nightly.schedule))
