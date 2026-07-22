"""
auth.py
-------
Token issuance/verification (PyJWT) and role-based permission decorators.
Password hashing uses bcrypt directly (no flask-bcrypt wrapper needed).
"""
import bcrypt
import hashlib
import secrets
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, g, jsonify, current_app, redirect, url_for

from core.database import get_db, row_to_dict, execute, utcnow

ROLES = ('admin', 'loan_officer', 'accountant', 'cashier')

PASSWORD_RESET_TOKEN_TTL = timedelta(hours=1)

# Endpoints a user with must_change_password=1 can still reach -- just
# enough to actually change their password and to log out. Everything else
# behind @login_required redirects (page requests) or 403s (API requests)
# until they do. Keep this list minimal and explicit rather than trying to
# infer "safe" routes automatically.
_PASSWORD_CHANGE_EXEMPT_ENDPOINTS = {'auth.force_password_change', 'auth.logout'}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain_password):
    return bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(plain_password, password_hash):
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), password_hash.encode('utf-8'))
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Password reset tokens ("Forgot password?" on the login page)
# ---------------------------------------------------------------------------
def _hash_reset_token(raw_token):
    # A plain sha256 digest (not bcrypt) is fine and fast here -- the raw
    # token is already a high-entropy random value (256 bits), not a
    # human-memorable password, so it doesn't need bcrypt's deliberate
    # slowness/salting to resist guessing.
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def create_password_reset_token(user_id):
    """Generates a random reset token, stores only its hash, and returns the
    raw token (this is the only place the raw value ever exists outside the
    email it gets sent in)."""
    raw_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + PASSWORD_RESET_TOKEN_TTL).isoformat()
    execute(
        "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (user_id, _hash_reset_token(raw_token), expires_at, utcnow())
    )
    return raw_token


def verify_password_reset_token(raw_token):
    """Returns the matching password_reset_tokens row if `raw_token` is
    valid, unused, and unexpired -- otherwise None. Does not consume it;
    call consume_password_reset_token() once the new password is actually set."""
    if not raw_token:
        return None
    row = get_db().execute(
        "SELECT * FROM password_reset_tokens WHERE token_hash = %s", (_hash_reset_token(raw_token),)
    ).fetchone()
    if not row:
        return None
    if row['used_at']:
        return None
    try:
        expires_at = datetime.fromisoformat(row['expires_at'])
    except (TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None
    return row


def consume_password_reset_token(token_id):
    execute("UPDATE password_reset_tokens SET used_at = %s WHERE id = %s", (utcnow(), token_id))


# ---------------------------------------------------------------------------
# JWT issuance / verification
# ---------------------------------------------------------------------------
def create_access_token(user_id, role, expires_delta=None):
    expires_delta = expires_delta or timedelta(hours=8)
    now = datetime.now(timezone.utc)
    payload = {
        'sub': str(user_id),
        'role': role,
        'iat': now,
        'exp': now + expires_delta,
    }
    return pyjwt.encode(payload, current_app.config['JWT_SECRET_KEY'], algorithm='HS256')


def decode_access_token(token):
    """Returns the decoded payload dict, or raises pyjwt.PyJWTError subclasses
    (ExpiredSignatureError, InvalidTokenError, ...) on failure."""
    return pyjwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])


def _extract_token_from_request():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    # Fall back to a cookie so server-rendered pages keep working too.
    return request.cookies.get('access_token')


def _is_page_navigation():
    """True for a plain browser page load (GET, expects HTML) as opposed to
    an XHR/fetch call from core.js expecting JSON. Those two cases need very
    different failure responses: a JSON blob dumped in place of a page is
    broken UX (and the whole point of a PWA is that people open pages
    directly, e.g. from a home-screen icon, not just via in-app fetches)."""
    return request.method == 'GET' and 'text/html' in (request.headers.get('Accept') or '')


def _unauthenticated_response(message, status=401):
    if _is_page_navigation():
        next_path = request.full_path.rstrip('?')
        return redirect(url_for('auth.login', next=next_path))
    return jsonify({'error': message}), status


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def login_required(fn):
    """Validates the JWT and stashes the current user (as a dict) on flask.g.
    Returns 401 for missing/invalid/expired tokens, 403 if the account is
    deactivated -- or, for a plain page navigation rather than an API call,
    redirects to the login page instead (optionally back to where the user
    was headed, via ?next=)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token_from_request()
        if not token:
            return _unauthenticated_response('Authentication required')

        try:
            payload = decode_access_token(token)
        except pyjwt.ExpiredSignatureError:
            return _unauthenticated_response('Token has expired')
        except pyjwt.InvalidTokenError:
            return _unauthenticated_response('Invalid token')

        user_row = get_db().execute(
            "SELECT * FROM users WHERE id = %s", (payload['sub'],)
        ).fetchone()
        user = row_to_dict(user_row)
        if not user:
            return _unauthenticated_response('User no longer exists')
        if not user['is_active']:
            return _unauthenticated_response('Account is deactivated', status=403)

        # Extra roles an admin has granted on top of the primary `role`
        # column (see core/database.py migration 17 / Settings > Users) --
        # role_required() below checks the union of the two.
        extra_roles = get_db().execute(
            "SELECT role FROM user_roles WHERE user_id = %s", (user['id'],)
        ).fetchall()
        user['additional_roles'] = [r['role'] for r in extra_roles]

        g.current_user = user
        g.current_user_id = user['id']

        if user['must_change_password'] and request.endpoint not in _PASSWORD_CHANGE_EXEMPT_ENDPOINTS:
            message = 'You must change your password before continuing.'
            if _is_page_navigation():
                return redirect(url_for('auth.force_password_change'))
            return jsonify({'error': message, 'must_change_password': True}), 403

        return fn(*args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    """Stack under @login_required:

        @loans_bp.route('/api/<int:loan_id>/approve', methods=['POST'])
        @login_required
        @role_required('admin', 'loan_officer')
        def approve_loan(loan_id): ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, 'current_user', None)
            if user is None:
                return jsonify({'error': 'Authentication required'}), 401
            user_roles = {user['role'], *user.get('additional_roles', [])}
            if not user_roles & set(allowed_roles):
                return jsonify({'error': 'Insufficient permissions for this action'}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def get_current_user():
    """Convenience accessor mirroring the old helper's call signature.
    Returns a dict (sqlite row) or None -- routes previously expecting a
    SQLAlchemy model with .to_dict() should call current_user directly since
    it's already dict-shaped."""
    return getattr(g, 'current_user', None)
