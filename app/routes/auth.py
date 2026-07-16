from flask import Blueprint, request, jsonify, render_template, redirect, url_for, make_response, current_app
import pyotp

from app.database import get_db, execute, utcnow
from app.auth import (
    hash_password, verify_password, create_access_token, login_required, get_current_user
)
from app.serializers import user_public
from app.utils import log_audit
from app import limiter

auth_bp = Blueprint('auth', __name__)


def _find_user_by_username_or_email(identifier):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ? OR email = ?", (identifier, identifier)
    ).fetchone()


@auth_bp.route('/login', methods=['GET', 'POST'])
# Brute-force protection: caps credential-guessing attempts per IP. The
# per-minute limit stops rapid automated guessing while still letting a
# real user retry a mistyped password; the per-hour limit catches slower,
# distributed attempts against the same IP.
@limiter.limit('10 per minute; 50 per hour')
def login():
    if request.method == 'GET':
        return render_template('auth/login.html')

    data = request.get_json() if request.is_json else request.form
    username = data.get('username', '').strip()
    password = data.get('password', '')

    user = _find_user_by_username_or_email(username)

    if not user or not verify_password(password, user['password_hash']):
        if request.is_json:
            return jsonify({'error': 'Invalid credentials'}), 401
        return render_template('auth/login.html', error='Invalid username or password')

    if not user['is_active']:
        if request.is_json:
            return jsonify({'error': 'Account is deactivated'}), 403
        return render_template('auth/login.html', error='Your account has been deactivated')

    # Check 2FA
    if user['totp_enabled']:
        totp_code = data.get('totp_code', '')
        if not totp_code:
            if request.is_json:
                return jsonify({'require_2fa': True, 'user_id': user['id']}), 200
            return render_template('auth/login.html', require_2fa=True, user_id=user['id'])

        totp = pyotp.TOTP(user['totp_secret'])
        if not totp.verify(totp_code):
            if request.is_json:
                return jsonify({'error': 'Invalid 2FA code'}), 401
            return render_template('auth/login.html', error='Invalid 2FA code',
                                    require_2fa=True, user_id=user['id'])

    execute("UPDATE users SET last_login = ? WHERE id = ?", (utcnow(), user['id']))

    access_token = create_access_token(user['id'], user['role'])
    log_audit('LOGIN', 'user', user['id'])

    if request.is_json:
        return jsonify({
            'access_token': access_token,
            'user': user_public(user)
        })

    response = make_response(redirect(url_for('dashboard.index')))
    response.set_cookie('access_token', access_token, httponly=True, samesite='Lax',
                         secure=current_app.config['COOKIE_SECURE'])
    return response


@auth_bp.route('/logout')
def logout():
    response = make_response(redirect(url_for('auth.login')))
    response.delete_cookie('access_token', path='/')
    response.set_cookie('access_token', '', expires=0, max_age=0, path='/', httponly=True, samesite='Lax',
                         secure=current_app.config['COOKIE_SECURE'])
    return response


@auth_bp.route('/profile', methods=['GET', 'PUT'])
@login_required
def profile():
    user = get_current_user()
    if request.method == 'GET':
        return render_template('auth/profile.html', user=user)

    data = request.get_json() if request.is_json else request.form
    full_name = data.get('full_name', user['full_name'])
    phone = data.get('phone', user['phone'])
    email = data.get('email', user['email'])

    if data.get('new_password'):
        password_hash = hash_password(data['new_password'])
        execute(
            "UPDATE users SET full_name = ?, phone = ?, email = ?, password_hash = ?, updated_at = ? WHERE id = ?",
            (full_name, phone, email, password_hash, utcnow(), user['id'])
        )
    else:
        execute(
            "UPDATE users SET full_name = ?, phone = ?, email = ?, updated_at = ? WHERE id = ?",
            (full_name, phone, email, utcnow(), user['id'])
        )

    updated = get_db().execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone()
    return jsonify({'message': 'Profile updated', 'user': user_public(updated)})


@auth_bp.route('/setup-2fa', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per minute')
def setup_2fa():
    user = get_current_user()
    if request.method == 'GET':
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        provisioning_uri = totp.provisioning_uri(user['email'], issuer_name='Jodala Microfinance')
        return jsonify({'secret': secret, 'provisioning_uri': provisioning_uri})

    data = request.get_json()
    secret = data.get('secret')
    code = data.get('code')

    totp = pyotp.TOTP(secret)
    if totp.verify(code):
        execute("UPDATE users SET totp_secret = ?, totp_enabled = 1, updated_at = ? WHERE id = ?",
                (secret, utcnow(), user['id']))
        return jsonify({'message': '2FA enabled successfully'})

    return jsonify({'error': 'Invalid code'}), 400


@auth_bp.route('/disable-2fa', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def disable_2fa():
    user = get_current_user()
    data = request.get_json()
    password = data.get('password', '')

    if not verify_password(password, user['password_hash']):
        return jsonify({'error': 'Invalid password'}), 401

    execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL, updated_at = ? WHERE id = ?",
            (utcnow(), user['id']))
    return jsonify({'message': '2FA disabled'})


@auth_bp.route('/me')
@login_required
def me():
    user = get_current_user()
    return jsonify(user_public(user))
