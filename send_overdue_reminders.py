"""
Stand-alone script to email overdue-loan reminders.

As of core/scheduler.py, this now runs automatically once a day *inside*
the web process itself (see app.py / ENABLE_OVERDUE_REMINDER_HOUR env vars
in .env.example) -- you no longer need to schedule anything externally for
this to happen.

This script still exists for cases where you'd rather drive it from your
own external scheduler instead (cron, Windows Task Scheduler, a systemd
timer, a platform "Cron Job" service, etc.) -- e.g. if you set
ENABLE_OVERDUE_SCHEDULER=false to disable the in-process job. Example:

    # crontab -e, run every day at 8am server time
    0 8 * * * cd /path/to/jodala && /path/to/venv/bin/python send_overdue_reminders.py >> /var/log/jodala_reminders.log 2>&1

Requires the same .env (or configured Gmail settings in the DB) as the web
app, since it uses the same app factory / DB / mailer.
"""
from core import create_app
from core.routes.loans import send_overdue_reminders

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        result = send_overdue_reminders()
        print(f"Overdue reminders: {result}")
