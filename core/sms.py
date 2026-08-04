"""
SMS sending via TextSMS (sms.textsms.co.ke).

TextSMS is used (rather than Twilio or another global provider) because it
has direct, low-cost local routes to Safaricom/Airtel/Telkom numbers in
Kenya -- where this app's members and clients are -- and pairs naturally
with the M-Pesa integration already in core/mpesa.py.

Setup:
  1. Create an account at https://sms.textsms.co.ke.
  2. Grab the API Key and Partner ID from the dashboard.
  3. Set them either in Settings > Notifications, or via env vars (see
     below). Optionally set a registered Sender ID / Shortcode; leave
     blank to send from TextSMS's default shared shortcode.

Credentials can come from either source, checked in this order:
  1. The `company_settings` DB table (keys: textsms_api_key,
     textsms_partner_id, textsms_sender_id) -- set from the Settings >
     Notifications page in-app.
  2. Environment variables (.env): TEXTSMS_API_KEY, TEXTSMS_PARTNER_ID,
     TEXTSMS_SENDER_ID.

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

# TextSMS live endpoint.
TEXTSMS_URL = 'https://sms.textsms.co.ke/api/services/sendsms/'


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
    """Resolve TextSMS credentials: DB settings first, then env vars."""
    api_key = _setting('textsms_api_key') or os.getenv('TEXTSMS_API_KEY')
    partner_id = _setting('textsms_partner_id') or os.getenv('TEXTSMS_PARTNER_ID')
    sender_id = _setting('textsms_sender_id') or os.getenv('TEXTSMS_SENDER_ID') or ''
    db_enabled = _setting('sms_notifications_enabled')
    if db_enabled is not None:
        enabled = db_enabled == '1'
    else:
        enabled = (os.getenv('SMS_NOTIFICATIONS_ENABLED', 'false') or '').strip().lower() in ('1', 'true', 'yes')
    return {
        'api_key': (api_key or '').strip(),
        'partner_id': (partner_id or '').strip(),
        'sender_id': (sender_id or '').strip(),
        'enabled': enabled,
    }


def is_configured():
    cfg = get_sms_config()
    return bool(cfg['api_key'] and cfg['partner_id'])


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
    format TextSMS expects. Returns None if it doesn't look like a usable
    number."""
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit() or c == '+')
    digits = digits.lstrip('+')
    if digits.startswith('254') and len(digits) == 12:
        return digits
    if digits.startswith('0') and len(digits) == 10:
        return f'254{digits[1:]}'
    if digits.startswith('7') and len(digits) == 9:
        return f'254{digits}'
    if digits.startswith('1') and len(digits) == 9:
        return f'254{digits}'
    if digits.startswith('254'):
        return digits
    return None


def _try_send(cfg, to_phone, message):
    """POST to TextSMS. Returns (success, error). TextSMS numbers are sent
    without the leading '+' (e.g. 2547XXXXXXXX)."""
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    payload = {
        'apikey': cfg['api_key'],
        'partnerID': cfg['partner_id'],
        'mobile': to_phone,
        'message': message,
        'shortcode': cfg['sender_id'] or 'TextSMS',
    }

    try:
        resp = requests.post(TEXTSMS_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f'Could not reach TextSMS: {e}'

    if resp.status_code != 200:
        return False, f'TextSMS returned HTTP {resp.status_code}: {resp.text[:200]}'

    try:
        data = resp.json()
    except ValueError:
        return False, f'Unexpected response from TextSMS: {resp.text[:200]}'

    responses = data.get('responses') if isinstance(data, dict) else None
    if not responses:
        return False, f'TextSMS accepted the request but returned no recipient status: {resp.text[:200]}'

    result = responses[0]
    # TextSMS uses response-code 200 for a successfully queued message.
    if str(result.get('respose-code', result.get('response-code', ''))) == '200':
        return True, None
    return False, result.get('response-description') or 'Unknown failure'


def send_sms(to_phone, message):
    """
    Send a single SMS via TextSMS. Returns (success: bool, error: str|None).
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
    if not cfg['api_key'] or not cfg['partner_id']:
        logger.warning("Skipped SMS to %s: TextSMS credentials not configured", normalized)
        _log_attempt(normalized, message, 'skipped', "TextSMS API key / Partner ID not configured")
        return False, "TextSMS API key / Partner ID not configured"

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
