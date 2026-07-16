"""
Email sending via Gmail SMTP.

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: gmail_address, gmail_app_password,
     gmail_sender_name) -- set from the Settings > Notifications page in-app.
  2. Environment variables (.env): GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
     GMAIL_SENDER_NAME.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying. Uses a Google Account "App Password" (not the regular
account password -- Google requires 2-Step Verification to be on and an
App Password generated at https://myaccount.google.com/apppasswords).

Every send attempt (success or failure) is written to the `email_log` table
and to the Python logger, so failures are never silent -- check Settings >
Notifications > Recent Email Activity, or the server console/log file.
"""
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

from core.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.mailer')


def _setting(key, default=None):
    try:
        row = get_db().execute(
            "SELECT value FROM company_settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row['value']:
            return row['value']
    except Exception:
        pass
    return default


def get_mail_config():
    """Resolve Gmail credentials: DB settings first, then env vars."""
    address = _setting('gmail_address') or os.getenv('GMAIL_ADDRESS')
    app_password = _setting('gmail_app_password') or os.getenv('GMAIL_APP_PASSWORD')
    sender_name = _setting('gmail_sender_name') or os.getenv('GMAIL_SENDER_NAME') or 'Jodala Microfinance'
    enabled = _setting('email_notifications_enabled', '1') != '0'
    return {
        'address': (address or '').strip(),
        'app_password': (app_password or '').strip().replace(' ', ''),
        'sender_name': sender_name,
        'enabled': enabled,
    }


def is_configured():
    cfg = get_mail_config()
    return bool(cfg['address'] and cfg['app_password'])


def _log_attempt(to_email, subject, status, error=None):
    """Persist every attempt to email_log so failures are visible in the UI,
    and keep the table from growing unbounded."""
    try:
        execute(
            "INSERT INTO email_log (recipient, subject, status, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (to_email, subject, status, error, utcnow())
        )
        execute(
            """DELETE FROM email_log WHERE id NOT IN (
                   SELECT id FROM email_log ORDER BY id DESC LIMIT 200)"""
        )
    except Exception:
        logger.exception("Failed to write email_log row")


def _build_message(cfg, to_email, subject, body_text, body_html):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"{cfg['sender_name']} <{cfg['address']}>"
    msg['To'] = to_email
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype='html')
    return msg


def _try_send(cfg, msg):
    """Attempt delivery over SSL:465 first, falling back to STARTTLS:587
    if the connection itself fails (some networks/ISPs/firewalls block one
    or the other). Returns (success, error)."""
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context, timeout=15) as server:
            server.login(cfg['address'], cfg['app_password'])
            server.send_message(msg)
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        # Auth failures won't be fixed by switching ports -- stop here.
        return False, f'Gmail rejected the credentials (check address / App Password): {e}'
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        logger.warning("SMTP_SSL:465 failed (%s), retrying via STARTTLS:587", e)
        try:
            with smtplib.SMTP('smtp.gmail.com', 587, timeout=15) as server:
                server.starttls(context=context)
                server.login(cfg['address'], cfg['app_password'])
                server.send_message(msg)
            return True, None
        except smtplib.SMTPAuthenticationError as e2:
            return False, f'Gmail rejected the credentials (check address / App Password): {e2}'
        except Exception as e2:
            return False, f'Could not reach Gmail on ports 465 or 587: {e2}'


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
        _log_attempt(to_email, subject, 'skipped', 'Gmail credentials not configured')
        return False, 'Gmail credentials not configured'

    msg = _build_message(cfg, to_email, subject, body_text, body_html)
    ok, error = _try_send(cfg, msg)

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
        "SELECT recipient, subject, status, error, created_at FROM email_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
