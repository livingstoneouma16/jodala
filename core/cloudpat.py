"""
CloudPat STK Push integration -- a switchable alternative to Safaricom's
Daraja API (core/mpesa.py) for triggering a mobile-money payment prompt on a
customer's phone.

This module intentionally mirrors core/mpesa.py's shape (config resolution
order, error handling, normalize_phone, etc.) so the two gateways are true
drop-in alternatives for the routes in core/routes/mpesa.py -- see
`get_active_gateway()` / `is_gateway_configured()` there for how the app
picks between them.

Credentials can come from either source, checked in this order (same
pattern as core/mpesa.py and core/mailer.py):
  1. The `company_settings` DB table (keys: cloudpat_api_key,
     cloudpat_api_secret, cloudpat_till_number, cloudpat_environment) --
     set from Settings > Payment Gateway in-app.
  2. Environment variables (.env): CLOUDPAT_API_KEY, CLOUDPAT_API_SECRET,
     CLOUDPAT_TILL_NUMBER, CLOUDPAT_ENVIRONMENT.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

Unlike Daraja, CloudPat has no publicly documented shared sandbox
credentials, so there is no built-in "just works" default here -- an admin
must enter at least a sandbox API key/secret under Settings before CloudPat
pushes will work, even for testing.

Every push attempt is written to the same `mpesa_transactions` table used by
the M-Pesa integration (disambiguated by its `gateway` column), so the
Settings activity log and the frontend polling endpoint work identically
regardless of which gateway sent the push.
"""
import logging
import os

import requests

from core.database import get_db

logger = logging.getLogger('jodala.cloudpat')

SANDBOX_BASE_URL = 'https://sandbox.cloudpat.io/v1'
PRODUCTION_BASE_URL = 'https://api.cloudpat.io/v1'


class CloudPatError(Exception):
    """Raised for any failure talking to CloudPat (auth, network, or a
    non-success response), so callers can show one clean error message."""
    pass


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


def get_cloudpat_config():
    """Resolve CloudPat credentials: DB settings first, then env vars.
    No public sandbox default exists for CloudPat (unlike Daraja), so
    is_configured() will be False until an admin sets these."""
    environment = (_setting('cloudpat_environment') or os.getenv('CLOUDPAT_ENVIRONMENT') or 'sandbox').strip().lower()
    is_sandbox = environment != 'production'

    api_key = _setting('cloudpat_api_key') or os.getenv('CLOUDPAT_API_KEY')
    api_secret = _setting('cloudpat_api_secret') or os.getenv('CLOUDPAT_API_SECRET')
    till_number = _setting('cloudpat_till_number') or os.getenv('CLOUDPAT_TILL_NUMBER')
    enabled = _setting('cloudpat_enabled', '1') != '0'

    return {
        'environment': 'production' if not is_sandbox else 'sandbox',
        'is_sandbox': is_sandbox,
        'base_url': PRODUCTION_BASE_URL if not is_sandbox else SANDBOX_BASE_URL,
        'api_key': (api_key or '').strip(),
        'api_secret': (api_secret or '').strip(),
        'till_number': (till_number or '').strip(),
        'enabled': enabled,
    }


def is_configured():
    cfg = get_cloudpat_config()
    return bool(cfg['api_key'] and cfg['api_secret'] and cfg['till_number'])


def _get_access_token(cfg):
    url = f"{cfg['base_url']}/oauth/token"
    try:
        resp = requests.post(
            url,
            json={'api_key': cfg['api_key'], 'api_secret': cfg['api_secret']},
            timeout=15,
        )
    except requests.RequestException as e:
        raise CloudPatError(f'Could not reach CloudPat ({e})')

    if resp.status_code != 200:
        raise CloudPatError(f'Authentication with CloudPat failed ({resp.status_code})')

    try:
        return resp.json()['access_token']
    except (ValueError, KeyError):
        raise CloudPatError('Unexpected response from CloudPat during authentication')


def normalize_phone(phone):
    """Normalize a Kenyan phone number to CloudPat's required 2547XXXXXXXX /
    2541XXXXXXXX format. Accepts 07.., 01.., +2547.., 2547.. -- identical
    rules to core.mpesa.normalize_phone so callers get consistent behavior
    regardless of the active gateway."""
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith('254') and len(digits) == 12:
        return digits
    if digits.startswith('0') and len(digits) == 10:
        return '254' + digits[1:]
    if digits.startswith('7') and len(digits) == 9:
        return '254' + digits
    if digits.startswith('1') and len(digits) == 9:
        return '254' + digits
    raise CloudPatError(f'"{phone}" does not look like a valid Kenyan phone number')


def initiate_stk_push(phone, amount, account_reference, transaction_desc, callback_url):
    """Trigger an STK Push prompt on the customer's phone via CloudPat.
    Returns a dict containing at least `checkout_request_id` on success (the
    routes layer stores this in the same `checkout_request_id` column used
    for Daraja pushes). Raises CloudPatError on any failure."""
    cfg = get_cloudpat_config()
    if not is_configured():
        raise CloudPatError('CloudPat is not configured yet -- set it up under Settings > Payment Gateway')
    if not cfg['enabled']:
        raise CloudPatError('CloudPat payments are currently disabled in Settings')

    phone = normalize_phone(phone)
    amount = int(round(float(amount)))
    if amount <= 0:
        raise CloudPatError('Amount must be positive')

    token = _get_access_token(cfg)

    payload = {
        'till_number': cfg['till_number'],
        'amount': amount,
        'phone_number': phone,
        'account_reference': str(account_reference)[:12],
        'description': str(transaction_desc)[:13],
        'callback_url': callback_url,
    }

    try:
        resp = requests.post(
            f"{cfg['base_url']}/stkpush",
            json=payload,
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
    except requests.RequestException as e:
        raise CloudPatError(f'Could not reach CloudPat ({e})')

    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200 or not body.get('success', resp.status_code == 200):
        error_msg = body.get('error') or body.get('message') or f'HTTP {resp.status_code}'
        raise CloudPatError(f'STK push was rejected by CloudPat: {error_msg}')

    return {
        'checkout_request_id': body.get('checkout_request_id') or body.get('reference'),
        'merchant_request_id': body.get('merchant_request_id'),
        'raw': body,
    }


def get_recent_cloudpat_log(limit=25):
    rows = get_db().execute(
        "SELECT * FROM mpesa_transactions WHERE gateway = 'cloudpat' ORDER BY created_at DESC LIMIT %s", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
