"""
webauthn_helper.py
-------------------
Fingerprint / passkey login (WebAuthn / FIDO2), built on the `webauthn`
library. This is an *addition* to password login, not a replacement --
users register a device (which stores a public key here and keeps the
matching private key in the device's secure enclave/TPM), then can sign in
afterwards with that device's fingerprint sensor / Face ID / Windows Hello
instead of typing a password.

Two ceremonies, each a two-step "options -> verify" round trip:

  Registration (user must already be logged in):
    1. begin_registration()  -> options handed to navigator.credentials.create()
    2. finish_registration() -> verifies the browser's response, stores the
                                 new credential row

  Authentication (used *instead of* a password, from the login page):
    1. begin_authentication()  -> options handed to navigator.credentials.get()
    2. finish_authentication() -> verifies the response against a stored
                                   credential and returns the matching user

The challenge for an in-progress ceremony is kept in the Flask session
(server-side signed cookie) between the two steps -- never trust a challenge
value sent back by the client.
"""
import base64
from flask import current_app, session

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
)
from webauthn.helpers import base64url_to_bytes

from core.database import get_db, execute, utcnow

_SESSION_REG_CHALLENGE = 'webauthn_reg_challenge'
_SESSION_AUTH_CHALLENGE = 'webauthn_auth_challenge'


def _rp_id():
    return current_app.config['WEBAUTHN_RP_ID']


def _rp_name():
    return current_app.config['WEBAUTHN_RP_NAME']


def _origin():
    return current_app.config['WEBAUTHN_ORIGIN']


def _b64u(raw_bytes):
    """Standard base64url, no padding -- matches what the browser sends."""
    return base64.urlsafe_b64encode(raw_bytes).decode('utf-8').rstrip('=')


# ---------------------------------------------------------------------------
# Registration (adding a new fingerprint/device to an existing account)
# ---------------------------------------------------------------------------
def begin_registration(user):
    existing = get_db().execute(
        "SELECT credential_id FROM webauthn_credentials WHERE user_id = %s", (user['id'],)
    ).fetchall()
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['credential_id']))
        for row in existing
    ]

    options = webauthn.generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=str(user['id']).encode('utf-8'),
        user_name=user['username'],
        user_display_name=user['full_name'] or user['username'],
        exclude_credentials=exclude_credentials or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    session[_SESSION_REG_CHALLENGE] = _b64u(options.challenge)
    return webauthn.options_to_json(options)


def finish_registration(user, credential_json, device_name=None):
    """Verifies the browser's response and stores the new credential.
    Raises ValueError on any verification failure (bad signature, challenge
    mismatch, wrong RP ID/origin, etc) -- callers should catch this and
    return a 400, never leak the underlying exception detail to the client."""
    expected_challenge_b64u = session.pop(_SESSION_REG_CHALLENGE, None)
    if not expected_challenge_b64u:
        raise ValueError('Registration session expired -- please try again.')

    try:
        verification = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(expected_challenge_b64u),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
        )
    except Exception as exc:
        raise ValueError(f'Could not verify this device: {exc}')

    credential_id_b64u = _b64u(verification.credential_id)
    public_key_b64u = _b64u(verification.credential_public_key)

    execute(
        """INSERT INTO webauthn_credentials
               (user_id, credential_id, public_key, sign_count, device_name, created_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user['id'], credential_id_b64u, public_key_b64u, verification.sign_count,
         (device_name or 'Unnamed device').strip()[:100] or 'Unnamed device', utcnow())
    )


# ---------------------------------------------------------------------------
# Authentication (logging in with a previously-registered fingerprint)
# ---------------------------------------------------------------------------
def begin_authentication(username=None):
    """If `username` is given, scopes the prompt to that account's registered
    devices (used on the login page, where the user has already typed their
    username). Without it, any discoverable credential on the device may be
    offered -- still safe, since finish_authentication() looks up the user
    from the credential itself rather than trusting anything the client
    claims."""
    allow_credentials = None
    if username:
        user_row = get_db().execute(
            "SELECT id FROM users WHERE username = %s OR email = %s", (username, username)
        ).fetchone()
        if user_row:
            creds = get_db().execute(
                "SELECT credential_id FROM webauthn_credentials WHERE user_id = %s", (user_row['id'],)
            ).fetchall()
            allow_credentials = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['credential_id']))
                for row in creds
            ] or None

    options = webauthn.generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session[_SESSION_AUTH_CHALLENGE] = _b64u(options.challenge)
    return webauthn.options_to_json(options)


def finish_authentication(credential_json):
    """Verifies the browser's response and returns the matching user dict on
    success. Raises ValueError on any failure -- unknown credential, bad
    signature, challenge mismatch, or a sign_count that goes backwards
    (a strong signal of a cloned authenticator)."""
    expected_challenge_b64u = session.pop(_SESSION_AUTH_CHALLENGE, None)
    if not expected_challenge_b64u:
        raise ValueError('Login session expired -- please try again.')

    # The credential ID in the response tells us which stored credential (and
    # therefore which user) this is -- looked up here, never trusted from any
    # other field the client sends.
    raw_id = credential_json.get('rawId') or credential_json.get('id')
    if not raw_id:
        raise ValueError('Malformed credential response.')
    credential_id_b64u = _b64u(base64url_to_bytes(raw_id))

    cred_row = get_db().execute(
        "SELECT * FROM webauthn_credentials WHERE credential_id = %s", (credential_id_b64u,)
    ).fetchone()
    if not cred_row:
        raise ValueError('This device is not registered to any account.')

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(expected_challenge_b64u),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            credential_public_key=base64url_to_bytes(cred_row['public_key']),
            credential_current_sign_count=cred_row['sign_count'],
        )
    except Exception as exc:
        raise ValueError(f'Could not verify this device: {exc}')

    execute(
        "UPDATE webauthn_credentials SET sign_count = %s, last_used_at = %s WHERE id = %s",
        (verification.new_sign_count, utcnow(), cred_row['id'])
    )

    user_row = get_db().execute("SELECT * FROM users WHERE id = %s", (cred_row['user_id'],)).fetchone()
    return dict(user_row) if user_row else None
