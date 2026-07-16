"""
Stand-alone script to email overdue-loan reminders. Intended to be run
once a day by cron (or Windows Task Scheduler / systemd timer), separately
from the web app process, e.g.:

    # crontab -e, run every day at 8am server time
    0 8 * * * cd /path/to/jodala && /path/to/venv/bin/python send_overdue_reminders.py >> /var/log/jodala_reminders.log 2>&1

Requires the same .env (or configured Gmail settings in the DB) as the web
app, since it uses the same app factory / DB / mailer.
"""
from app import create_app
from app.routes.loans import send_overdue_reminders

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        result = send_overdue_reminders()
        print(f"Overdue reminders: {result}")
