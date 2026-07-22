from flask import Blueprint, request, jsonify, render_template, redirect, url_for, make_response, current_app
import pyotp

from core.database import get_db, execute, utcnow
from core.auth import (
    hash_password, verify_password, create_access_token, login_required, get_current_user,
    create_password_reset_token, verify_password_reset_token, consume_password_reset_token,
)
from core.serializers import user_public
from core.utils import log_audit
from core import limiter

auth_bp = Blueprint('auth', __name__)


def _find_user_by_username_or_email(identifier):
    return get_db().execute(
        "SELECT * FROM users WHERE username = %s OR email = %s", (identifier, identifier)
    ).fetchone()


def _safe_next_path(value):
    """Only accept a same-site relative path as a post-login redirect
    target -- anything else (an absolute URL, or a protocol-relative
    '//evil.com' which browsers treat as absolute) is an open-redirect risk
    and gets dropped in favor of the default destination."""
    if value and value.startswith('/') and not value.startswith('//'):
        return value
    return None


@auth_bp.route('/login', methods=['GET', 'POST'])
# Brute-force protection: caps credential-guessing attempts per IP. The
# per-minute limit stops rapid automated guessing while still letting a
# real user retry a mistyped password; the per-hour limit catches slower,
# distributed attempts against the same IP.
@limiter.limit('10 per minute; 50 per hour')
def login():
    if request.method == 'GET':
        reset_done = request.args.get('reset') == '1'
        return render_template('auth/login.html', next=_safe_next_path(request.args.get('next')),
                               reset_done=reset_done)

    data = request.get_json() if request.is_json else request.form
    username = data.get('username', '').strip()
    password = data.get('password', '')
    next_path = _safe_next_path(data.get('next'))

    user = _find_user_by_username_or_email(username)

    if not user or not verify_password(password, user['password_hash']):
        if request.is_json:
            return jsonify({'error': 'Invalid credentials'}), 401
        return render_template('auth/login.html', error='Invalid username or password', next=next_path)

    if not user['is_active']:
        if request.is_json:
            return jsonify({'error': 'Account is deactivated'}), 403
        return render_template('auth/login.html', error='Your account has been deactivated', next=next_path)

    # Check 2FA
    if user['totp_enabled']:
        totp_code = data.get('totp_code', '')
        if not totp_code:
            if request.is_json:
                return jsonify({'require_2fa': True, 'user_id': user['id']}), 200
            return render_template('auth/login.html', require_2fa=True, user_id=user['id'], next=next_path)

        totp = pyotp.TOTP(user['totp_secret'])
        if not totp.verify(totp_code):
            if request.is_json:
                return jsonify({'error': 'Invalid 2FA code'}), 401
            return render_template('auth/login.html', error='Invalid 2FA code',
                                    require_2fa=True, user_id=user['id'], next=next_path)

    execute("UPDATE users SET last_login = %s WHERE id = %s", (utcnow(), user['id']))

    access_token = create_access_token(user['id'], user['role'])
    log_audit('LOGIN', 'user', user['id'])

    if request.is_json:
        return jsonify({
            'access_token': access_token,
            'user': user_public(user),
            'must_change_password': bool(user['must_change_password']),
        })

    if user['must_change_password']:
        destination = url_for('auth.force_password_change')
    else:
        destination = next_path or url_for('dashboard.index')
    response = make_response(redirect(destination))
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


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour')
def forgot_password():
    """Self-service password reset entry point, linked from the login page.
    Always shows the same confirmation regardless of whether the
    email/username matched a real account -- telling an anonymous visitor
    "no account with that email" would let them enumerate valid usernames/
    emails, so both the match and no-match paths render identically."""
    if request.method == 'GET':
        return render_template('auth/forgot_password.html')

    data = request.get_json() if request.is_json else request.form
    identifier = (data.get('identifier') or '').strip()

    user = None
    if identifier:
        user = _find_user_by_username_or_email(identifier)

    if user and user['is_active']:
        raw_token = create_password_reset_token(user['id'])
        reset_url = url_for('auth.reset_password', token=raw_token, _external=True)
        from core.mailer import send_email_async
        send_email_async(
            user['email'],
            'Reset your Jodala Microfinance password',
            f"Hi {user['full_name']},\n\n"
            f"We received a request to reset your password. Click the link below to "
            f"choose a new one (valid for 1 hour):\n\n{reset_url}\n\n"
            f"If you didn't request this, you can safely ignore this email -- your "
            f"password will stay unchanged.\n\n-- Jodala Microfinance",
            f"<p>Hi {user['full_name']},</p>"
            f"<p>We received a request to reset your password. Click the link below to "
            f"choose a new one (valid for 1 hour):</p>"
            f"<p><a href='{reset_url}'>{reset_url}</a></p>"
            f"<p>If you didn't request this, you can safely ignore this email -- your "
            f"password will stay unchanged.</p>"
        )
        log_audit('PASSWORD_RESET_REQUESTED', 'user', user['id'])

    message = 'If an account matches that username or email, a reset link has been sent to it.'
    if request.is_json:
        return jsonify({'message': message})
    return render_template('auth/forgot_password.html', sent=True, message=message)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def reset_password(token):
    row = verify_password_reset_token(token)

    if request.method == 'GET':
        if not row:
            return render_template('auth/reset_password.html', invalid=True)
        return render_template('auth/reset_password.html', token=token)

    if not row:
        message = 'This reset link is invalid or has expired. Request a new one.'
        if request.is_json:
            return jsonify({'error': message}), 400
        return render_template('auth/reset_password.html', invalid=True, error=message)

    data = request.get_json() if request.is_json else request.form
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    def fail(message):
        if request.is_json:
            return jsonify({'error': message}), 400
        return render_template('auth/reset_password.html', token=token, error=message)

    if len(new_password) < 8:
        return fail('New password must be at least 8 characters')
    if new_password != confirm_password:
        return fail('New password and confirmation do not match')

    execute(
        "UPDATE users SET password_hash = %s, must_change_password = 0, updated_at = %s WHERE id = %s",
        (hash_password(new_password), utcnow(), row['user_id'])
    )
    consume_password_reset_token(row['id'])
    log_audit('PASSWORD_RESET_COMPLETED', 'user', row['user_id'])

    if request.is_json:
        return jsonify({'message': 'Password reset successfully. You can now log in.'})
    return redirect(url_for('auth.login', reset='1'))


@auth_bp.route('/change-password-required', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per minute')
def force_password_change():
    """Where login_required redirects any user with must_change_password=1
    -- seeded default admins, and anyone whose password was just set by
    someone else (new-user creation, an admin-initiated reset). Requires
    re-entering the current (temporary) password, same as a normal password
    change, so this can't be used to take over a session left logged in."""
    user = get_current_user()
    if not user['must_change_password']:
        return redirect(url_for('dashboard.index'))

    if request.method == 'GET':
        return render_template('auth/force_password_change.html', user=user)

    data = request.get_json() if request.is_json else request.form
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    def fail(message):
        if request.is_json:
            return jsonify({'error': message}), 400
        return render_template('auth/force_password_change.html', user=user, error=message)

    if not verify_password(current_password, user['password_hash']):
        return fail('Current password is incorrect')
    if len(new_password) < 8:
        return fail('New password must be at least 8 characters')
    if new_password != confirm_password:
        return fail('New password and confirmation do not match')
    if verify_password(new_password, user['password_hash']):
        return fail('New password must be different from your current password')

    execute(
        "UPDATE users SET password_hash = %s, must_change_password = 0, updated_at = %s WHERE id = %s",
        (hash_password(new_password), utcnow(), user['id'])
    )
    log_audit('FORCED_PASSWORD_CHANGE', 'user', user['id'])

    if request.is_json:
        return jsonify({'message': 'Password changed successfully'})
    return redirect(url_for('dashboard.index'))


@auth_bp.route('/profile', methods=['GET', 'PUT'])
@login_required
def profile():
    user = get_current_user()
    if request.method == 'GET':
        return render_template('auth/profile.html', user=user)

    data = request.get_json() if request.is_json else request.form
    full_name = data.get('full_name') or user['full_name']
    phone = data.get('phone') or user['phone']
    email = data.get('email') or user['email']

    if data.get('new_password'):
        password_hash = hash_password(data['new_password'])
        execute(
            "UPDATE users SET full_name = %s, phone = %s, email = %s, password_hash = %s, "
            "must_change_password = 0, updated_at = %s WHERE id = %s",
            (full_name, phone, email, password_hash, utcnow(), user['id'])
        )
    else:
        execute(
            "UPDATE users SET full_name = %s, phone = %s, email = %s, updated_at = %s WHERE id = %s",
            (full_name, phone, email, utcnow(), user['id'])
        )

    updated = get_db().execute("SELECT * FROM users WHERE id = %s", (user['id'],)).fetchone()
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

    data = request.get_json(silent=True) or {}
    secret = data.get('secret')
    code = data.get('code')
    if not secret or not code:
        return jsonify({'error': 'secret and code are required'}), 400

    totp = pyotp.TOTP(secret)
    if totp.verify(code):
        execute("UPDATE users SET totp_secret = %s, totp_enabled = 1, updated_at = %s WHERE id = %s",
                (secret, utcnow(), user['id']))
        return jsonify({'message': '2FA enabled successfully'})

    return jsonify({'error': 'Invalid code'}), 400


@auth_bp.route('/disable-2fa', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def disable_2fa():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')

    if not verify_password(password, user['password_hash']):
        return jsonify({'error': 'Invalid password'}), 401

    execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL, updated_at = %s WHERE id = %s",
            (utcnow(), user['id']))
    return jsonify({'message': '2FA disabled'})


@auth_bp.route('/me')
@login_required
def me():
    user = get_current_user()
    return jsonify(user_public(user))
