"""
SMS sending via Africa's Talking.

Africa's Talking is used (rather than Twilio or another global provider)
because it has direct, low-cost local routes to Safaricom/Airtel/Telkom
numbers in Kenya -- where this app's members and clients are -- and pairs
naturally with the M-Pesa integration already in core/mpesa.py.

Setup:
  1. Create an account at https://africastalking.com (a free sandbox
     account works for testing -- sandbox messages aren't actually
     delivered to real phones but the API behaves identically).
  2. Grab the Username and API Key from the dashboard.
  3. Set them either in Settings > Notifications, or via env vars (see
     below). Optionally set a registered Sender ID / Short Code; leave
     blank to send from Africa's Talking's shared alphanumeric ID.

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: at_username, at_api_key,
     at_sender_id) -- set from the Settings > Notifications page in-app.
  2. Environment variables (.env): AT_USERNAME, AT_API_KEY, AT_SENDER_ID.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

Every send attempt (success or failure) is written to the `sms_log` table
and to the Python logger, so failures are never silent -- check Settings >
Notifications > Recent SMS Activity, or the server console/log file.
"""
import logging
import os
import threading

import requests

from core.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.sms')

# Africa's Talking live endpoint. Sandbox apps (username 'sandbox') are
# routed automatically by AT based on the username, not the URL.
AT_URL = 'https://api.africastalking.com/version1/messaging'


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


def get_sms_config():
    """Resolve Africa's Talking credentials: DB settings first, then env vars."""
    username = _setting('at_username') or os.getenv('AT_USERNAME')
    api_key = _setting('at_api_key') or os.getenv('AT_API_KEY')
    sender_id = _setting('at_sender_id') or os.getenv('AT_SENDER_ID') or ''
    db_enabled = _setting('sms_notifications_enabled')
    if db_enabled is not None:
        enabled = db_enabled == '1'
    else:
        enabled = (os.getenv('SMS_NOTIFICATIONS_ENABLED', 'false') or '').strip().lower() in ('1', 'true', 'yes')
    return {
        'username': (username or '').strip(),
        'api_key': (api_key or '').strip(),
        'sender_id': (sender_id or '').strip(),
        'enabled': enabled,
    }


def is_configured():
    cfg = get_sms_config()
    return bool(cfg['username'] and cfg['api_key'])


def _log_attempt(to_phone, message, status, error=None):
    """Persist every attempt to sms_log so failures are visible in the UI,
    and keep the table from growing unbounded."""
    try:
        execute(
            "INSERT INTO sms_log (recipient, message, status, error, created_at) VALUES (%s, %s, %s, %s, %s)",
            (to_phone, message[:500], status, error, utcnow())
        )
        execute(
            """DELETE FROM sms_log WHERE id NOT IN (
                   SELECT id FROM sms_log ORDER BY id DESC LIMIT 200)"""
        )
    except Exception:
        logger.exception("Failed to write sms_log row")


def normalize_phone(phone):
    """Normalize a Kenyan phone number to the 2547XXXXXXXX / 2541XXXXXXXX
    format Africa's Talking expects. Returns None if it doesn't look like
    a usable number."""
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit() or c == '+')
    digits = digits.lstrip('+')
    if digits.startswith('254') and len(digits) == 12:
        return f'+{digits}'
    if digits.startswith('0') and len(digits) == 10:
        return f'+254{digits[1:]}'
    if digits.startswith('7') and len(digits) == 9:
        return f'+254{digits}'
    if digits.startswith('1') and len(digits) == 9:
        return f'+254{digits}'
    if digits.startswith('254'):
        return f'+{digits}'
    return None


def _try_send(cfg, to_phone, message):
    """POST to Africa's Talking. Returns (success, error)."""
    headers = {
        'apiKey': cfg['api_key'],
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
    }
    payload = {
        'username': cfg['username'],
        'to': to_phone,
        'message': message,
    }
    if cfg['sender_id']:
        payload['from'] = cfg['sender_id']

    try:
        resp = requests.post(AT_URL, data=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f'Could not reach Africa\'s Talking: {e}'

    if resp.status_code != 201:
        return False, f'Africa\'s Talking returned HTTP {resp.status_code}: {resp.text[:200]}'

    try:
        data = resp.json()
        recipients = data.get('SMSMessageData', {}).get('Recipients', [])
    except ValueError:
        return False, f'Unexpected response from Africa\'s Talking: {resp.text[:200]}'

    if not recipients:
        return False, 'Africa\'s Talking accepted the request but returned no recipient status'

    status = recipients[0].get('status', '')
    if status.lower() == 'success':
        return True, None
    return False, recipients[0].get('status', 'Unknown failure') or 'Unknown failure'


def send_sms(to_phone, message):
    """
    Send a single SMS via Africa's Talking. Returns (success: bool, error: str|None).
    Never raises -- callers should not have a notification failure break the
    calling request. Every attempt is logged (see sms_log table / server log).
    """
    normalized = normalize_phone(to_phone)
    if not normalized:
        logger.info("Skipped SMS: no usable phone number ('%s')", to_phone)
        return False, 'No usable recipient phone number'

    cfg = get_sms_config()
    if not cfg['enabled']:
        logger.info("Skipped SMS to %s: SMS notifications disabled in Settings", normalized)
        _log_attempt(normalized, message, 'skipped', 'SMS notifications disabled')
        return False, 'SMS notifications disabled'
    if not cfg['username'] or not cfg['api_key']:
        logger.warning("Skipped SMS to %s: Africa's Talking credentials not configured", normalized)
        _log_attempt(normalized, message, 'skipped', "Africa's Talking username / API key not configured")
        return False, "Africa's Talking username / API key not configured"

    ok, error = _try_send(cfg, normalized, message)

    if ok:
        logger.info("Sent SMS to %s", normalized)
        _log_attempt(normalized, message, 'sent')
    else:
        logger.error("Failed to send SMS to %s: %s", normalized, error)
        _log_attempt(normalized, message, 'failed', error)

    return ok, error


def send_sms_async(to_phone, message):
    """Fire-and-forget version so SMS sending never blocks/breaks a request.
    Errors still get logged to sms_log + the server log -- check those if
    an expected SMS never shows up."""
    from flask import current_app
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                send_sms(to_phone, message)
            except Exception:
                logger.exception("Unhandled error sending SMS to %s", to_phone)

    threading.Thread(target=_run, daemon=True).start()


def get_recent_sms_log(limit=25):
    rows = get_db().execute(
        "SELECT recipient, message, status, error, created_at FROM sms_log ORDER BY id DESC LIMIT %s",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
