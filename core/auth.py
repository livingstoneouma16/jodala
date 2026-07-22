"""
auth.py
-------
Token issuance/verification (PyJWT) and role-based permission decorators.
Password hashing uses bcrypt directly (no flask-bcrypt wrapper needed).
"""
import bcrypt
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, g, jsonify, current_app, redirect, url_for

from core.database import get_db, row_to_dict

ROLES = ('admin', 'loan_officer', 'accountant', 'cashier')

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
            if user['role'] not in allowed_roles:
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
