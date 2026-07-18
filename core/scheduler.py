"""
In-process daily scheduler for overdue-loan reminder emails.

`send_overdue_reminders.py` (repo root) does the same job but relies on an
*external* scheduler (cron, Windows Task Scheduler, a systemd timer, a
platform "Cron Job" service) to actually invoke it -- and none of Render,
Fly.io, Railway, or docker-compose are configured to do that here, so it
was silently never running. Rather than pick one platform's cron mechanism
(which the other three wouldn't get), this runs the same job as a
background thread inside the web process itself via APScheduler, so it
works identically no matter where/how this app is deployed.

Started once from app.py (see the guard there for why). Safe to disable
with ENABLE_OVERDUE_SCHEDULER=false if you'd rather drive
send_overdue_reminders.py from your own external cron instead.
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger('jodala')

_scheduler = None


def start_scheduler(app):
    """Idempotently start a background job that runs send_overdue_reminders
    once a day. No-op if ENABLE_OVERDUE_SCHEDULER=false or if already
    started (guards against being called twice in the same process)."""
    global _scheduler

    if os.getenv('ENABLE_OVERDUE_SCHEDULER', 'true').strip().lower() not in ('1', 'true', 'yes'):
        logger.info('Overdue-reminder scheduler disabled via ENABLE_OVERDUE_SCHEDULER.')
        return None

    if _scheduler is not None:
        return _scheduler

    hour = int(os.getenv('OVERDUE_REMINDER_HOUR', '8'))
    minute = int(os.getenv('OVERDUE_REMINDER_MINUTE', '0'))

    def _run_job():
        # Runs on the scheduler's own background thread, so it needs its
        # own app context (there's no request in flight to piggyback on).
        with app.app_context():
            from core.routes.loans import send_overdue_reminders
            try:
                result = send_overdue_reminders()
                logger.info('Overdue reminders (scheduled): %s', result)
            except Exception:
                # A failed run (e.g. transient DB/SMTP hiccup) should never
                # take down the scheduler thread -- it'll just try again at
                # the next scheduled time tomorrow.
                logger.exception('Scheduled overdue-reminder run failed')

    _scheduler = BackgroundScheduler(daemon=True, timezone=os.getenv('SCHEDULER_TIMEZONE', 'UTC'))
    _scheduler.add_job(
        _run_job,
        trigger=CronTrigger(hour=hour, minute=minute),
        id='overdue_loan_reminders',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info('Overdue-reminder scheduler started (daily at %02d:%02d %s).',
                hour, minute, os.getenv('SCHEDULER_TIMEZONE', 'UTC'))
    return _scheduler
