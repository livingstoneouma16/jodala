"""
Paywave Express STK Push integration (https://paywavexpress.co.ke).

Paywave Express is a hosted aggregator that sits in front of Safaricom's
Daraja API -- rather than talking to Daraja directly (which is what
core/mpesa.py does), this module talks to Paywave Express's own REST API,
which handles the Daraja complexity for us in exchange for an api_key/email
pair and a small monthly subscription.

Credentials come from the same two-tier pattern as core/mpesa.py:
  1. The `company_settings` DB table (keys: paywave_api_key, paywave_email,
     paywave_enabled) -- set from Settings > Payment Gateway in-app.
  2. Environment variables (.env): PAYWAVE_API_KEY, PAYWAVE_EMAIL.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying. There is no sandbox/production split for Paywave
Express the way Daraja has one -- a single account and api_key covers both,
gated instead by whether the linked M-Pesa account (Till/Paybill/Bank) is
itself in test mode on Paywave's dashboard.

Every push attempt is written to the shared `mpesa_transactions` table
(tagged gateway='paywave'), so the Settings > Payment Gateway activity log,
the frontend status-polling endpoint, and the repayment/deposit application
logic all work identically no matter which gateway sent the push.
"""
import logging
import os

import requests

from core.database import get_db

logger = logging.getLogger('jodala.paywave')

BASE_URL = 'https://paywavexpress.co.ke'


class PaywaveError(Exception):
    """Raised for any failure talking to Paywave Express (auth, network, or
    a non-success response), so callers can show one clean error message."""
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


def get_paywave_config():
    """Resolve Paywave Express credentials: DB settings first, then env
    vars."""
    api_key = _setting('paywave_api_key') or os.getenv('PAYWAVE_API_KEY')
    email = _setting('paywave_email') or os.getenv('PAYWAVE_EMAIL')
    enabled = _setting('paywave_enabled', '1') != '0'

    return {
        'base_url': BASE_URL,
        'api_key': (api_key or '').strip(),
        'email': (email or '').strip(),
        'enabled': enabled,
    }


def is_configured():
    cfg = get_paywave_config()
    return bool(cfg['api_key'] and cfg['email'])


def normalize_phone(phone):
    """Normalize a Kenyan phone number the same way core.mpesa does.
    Paywave Express's own docs accept either 07XXXXXXXX or 2547XXXXXXXX, but
    we standardize on 2547XXXXXXXX everywhere else in the app so the
    mpesa_transactions.phone column stays consistent regardless of gateway."""
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith('254') and len(digits) == 12:
        return digits
    if digits.startswith('0') and len(digits) == 10:
        return '254' + digits[1:]
    if digits.startswith('7') and len(digits) == 9:
        return '254' + digits
    if digits.startswith('1') and len(digits) == 9:
        return '254' + digits
    raise PaywaveError(f'"{phone}" does not look like a valid Kenyan phone number')


def initiate_stk_push(phone, amount, reference, account_number=None):
    """Trigger an M-Pesa STK Push prompt via Paywave Express. Returns the
    parsed response dict (contains transaction_request_id/
    CheckoutRequestID/MerchantRequestID) on success. Raises PaywaveError on
    any failure. `account_number` is only meaningful if the linked account
    on the Paywave dashboard is a Paybill -- harmless to omit otherwise."""
    cfg = get_paywave_config()
    if not is_configured():
        raise PaywaveError('Paywave Express is not configured yet -- set it up under Settings > Payment Gateway')
    if not cfg['enabled']:
        raise PaywaveError('Paywave Express payments are currently disabled in Settings')

    phone = normalize_phone(phone)
    amount = int(round(float(amount)))  # Paywave forwards this straight to Daraja, which requires whole shillings
    if amount <= 0:
        raise PaywaveError('Amount must be positive')

    payload = {
        'api_key': cfg['api_key'],
        'email': cfg['email'],
        'amount': str(amount),
        'msisdn': phone,
        'reference': str(reference)[:100],
    }
    if account_number:
        payload['account_number'] = str(account_number)[:20]

    try:
        resp = requests.post(f"{cfg['base_url']}/v1/stkpush", json=payload, timeout=20)
    except requests.RequestException as e:
        raise PaywaveError(f'Could not reach Paywave Express ({e})')

    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200 or str(body.get('ResponseCode')) != '0':
        error_msg = body.get('errorMessage') or body.get('message') or f'HTTP {resp.status_code}'
        raise PaywaveError(f'STK push was rejected by Paywave Express: {error_msg}')

    return body


def check_transaction_status(transaction_request_id):
    """Query Paywave Express directly for a transaction's current status --
    used as a manual "reconcile" fallback (Settings activity log / a stuck
    pending row) when the webhook hasn't arrived. Returns the parsed
    response dict. Raises PaywaveError on failure."""
    cfg = get_paywave_config()
    if not is_configured():
        raise PaywaveError('Paywave Express is not configured yet -- set it up under Settings > Payment Gateway')

    payload = {
        'api_key': cfg['api_key'],
        'email': cfg['email'],
        'transaction_request_id': transaction_request_id,
    }
    try:
        resp = requests.post(f"{cfg['base_url']}/v1/tstatus", json=payload, timeout=20)
    except requests.RequestException as e:
        raise PaywaveError(f'Could not reach Paywave Express ({e})')

    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200:
        error_msg = body.get('errorMessage') or body.get('message') or f'HTTP {resp.status_code}'
        raise PaywaveError(f'Could not check transaction status: {error_msg}')

    return body
