"""
Email sending via the Resend HTTP API.

Gmail SMTP (the previous implementation) doesn't work on Render or most
free-tier PaaS hosts -- they block outbound SMTP ports (25/465/587) at the
network level to stop the platform being used for spam, so every send would
fail with something like "[Errno 101] Network is unreachable" no matter what
credentials were configured. Resend sends over plain HTTPS (port 443, same
as any other API call this app already makes), which isn't blocked anywhere
Flask itself can run -- see migration 13 in core/database.py.

Setup:
  1. Create an account at https://resend.com and verify a sending domain
     (or use their onboarding@resend.dev address for quick testing -- it
     only delivers to the account owner's own inbox).
  2. Grab an API key from https://resend.com/api-keys.
  3. Set the API key + from-address either in Settings > Notifications, or
     via env vars (see below).

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: resend_api_key,
     resend_from_email, resend_sender_name) -- set from the Settings >
     Notifications page in-app.
  2. Environment variables (.env): RESEND_API_KEY, RESEND_FROM_EMAIL,
     RESEND_SENDER_NAME.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

Every send attempt (success or failure) is written to the `email_log` table
and to the Python logger, so failures are never silent -- check Settings >
Notifications > Recent Email Activity, or the server console/log file.
"""
import logging
import os
import threading

import requests

from core.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.mailer')

RESEND_API_URL = 'https://api.resend.com/emails'


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
    """Resolve Resend credentials: DB settings first, then env vars."""
    api_key = _setting('resend_api_key') or os.getenv('RESEND_API_KEY')
    from_email = _setting('resend_from_email') or os.getenv('RESEND_FROM_EMAIL')
    sender_name = _setting('resend_sender_name') or os.getenv('RESEND_SENDER_NAME') or 'Jodala Microfinance'
    enabled = _setting('email_notifications_enabled', '1') != '0'
    return {
        'api_key': (api_key or '').strip(),
        'from_email': (from_email or '').strip(),
        'sender_name': sender_name,
        'enabled': enabled,
    }


def is_configured():
    cfg = get_mail_config()
    return bool(cfg['api_key'] and cfg['from_email'])


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


def _try_send(cfg, to_email, subject, body_text, body_html):
    """POST to the Resend HTTP API. Returns (success, error)."""
    headers = {
        'Authorization': f"Bearer {cfg['api_key']}",
        'Content-Type': 'application/json',
    }
    payload = {
        'from': f"{cfg['sender_name']} <{cfg['from_email']}>",
        'to': [to_email],
        'subject': subject,
        'text': body_text,
    }
    if body_html:
        payload['html'] = body_html

    try:
        resp = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f'Could not reach Resend: {e}'

    if resp.status_code in (200, 201):
        return True, None

    try:
        data = resp.json()
        message = data.get('message') or resp.text[:200]
    except ValueError:
        message = resp.text[:200]

    if resp.status_code in (401, 403):
        return False, f'Resend rejected the API key -- check it under Settings > Notifications. ({message})'
    if resp.status_code == 422:
        return False, f'Resend rejected the request -- check the from address is on a verified domain. ({message})'
    return False, f'Resend returned HTTP {resp.status_code}: {message}'


def send_email(to_email, subject, body_text, body_html=None):
    """
    Send a single email via the Resend HTTP API. Returns
    (success: bool, error: str|None). Never raises -- callers should not
    have a notification failure break the calling request. Every attempt is
    logged (see email_log table / server log).
    """
    if not to_email:
        logger.info("Skipped email '%s': no recipient address on file", subject)
        return False, 'No recipient email address'

    cfg = get_mail_config()
    if not cfg['enabled']:
        logger.info("Skipped email '%s' to %s: notifications disabled in Settings", subject, to_email)
        _log_attempt(to_email, subject, 'skipped', 'Email notifications disabled')
        return False, 'Email notifications disabled'
    if not cfg['api_key'] or not cfg['from_email']:
        logger.warning("Skipped email '%s' to %s: Resend credentials not configured", subject, to_email)
        _log_attempt(to_email, subject, 'skipped', 'Resend API key / from address not configured')
        return False, 'Resend API key / from address not configured'

    ok, error = _try_send(cfg, to_email, subject, body_text, body_html)

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
