"""
Email sending via Gmail SMTP.

IMPORTANT DEPLOYMENT CAVEAT: outbound SMTP (ports 25/465/587) is blocked at
the network level on Render and most free-tier PaaS hosts, to stop the
platform being used for spam. If this app is deployed somewhere like that,
every send will fail with something like "[Errno 101] Network is
unreachable" -- that's the platform blocking the connection, not Gmail
rejecting anything, and no code or credential change fixes it there. This
works when running locally or on a host that allows outbound SMTP.

Setup:
  1. Turn on 2-Step Verification on the Gmail account.
  2. Generate an App Password at https://myaccount.google.com/apppasswords
     (NOT the normal Gmail password -- that will not work).
  3. Set the address + app password either in Settings > Notifications, or
     via env vars (see below).

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: gmail_address, gmail_app_password,
     gmail_sender_name) -- set from the Settings > Notifications page in-app.
  2. Environment variables (.env): GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
     GMAIL_SENDER_NAME.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

Every send attempt (success or failure) is written to the `email_log` table
and to the Python logger, so failures are never silent -- check Settings >
Notifications > Recent Email Activity, or the server console/log file.
"""
import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from core.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.mailer')

SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587


def _setting(key, default=None):
    try:
        row = get_db().execute(
            "SELECT value FROM company_settings WHERE key = %s", (key,)
        ).fetchone()
        if row and row['value']:
            return row['value']
    except Exception:
        pass
    return default


def get_mail_config():
    """Resolve Gmail SMTP credentials: DB settings first, then env vars."""
    address = _setting('gmail_address') or os.getenv('GMAIL_ADDRESS')
    app_password = _setting('gmail_app_password') or os.getenv('GMAIL_APP_PASSWORD')
    sender_name = _setting('gmail_sender_name') or os.getenv('GMAIL_SENDER_NAME') or 'Jodala Microfinance'
    enabled = _setting('email_notifications_enabled', '1') != '0'
    return {
        'address': (address or '').strip(),
        'app_password': (app_password or '').strip(),
        'sender_name': sender_name,
        'enabled': enabled,
        # Kept for backward compatibility with any code checking cfg['from_email']
        'from_email': (address or '').strip(),
    }


def is_configured():
    cfg = get_mail_config()
    return bool(cfg['address'] and cfg['app_password'])


def _log_attempt(to_email, subject, status, error=None):
    """Persist every attempt to email_log so failures are visible in the UI,
    and keep the table from growing unbounded."""
    try:
        execute(
            "INSERT INTO email_log (recipient, subject, status, error, created_at) VALUES (%s, %s, %s, %s, %s)",
            (to_email, subject, status, error, utcnow())
        )
        execute(
            """DELETE FROM email_log WHERE id NOT IN (
                   SELECT id FROM email_log ORDER BY id DESC LIMIT 200)"""
        )
    except Exception:
        logger.exception("Failed to write email_log row")


def _build_message(cfg, to_email, subject, body_text, body_html):
    msg = MIMEMultipart('alternative')
    msg['From'] = formataddr((cfg['sender_name'], cfg['address']))
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html'))
    return msg


def _try_send(cfg, msg, to_email):
    """Connect to Gmail's SMTP server and send. Returns (success, error)."""
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg['address'], cfg['app_password'])
            server.sendmail(cfg['address'], [to_email], msg.as_string())
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, (
            'Gmail rejected the login -- check the address and App Password. '
            'Make sure 2-Step Verification is on and you generated an App '
            'Password at myaccount.google.com/apppasswords (not your normal password).'
        )
    except (OSError, smtplib.SMTPException) as e:
        # Covers "Network is unreachable" style errors, which usually mean
        # outbound SMTP is blocked at the platform level (see module docstring).
        return False, f'Could not send via Gmail SMTP: {e}'


def send_email(to_email, subject, body_text, body_html=None):
    """
    Send a single email via Gmail SMTP. Returns (success: bool, error: str|None).
    Never raises -- callers should not have a notification failure break the
    calling request. Every attempt is logged (see email_log table / server log).
    """
    if not to_email:
        logger.info("Skipped email '%s': no recipient address on file", subject)
        return False, 'No recipient email address'

    cfg = get_mail_config()
    if not cfg['enabled']:
        logger.info("Skipped email '%s' to %s: notifications disabled in Settings", subject, to_email)
        _log_attempt(to_email, subject, 'skipped', 'Email notifications disabled')
        return False, 'Email notifications disabled'
    if not cfg['address'] or not cfg['app_password']:
        logger.warning("Skipped email '%s' to %s: Gmail credentials not configured", subject, to_email)
        _log_attempt(to_email, subject, 'skipped', 'Gmail address / app password not configured')
        return False, 'Gmail address / app password not configured'

    msg = _build_message(cfg, to_email, subject, body_text, body_html)
    ok, error = _try_send(cfg, msg, to_email)

    if ok:
        logger.info("Sent email '%s' to %s", subject, to_email)
        _log_attempt(to_email, subject, 'sent')
    else:
        logger.error("Failed to send email '%s' to %s: %s", subject, to_email, error)
        _log_attempt(to_email, subject, 'failed', error)

    return ok, error


def send_email_async(to_email, subject, body_text, body_html=None):
    """Fire-and-forget version so email sending never blocks/breaks a request.
    Errors still get logged to email_log + the server log -- check those if
    an expected email never shows up."""
    from flask import current_app
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                send_email(to_email, subject, body_text, body_html)
            except Exception:
                logger.exception("Unhandled error sending email '%s' to %s", subject, to_email)

    threading.Thread(target=_run, daemon=True).start()


def get_recent_email_log(limit=25):
    rows = get_db().execute(
        "SELECT recipient, subject, status, error, created_at FROM email_log ORDER BY id DESC LIMIT %s",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
