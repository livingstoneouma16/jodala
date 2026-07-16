"""
M-Pesa STK Push (Lipa Na M-Pesa Online) integration via Safaricom's Daraja API.

Credentials can come from either source, checked in this order (same pattern
as app/mailer.py):
  1. The `company_settings` DB table (keys: mpesa_consumer_key,
     mpesa_consumer_secret, mpesa_shortcode, mpesa_passkey,
     mpesa_environment) -- set from Settings > M-Pesa in-app.
  2. Environment variables (.env): MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET,
     MPESA_SHORTCODE, MPESA_PASSKEY, MPESA_ENVIRONMENT.

DB settings take precedence so an admin can configure/rotate credentials
without redeploying.

Defaults to Safaricom's SANDBOX environment and the standard Daraja sandbox
test shortcode (174379) / passkey, so STK pushes work out of the box against
Safaricom's test numbers before any real credentials are entered. Switch
mpesa_environment to "production" (and supply real credentials + a publicly
reachable HTTPS callback URL) to go live.

Every push attempt is written to the `mpesa_transactions` table so the
Settings > M-Pesa page can show a live audit trail, mirroring the email log.
"""
import base64
import logging
import os
from datetime import datetime

import requests

from app.database import get_db, execute, utcnow

logger = logging.getLogger('jodala.mpesa')

SANDBOX_BASE_URL = 'https://sandbox.safaricom.co.ke'
PRODUCTION_BASE_URL = 'https://api.safaricom.co.ke'

# Safaricom's published Daraja sandbox test credentials -- these are public
# knowledge (documented at developer.safaricom.co.ke) and only work against
# the sandbox environment, so it's safe to ship them as defaults purely so
# STK Push "just works" for evaluation/testing before real credentials are
# configured.
SANDBOX_SHORTCODE = '174379'
SANDBOX_PASSKEY = 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919'


class MpesaError(Exception):
    """Raised for any failure talking to Daraja (auth, network, or a
    non-success response), so callers can show one clean error message."""
    pass


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


def get_mpesa_config():
    """Resolve M-Pesa credentials: DB settings first, then env vars, then
    the public sandbox defaults."""
    environment = (_setting('mpesa_environment') or os.getenv('MPESA_ENVIRONMENT') or 'sandbox').strip().lower()
    is_sandbox = environment != 'production'

    consumer_key = _setting('mpesa_consumer_key') or os.getenv('MPESA_CONSUMER_KEY')
    consumer_secret = _setting('mpesa_consumer_secret') or os.getenv('MPESA_CONSUMER_SECRET')
    shortcode = (_setting('mpesa_shortcode') or os.getenv('MPESA_SHORTCODE')
                 or (SANDBOX_SHORTCODE if is_sandbox else None))
    passkey = (_setting('mpesa_passkey') or os.getenv('MPESA_PASSKEY')
               or (SANDBOX_PASSKEY if is_sandbox else None))
    enabled = _setting('mpesa_enabled', '1') != '0'

    return {
        'environment': 'production' if not is_sandbox else 'sandbox',
        'is_sandbox': is_sandbox,
        'base_url': PRODUCTION_BASE_URL if not is_sandbox else SANDBOX_BASE_URL,
        'consumer_key': (consumer_key or '').strip(),
        'consumer_secret': (consumer_secret or '').strip(),
        'shortcode': (shortcode or '').strip(),
        'passkey': (passkey or '').strip(),
        'enabled': enabled,
    }


def is_configured():
    cfg = get_mpesa_config()
    return bool(cfg['consumer_key'] and cfg['consumer_secret'] and cfg['shortcode'] and cfg['passkey'])


def _get_access_token(cfg):
    url = f"{cfg['base_url']}/oauth/v1/generate?grant_type=client_credentials"
    try:
        resp = requests.get(url, auth=(cfg['consumer_key'], cfg['consumer_secret']), timeout=15)
    except requests.RequestException as e:
        raise MpesaError(f'Could not reach Safaricom ({e})')

    if resp.status_code != 200:
        raise MpesaError(f'Authentication with Safaricom failed ({resp.status_code})')

    try:
        return resp.json()['access_token']
    except (ValueError, KeyError):
        raise MpesaError('Unexpected response from Safaricom during authentication')


def normalize_phone(phone):
    """Normalize a Kenyan phone number to Daraja's required 2547XXXXXXXX /
    2541XXXXXXXX format. Accepts 07.., 01.., +2547.., 2547.. """
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith('254') and len(digits) == 12:
        return digits
    if digits.startswith('0') and len(digits) == 10:
        return '254' + digits[1:]
    if digits.startswith('7') and len(digits) == 9:
        return '254' + digits
    if digits.startswith('1') and len(digits) == 9:
        return '254' + digits
    raise MpesaError(f'"{phone}" does not look like a valid Kenyan phone number')


def initiate_stk_push(phone, amount, account_reference, transaction_desc, callback_url):
    """Trigger an STK Push prompt on the customer's phone. Returns the
    Daraja response dict (contains CheckoutRequestID/MerchantRequestID) on
    success. Raises MpesaError on any failure."""
    cfg = get_mpesa_config()
    if not is_configured():
        raise MpesaError('M-Pesa is not configured yet -- set it up under Settings > M-Pesa')
    if not cfg['enabled']:
        raise MpesaError('M-Pesa payments are currently disabled in Settings')

    phone = normalize_phone(phone)
    amount = int(round(float(amount)))  # Daraja requires a whole-shilling integer
    if amount <= 0:
        raise MpesaError('Amount must be positive')

    token = _get_access_token(cfg)

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(
        (cfg['shortcode'] + cfg['passkey'] + timestamp).encode()
    ).decode()

    payload = {
        'BusinessShortCode': cfg['shortcode'],
        'Password': password,
        'Timestamp': timestamp,
        'TransactionType': 'CustomerPayBillOnline',
        'Amount': amount,
        'PartyA': phone,
        'PartyB': cfg['shortcode'],
        'PhoneNumber': phone,
        'CallBackURL': callback_url,
        'AccountReference': str(account_reference)[:12],
        'TransactionDesc': str(transaction_desc)[:13],
    }

    try:
        resp = requests.post(
            f"{cfg['base_url']}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
    except requests.RequestException as e:
        raise MpesaError(f'Could not reach Safaricom ({e})')

    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200 or str(body.get('ResponseCode')) != '0':
        error_msg = body.get('errorMessage') or body.get('ResponseDescription') or f'HTTP {resp.status_code}'
        raise MpesaError(f'STK push was rejected by Safaricom: {error_msg}')

    return body


def get_b2c_config():
    """Resolve B2C (disbursement) credentials. Unlike STK Push, B2C has NO
    usable shared sandbox default -- Safaricom's newer Daraja portal issues
    a per-developer Initiator Name/Password and certificate on the "My Apps"
    / test_credentials page, so every one of these must be configured by
    the admin (Settings > M-Pesa) before B2C will work, even in sandbox."""
    base = get_mpesa_config()
    b2c_shortcode = (_setting('mpesa_b2c_shortcode') or os.getenv('MPESA_B2C_SHORTCODE') or base['shortcode'])
    return {
        **base,
        'initiator_name': (_setting('mpesa_initiator_name') or os.getenv('MPESA_INITIATOR_NAME') or '').strip(),
        'initiator_password': (_setting('mpesa_initiator_password')
                                or os.getenv('MPESA_INITIATOR_PASSWORD') or '').strip(),
        'b2c_shortcode': (b2c_shortcode or '').strip(),
        'certificate': (_setting('mpesa_b2c_certificate') or '').strip(),
        'command_id': (_setting('mpesa_b2c_command_id') or 'BusinessPayment').strip(),
    }


def is_b2c_configured():
    cfg = get_b2c_config()
    return bool(cfg['consumer_key'] and cfg['consumer_secret'] and cfg['initiator_name']
                and cfg['initiator_password'] and cfg['b2c_shortcode'] and cfg['certificate'])


def encrypt_security_credential(initiator_password, certificate_pem):
    """Encrypt the Initiator Password with Safaricom's RSA public-key
    certificate to produce the SecurityCredential B2C requests require.
    Safaricom specifically requires RSA PKCS#1 v1.5 padding (not OAEP)."""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509 import load_pem_x509_certificate, load_der_x509_certificate
    import base64 as _b64

    cert_bytes = certificate_pem.encode() if isinstance(certificate_pem, str) else certificate_pem
    try:
        if b'BEGIN CERTIFICATE' in cert_bytes:
            cert = load_pem_x509_certificate(cert_bytes)
        else:
            try:
                cert = load_der_x509_certificate(cert_bytes)
            except ValueError:
                der = _b64.b64decode(cert_bytes, validate=False)
                cert = load_der_x509_certificate(der)
    except Exception as e:
        raise MpesaError(f'Could not read the M-Pesa certificate ({e}) -- '
                          f're-paste the .cer file from your Daraja app')

    public_key = cert.public_key()
    encrypted = public_key.encrypt(initiator_password.encode(), padding.PKCS1v15())
    return _b64.b64encode(encrypted).decode()


def initiate_b2c_payment(phone, amount, remarks, occasion, result_url, timeout_url):
    """Send money from the business's M-Pesa account to a customer's phone
    (loan disbursement). Returns the Daraja response dict (contains
    ConversationID/OriginatorConversationID) on success. Raises MpesaError
    on any failure -- including "not configured", since B2C additionally
    requires Safaricom's "Go Live for B2C" approval on the shortcode."""
    cfg = get_b2c_config()
    if not is_b2c_configured():
        raise MpesaError(
            'M-Pesa disbursement is not fully configured -- set Initiator Name, Initiator Password, '
            'B2C Shortcode, and the Certificate under Settings > M-Pesa'
        )
    if not cfg['enabled']:
        raise MpesaError('M-Pesa payments are currently disabled in Settings')

    phone = normalize_phone(phone)
    amount = int(round(float(amount)))
    if amount <= 0:
        raise MpesaError('Amount must be positive')

    token = _get_access_token(cfg)
    security_credential = encrypt_security_credential(cfg['initiator_password'], cfg['certificate'])

    payload = {
        'InitiatorName': cfg['initiator_name'],
        'SecurityCredential': security_credential,
        'CommandID': cfg['command_id'],
        'Amount': amount,
        'PartyA': cfg['b2c_shortcode'],
        'PartyB': phone,
        'Remarks': str(remarks)[:100] or 'Loan disbursement',
        'QueueTimeOutURL': timeout_url,
        'ResultURL': result_url,
        'Occasion': str(occasion or '')[:100],
    }

    try:
        resp = requests.post(
            f"{cfg['base_url']}/mpesa/b2c/v3/paymentrequest",
            json=payload,
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
    except requests.RequestException as e:
        raise MpesaError(f'Could not reach Safaricom ({e})')

    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200 or str(body.get('ResponseCode')) != '0':
        error_msg = body.get('errorMessage') or body.get('ResponseDescription') or f'HTTP {resp.status_code}'
        raise MpesaError(f'B2C payment was rejected by Safaricom: {error_msg}')

    return body


def get_recent_mpesa_log(limit=25):
    rows = get_db().execute(
        "SELECT * FROM mpesa_transactions ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
