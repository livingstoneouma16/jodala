"""
Email sending via the Gmail API (OAuth2), using an app-scoped account.

Plain Gmail SMTP doesn't work on Render, Fly.io, or most free/cheap PaaS
hosts -- they block outbound SMTP ports (25/465/587) at the network level
to stop the platform being used for spam, so every send would fail with
something like "[Errno 101] Network is unreachable" no matter what
credentials were configured. The Gmail *API* sends over plain HTTPS
(port 443, same as any other API call this app already makes), which
isn't blocked anywhere Flask itself can run.

Setup (one-time, per Google account you want to send from):
  1. In Google Cloud Console, create/select a project and enable the
     "Gmail API" (APIs & Services > Library).
  2. Configure the OAuth consent screen (External is fine; add the sending
     Gmail address as a Test user if the app stays in "Testing" mode).
  3. Create an OAuth Client ID (Application type: Desktop app) under
     APIs & Services > Credentials. Note the Client ID + Client Secret.
  4. Generate a refresh token for the sending account with the
     `https://www.googleapis.com/auth/gmail.send` scope -- easiest way is
     Google's OAuth Playground (https://developers.google.com/oauthplayground):
       a. Click the gear icon > check "Use your own OAuth credentials" >
          paste your Client ID + Secret.
       b. In Step 1, find "Gmail API v1" > select
          "https://www.googleapis.com/auth/gmail.send" > Authorize APIs.
       c. Sign in as the Gmail account that should send the mail.
       d. In Step 2, click "Exchange authorization code for tokens" and
          copy the Refresh token shown.
  5. Set the four values below either in Settings > Notifications, or via
     env vars (see below).

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: gmail_client_id,
     gmail_client_secret, gmail_refresh_token, gmail_from_email,
     gmail_sender_name) -- set from the Settings > Notifications page
     in-app.
  2. Environment variables (.env): GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET,
     GMAIL_REFRESH_TOKEN, GMAIL_FROM_EMAIL, GMAIL_SENDER_NAME.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

A refresh token doesn't expire from time passing (only from being
revoked, unused for 6 months, or the OAuth consent screen being in
"Testing" mode for >7 days without publishing it), so this only needs to
be done once per sending account -- unlike the short-lived access token,
which this module exchanges for automatically on every send and never
persists.

Every send attempt (success or failure) is written to the `email_log`
table and to the Python logger, so failures are never silent -- check
Settings > Notifications > Recent Email Activity, or the server
console/log file.
"""
import base64
import logging
import os
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from core.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.mailer')

GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GMAIL_SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'

# Cache the exchanged access token in-process (per gunicorn worker) so we
# don't hit the token endpoint on every single email -- Google access
# tokens are valid for ~1 hour. Keyed by refresh token so a credential
# rotation in Settings invalidates the cache automatically.
_token_cache = {}
_token_cache_lock = threading.Lock()


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
    """Resolve Gmail API OAuth credentials: DB settings first, then env vars."""
    client_id = _setting('gmail_client_id') or os.getenv('GMAIL_CLIENT_ID')
    client_secret = _setting('gmail_client_secret') or os.getenv('GMAIL_CLIENT_SECRET')
    refresh_token = _setting('gmail_refresh_token') or os.getenv('GMAIL_REFRESH_TOKEN')
    from_email = _setting('gmail_from_email') or os.getenv('GMAIL_FROM_EMAIL')
    sender_name = _setting('gmail_sender_name') or os.getenv('GMAIL_SENDER_NAME') or 'Jodala Microfinance'
    enabled = _setting('email_notifications_enabled', '1') != '0'
    return {
        'client_id': (client_id or '').strip(),
        'client_secret': (client_secret or '').strip(),
        'refresh_token': (refresh_token or '').strip(),
        'from_email': (from_email or '').strip(),
        'sender_name': sender_name,
        'enabled': enabled,
    }


def is_configured():
    cfg = get_mail_config()
    return bool(cfg['client_id'] and cfg['client_secret'] and cfg['refresh_token'] and cfg['from_email'])


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


def _get_access_token(cfg):
    """Exchange the long-lived refresh token for a short-lived access token,
    reusing a cached one until shortly before it expires. Returns
    (access_token, error)."""
    cache_key = cfg['refresh_token']

    with _token_cache_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached['expires_at'] > time.time() + 30:
            return cached['access_token'], None

    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            'client_id': cfg['client_id'],
            'client_secret': cfg['client_secret'],
            'refresh_token': cfg['refresh_token'],
            'grant_type': 'refresh_token',
        }, timeout=15)
    except requests.RequestException as e:
        return None, f'Could not reach Google to refresh the access token: {e}'

    if resp.status_code != 200:
        try:
            message = resp.json().get('error_description') or resp.text[:200]
        except ValueError:
            message = resp.text[:200]
        if resp.status_code in (400, 401):
            return None, (
                'Google rejected the Gmail credentials -- check the Client ID/Secret and '
                f'that the Refresh Token hasn\'t been revoked, under Settings > Notifications. ({message})'
            )
        return None, f'Google token endpoint returned HTTP {resp.status_code}: {message}'

    data = resp.json()
    access_token = data.get('access_token')
    expires_in = data.get('expires_in', 3600)
    if not access_token:
        return None, 'Google token response did not include an access token'

    with _token_cache_lock:
        _token_cache[cache_key] = {
            'access_token': access_token,
            'expires_at': time.time() + expires_in,
        }

    return access_token, None


def _build_raw_message(cfg, to_email, subject, body_text, body_html):
    """Build an RFC 2822 message and base64url-encode it the way the
    Gmail API's messages.send endpoint requires."""
    msg = MIMEMultipart('alternative')
    msg['To'] = to_email
    msg['From'] = f"{cfg['sender_name']} <{cfg['from_email']}>"
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html'))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')


def _try_send(cfg, to_email, subject, body_text, body_html):
    """POST to the Gmail API. Returns (success, error)."""
    access_token, error = _get_access_token(cfg)
    if not access_token:
        return False, error

    headers = {
        'Authorization': f"Bearer {access_token}",
        'Content-Type': 'application/json',
    }
    payload = {'raw': _build_raw_message(cfg, to_email, subject, body_text, body_html)}

    try:
        resp = requests.post(GMAIL_SEND_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f'Could not reach Gmail: {e}'

    if resp.status_code in (200, 201):
        return True, None

    try:
        data = resp.json()
        message = data.get('error', {}).get('message') or resp.text[:200]
    except ValueError:
        message = resp.text[:200]

    if resp.status_code in (401, 403):
        return False, (
            'Gmail rejected the request -- the refresh token may have been revoked or '
            f'lack the gmail.send scope. Re-check under Settings > Notifications. ({message})'
        )
    if resp.status_code == 400:
        return False, f'Gmail rejected the message: {message}'
    return False, f'Gmail returned HTTP {resp.status_code}: {message}'


def send_email(to_email, subject, body_text, body_html=None):
    """
    Send a single email via the Gmail API. Returns
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
    if not is_configured():
        logger.warning("Skipped email '%s' to %s: Gmail API credentials not configured", subject, to_email)
        _log_attempt(to_email, subject, 'skipped', 'Gmail API credentials not fully configured')
        return False, 'Gmail API credentials not fully configured'

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
