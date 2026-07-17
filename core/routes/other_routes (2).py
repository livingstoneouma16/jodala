from flask import Blueprint, request, jsonify, render_template, send_file
from datetime import date
import io
import os

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, get_current_user, hash_password
from core.serializers import (loan_product_public, user_public, notification_public,
                              audit_log_public, member_full_name, client_full_name)
from core.utils import paginate, log_audit, adjust_main_account_balance, adjust_account_balance, notify

settings_bp = Blueprint('settings', __name__)
notifications_bp = Blueprint('notifications', __name__)
documents_bp = Blueprint('documents', __name__)
users_bp = Blueprint('users', __name__)


# ==================== SETTINGS ====================

@settings_bp.route('/')
@login_required
def index():
    return render_template('settings/index.html', user=get_current_user())


@settings_bp.route('/api/company', methods=['GET'])
@login_required
def get_company():
    keys = ['company_name', 'company_email', 'company_phone', 'company_address',
            'company_website', 'currency', 'fiscal_year_start', 'loan_prefix',
            'logo_image', 'main_account_opening_balance']
    db = get_db()
    settings = {}
    for k in keys:
        row = db.execute("SELECT value FROM company_settings WHERE key = %s", (k,)).fetchone()
        settings[k] = row['value'] if row else ''
    return jsonify(settings)


@settings_bp.route('/api/company', methods=['PUT'])
@login_required
@role_required('admin')
def update_company():
    data = request.get_json()
    now = utcnow()

    # 'main_account_opening_balance' isn't just another text setting -- it's
    # the headline cash figure that the Chart of Accounts / Trial Balance are
    # built from. If it's edited here directly (as opposed to going through
    # the "Add to Main Account" endpoint), we still need to move the ledger
    # by the same delta -- otherwise the Cash (1000) account and this number
    # silently drift apart. A change in opening balance represents capital
    # rather than income/expense, so we post the delta to Equity (3000) too,
    # keeping the books balanced.
    if 'main_account_opening_balance' in data:
        try:
            new_value = float(data['main_account_opening_balance'])
        except (TypeError, ValueError):
            new_value = 0
        row = get_db().execute(
            "SELECT value FROM company_settings WHERE key = 'main_account_opening_balance'"
        ).fetchone()
        current_value = float(row['value']) if row and row['value'] else 0
        delta = round(new_value - current_value, 2)
        if delta:
            adjust_account_balance('1000', delta)
            adjust_account_balance('3000', delta)

    for key, value in data.items():
        existing = get_db().execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if existing:
            execute("UPDATE company_settings SET value = %s, updated_at = %s WHERE key = %s", (str(value), now, key))
        else:
            execute("INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)", (key, str(value), now))
    return jsonify({'message': 'Company settings updated'})


@settings_bp.route('/api/notifications', methods=['GET'])
@login_required
@role_required('admin')
def get_notification_settings():
    from core.mailer import get_mail_config
    cfg = get_mail_config()
    return jsonify({
        'resend_from_email': cfg['from_email'],
        # Never echo the real API key back to the browser -- just tell
        # the UI whether one is already set, so the field can show a
        # placeholder instead of leaking the secret.
        'resend_api_key_set': bool(cfg['api_key']),
        'resend_sender_name': cfg['sender_name'],
        'email_notifications_enabled': cfg['enabled'],
    })


@settings_bp.route('/api/notifications', methods=['PUT'])
@login_required
@role_required('admin')
def update_notification_settings():
    data = request.get_json() or {}
    now = utcnow()

    def _set(key, value):
        existing = get_db().execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if existing:
            execute("UPDATE company_settings SET value = %s, updated_at = %s WHERE key = %s", (str(value), now, key))
        else:
            execute("INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)", (key, str(value), now))

    if 'resend_from_email' in data:
        _set('resend_from_email', (data.get('resend_from_email') or '').strip())
    if 'resend_sender_name' in data:
        _set('resend_sender_name', (data.get('resend_sender_name') or '').strip())
    if 'email_notifications_enabled' in data:
        _set('email_notifications_enabled', '1' if data.get('email_notifications_enabled') else '0')
    # Only overwrite the stored API key if a new one was actually typed
    # in -- an empty string here means "leave it unchanged", not "clear it".
    if data.get('resend_api_key'):
        _set('resend_api_key', data['resend_api_key'].strip())

    log_audit('UPDATE_NOTIFICATION_SETTINGS', 'company_settings', None)
    return jsonify({'message': 'Notification settings updated'})


@settings_bp.route('/api/notifications/log', methods=['GET'])
@login_required
@role_required('admin')
def get_email_log():
    from core.mailer import get_recent_email_log
    return jsonify(get_recent_email_log(25))


@settings_bp.route('/api/notifications/test', methods=['POST'])
@login_required
@role_required('admin')
def send_test_notification_email():
    from core.mailer import send_email, is_configured
    user = get_current_user()
    if not is_configured():
        return jsonify({'error': 'Resend API key and From address must be set first'}), 400
    to = (request.get_json() or {}).get('to') or user['email']
    ok, error = send_email(
        to,
        'Jodala Microfinance - Test Email',
        f"Hi {user['full_name']},\n\nThis is a test email from your Jodala Microfinance system. "
        "If you received this, Gmail notifications are working correctly.\n\n-- Jodala Microfinance"
    )
    if not ok:
        return jsonify({'error': f'Failed to send: {error}'}), 502
    return jsonify({'message': f'Test email sent to {to}'})


@settings_bp.route('/api/mpesa', methods=['GET'])
@login_required
@role_required('admin')
def get_mpesa_settings():
    from core.mpesa import get_b2c_config, is_b2c_configured, _setting
    cfg = get_b2c_config()
    return jsonify({
        'mpesa_environment': cfg['environment'],
        'mpesa_consumer_key': cfg['consumer_key'],
        # Never echo the real consumer secret / passkey / initiator password
        # back to the browser -- just tell the UI whether one is already
        # set, so the field can show a placeholder instead of leaking it.
        'mpesa_consumer_secret_set': bool(cfg['consumer_secret']),
        'mpesa_shortcode': cfg['shortcode'],
        'mpesa_passkey_set': bool(cfg['passkey']),
        'mpesa_enabled': cfg['enabled'],
        'mpesa_callback_url': _setting('mpesa_callback_url', ''),
        'using_sandbox_defaults': cfg['is_sandbox'] and not (_setting('mpesa_consumer_key') or os.getenv('MPESA_CONSUMER_KEY')),
        # B2C (disbursement)
        'mpesa_initiator_name': cfg['initiator_name'],
        'mpesa_initiator_password_set': bool(cfg['initiator_password']),
        'mpesa_b2c_shortcode': cfg['b2c_shortcode'],
        'mpesa_b2c_certificate_set': bool(cfg['certificate']),
        'mpesa_b2c_command_id': cfg['command_id'],
        'mpesa_b2c_configured': is_b2c_configured(),
        'mpesa_b2c_result_url': _setting('mpesa_b2c_result_url', ''),
        'mpesa_b2c_timeout_url': _setting('mpesa_b2c_timeout_url', ''),
    })


@settings_bp.route('/api/mpesa', methods=['PUT'])
@login_required
@role_required('admin')
def update_mpesa_settings():
    data = request.get_json() or {}
    now = utcnow()

    def _set(key, value):
        existing = get_db().execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if existing:
            execute("UPDATE company_settings SET value = %s, updated_at = %s WHERE key = %s", (str(value), now, key))
        else:
            execute("INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)", (key, str(value), now))

    if 'mpesa_environment' in data:
        _set('mpesa_environment', 'production' if data.get('mpesa_environment') == 'production' else 'sandbox')
    if 'mpesa_consumer_key' in data:
        _set('mpesa_consumer_key', (data.get('mpesa_consumer_key') or '').strip())
    if 'mpesa_shortcode' in data:
        _set('mpesa_shortcode', (data.get('mpesa_shortcode') or '').strip())
    if 'mpesa_callback_url' in data:
        _set('mpesa_callback_url', (data.get('mpesa_callback_url') or '').strip())
    if 'mpesa_enabled' in data:
        _set('mpesa_enabled', '1' if data.get('mpesa_enabled') else '0')
    # Only overwrite stored secrets if a new value was actually typed in --
    # an empty string means "leave it unchanged", not "clear it".
    if data.get('mpesa_consumer_secret'):
        _set('mpesa_consumer_secret', data['mpesa_consumer_secret'].strip())
    if data.get('mpesa_passkey'):
        _set('mpesa_passkey', data['mpesa_passkey'].strip())

    # B2C (disbursement)
    if 'mpesa_initiator_name' in data:
        _set('mpesa_initiator_name', (data.get('mpesa_initiator_name') or '').strip())
    if 'mpesa_b2c_shortcode' in data:
        _set('mpesa_b2c_shortcode', (data.get('mpesa_b2c_shortcode') or '').strip())
    if 'mpesa_b2c_command_id' in data:
        _set('mpesa_b2c_command_id', (data.get('mpesa_b2c_command_id') or 'BusinessPayment').strip())
    if 'mpesa_b2c_result_url' in data:
        _set('mpesa_b2c_result_url', (data.get('mpesa_b2c_result_url') or '').strip())
    if 'mpesa_b2c_timeout_url' in data:
        _set('mpesa_b2c_timeout_url', (data.get('mpesa_b2c_timeout_url') or '').strip())
    if data.get('mpesa_initiator_password'):
        _set('mpesa_initiator_password', data['mpesa_initiator_password'].strip())
    if data.get('mpesa_b2c_certificate'):
        # Validate it's actually parseable before saving -- a bad paste here
        # would otherwise fail silently until the first real disbursement.
        from core.mpesa import encrypt_security_credential, MpesaError
        try:
            encrypt_security_credential('test', data['mpesa_b2c_certificate'].strip())
        except MpesaError as e:
            return jsonify({'error': str(e)}), 400
        _set('mpesa_b2c_certificate', data['mpesa_b2c_certificate'].strip())

    log_audit('UPDATE_MPESA_SETTINGS', 'company_settings', None)
    return jsonify({'message': 'M-Pesa settings updated'})


@settings_bp.route('/api/mpesa/log', methods=['GET'])
@login_required
@role_required('admin')
def get_mpesa_log():
    from core.mpesa import get_recent_mpesa_log
    return jsonify(get_recent_mpesa_log(25))


@settings_bp.route('/api/mpesa/test', methods=['POST'])
@login_required
@role_required('admin')
def send_test_mpesa_push():
    from core.mpesa import initiate_stk_push, MpesaError, normalize_phone
    from core.routes.mpesa import _callback_url
    data = request.get_json() or {}
    phone = data.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400
    try:
        phone_normalized = normalize_phone(phone)
        result = initiate_stk_push(
            phone=phone_normalized, amount=1, account_reference='TEST',
            transaction_desc='Test Push', callback_url=_callback_url(),
        )
    except MpesaError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({
        'message': f'Test STK push (KSh 1) sent to {phone_normalized} -- check the phone for the M-Pesa prompt.',
        'checkout_request_id': result.get('CheckoutRequestID'),
    })


@settings_bp.route('/api/mpesa/test-b2c', methods=['POST'])
@login_required
@role_required('admin')
def send_test_mpesa_b2c():
    from core.mpesa import initiate_b2c_payment, MpesaError, normalize_phone
    from core.routes.mpesa import _b2c_result_url, _b2c_timeout_url
    data = request.get_json() or {}
    phone = data.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400
    try:
        phone_normalized = normalize_phone(phone)
        result = initiate_b2c_payment(
            phone=phone_normalized, amount=1, remarks='Test payout', occasion='Test',
            result_url=_b2c_result_url(), timeout_url=_b2c_timeout_url(),
        )
    except MpesaError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({
        'message': f'Test B2C payout (KSh 1) sent to {phone_normalized}.',
        'originator_conversation_id': result.get('OriginatorConversationID'),
    })


@settings_bp.route('/api/company/main-account', methods=['POST'])
@login_required
@role_required('admin')
def add_to_main_account():
    data = request.get_json()
    try:
        amount = float(data.get('amount', 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({'error': 'Enter a valid amount greater than zero'}), 400

    new_balance = adjust_main_account_balance(amount)
    # adjust_main_account_balance() already posts the debit side to Cash
    # (1000). This is a capital injection, not income, so the offsetting
    # credit belongs in Equity (3000) -- without it the Trial Balance would
    # stop balancing every time this button is used.
    adjust_account_balance('3000', amount)

    log_audit('ADD_MAIN_ACCOUNT_BALANCE', 'company_settings', None, new_values={'amount_added': amount, 'new_balance': new_balance})

    return jsonify({'message': 'Balance updated', 'main_account_opening_balance': new_balance})


@settings_bp.route('/api/loan-products', methods=['GET'])
@login_required
def list_loan_products():
    products = get_db().execute("SELECT * FROM loan_products").fetchall()
    return jsonify([loan_product_public(p) for p in products])


@settings_bp.route('/api/loan-products', methods=['POST'])
@login_required
@role_required('admin')
def create_loan_product():
    data = request.get_json()
    cur = execute(
        """INSERT INTO loan_products (name, code, description, min_amount, max_amount, interest_rate,
               interest_type, repayment_frequency, min_term, max_term, penalty_rate,
               insurance_fee, require_guarantor, require_collateral, is_active, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)""",
        (data.get('name'), data.get('code'), data.get('description'),
         float(data.get('min_amount', 0)), float(data.get('max_amount', 0)),
         float(data.get('interest_rate', 0)), data.get('interest_type', 'flat'),
         data.get('repayment_frequency', 'monthly'), int(data.get('min_term', 1)),
         int(data.get('max_term', 24)), float(data.get('penalty_rate', 0)),
         float(data.get('insurance_fee', 0)),
         1 if data.get('require_guarantor') else 0, 1 if data.get('require_collateral') else 0,
         utcnow())
    )
    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (cur.lastrowid,)).fetchone()
    return jsonify({'message': 'Product created', 'product': loan_product_public(product)}), 201


@settings_bp.route('/api/loan-products/<int:product_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_loan_product(product_id):
    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    data = request.get_json()

    editable = ['name', 'description', 'interest_rate', 'min_amount', 'max_amount',
                'min_term', 'max_term', 'penalty_rate', 'insurance_fee',
                'interest_type', 'repayment_frequency']
    values = {f: data.get(f, product[f]) for f in editable}
    is_active = 1 if data.get('is_active', product['is_active']) else 0

    execute(
        f"UPDATE loan_products SET {', '.join(f'{f} = %s' for f in editable)}, is_active = %s WHERE id = %s",
        tuple(values[f] for f in editable) + (is_active, product_id)
    )
    updated = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (product_id,)).fetchone()
    return jsonify({'message': 'Product updated', 'product': loan_product_public(updated)})


@settings_bp.route('/api/loan-products/<int:product_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_loan_product(product_id):
    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    in_use = get_db().execute(
        "SELECT id FROM loans WHERE product_id = %s", (product_id,)
    ).fetchone()
    if in_use:
        return jsonify({'error': 'Cannot delete a loan product with existing loans. Deactivate it instead.'}), 400

    old_data = loan_product_public(product)
    execute("DELETE FROM loan_products WHERE id = %s", (product_id,))
    log_audit('DELETE_LOAN_PRODUCT', 'loan_product', product_id, old_values=old_data)

    return jsonify({'message': 'Loan product deleted successfully'})


@settings_bp.route('/api/savings-products', methods=['GET'])
@login_required
def list_savings_products():
    products = get_db().execute("SELECT * FROM savings_products").fetchall()
    return jsonify([{
        'id': p['id'], 'name': p['name'], 'code': p['code'], 'description': p['description'],
        'interest_rate': p['interest_rate'], 'min_balance': p['min_balance'],
        'is_active': bool(p['is_active'])
    } for p in products])


@settings_bp.route('/api/savings-products', methods=['POST'])
@login_required
@role_required('admin')
def create_savings_product():
    data = request.get_json()
    execute(
        """INSERT INTO savings_products (name, code, description, interest_rate, min_balance, is_active, created_at)
           VALUES (%s, %s, %s, %s, %s, 1, %s)""",
        (data.get('name'), data.get('code'), data.get('description'),
         float(data.get('interest_rate', 0)), float(data.get('min_balance', 0)), utcnow())
    )
    return jsonify({'message': 'Savings product created'}), 201


@settings_bp.route('/api/savings-products/<int:product_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_savings_product(product_id):
    product = get_db().execute("SELECT * FROM savings_products WHERE id = %s", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    data = request.get_json()

    editable = ['name', 'description', 'interest_rate', 'min_balance']
    values = {f: data.get(f, product[f]) for f in editable}
    is_active = 1 if data.get('is_active', product['is_active']) else 0

    execute(
        f"UPDATE savings_products SET {', '.join(f'{f} = %s' for f in editable)}, is_active = %s WHERE id = %s",
        tuple(values[f] for f in editable) + (is_active, product_id)
    )
    updated = get_db().execute("SELECT * FROM savings_products WHERE id = %s", (product_id,)).fetchone()
    return jsonify({'message': 'Product updated', 'product': {
        'id': updated['id'], 'name': updated['name'], 'code': updated['code'],
        'description': updated['description'], 'interest_rate': updated['interest_rate'],
        'min_balance': updated['min_balance'], 'is_active': bool(updated['is_active'])
    }})


@settings_bp.route('/api/savings-products/<int:product_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_savings_product(product_id):
    product = get_db().execute("SELECT * FROM savings_products WHERE id = %s", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    in_use = get_db().execute(
        "SELECT id FROM savings_accounts WHERE product_id = %s", (product_id,)
    ).fetchone()
    if in_use:
        return jsonify({'error': 'Cannot delete a savings product with existing accounts. Deactivate it instead.'}), 400

    old_data = {
        'id': product['id'], 'name': product['name'], 'code': product['code'],
        'description': product['description'], 'interest_rate': product['interest_rate'],
        'min_balance': product['min_balance'], 'is_active': bool(product['is_active'])
    }
    execute("DELETE FROM savings_products WHERE id = %s", (product_id,))
    log_audit('DELETE_SAVINGS_PRODUCT', 'savings_product', product_id, old_values=old_data)

    return jsonify({'message': 'Savings product deleted successfully'})


@settings_bp.route('/api/regions', methods=['GET'])
@login_required
def list_regions():
    regions = get_db().execute("SELECT * FROM regions ORDER BY name").fetchall()
    return jsonify([{'id': r['id'], 'name': r['name'], 'is_active': bool(r['is_active'])} for r in regions])


@settings_bp.route('/api/regions', methods=['POST'])
@login_required
@role_required('admin')
def create_region():
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Region name is required'}), 400

    existing = get_db().execute("SELECT id FROM regions WHERE name = %s", (name,)).fetchone()
    if existing:
        return jsonify({'error': 'A region with this name already exists'}), 400

    cur = execute(
        "INSERT INTO regions (name, is_active, created_at) VALUES (%s, 1, %s)",
        (name, utcnow())
    )
    region = get_db().execute("SELECT * FROM regions WHERE id = %s", (cur.lastrowid,)).fetchone()
    log_audit('CREATE_REGION', 'region', region['id'], new_values={'name': name})
    return jsonify({'message': 'Region created', 'region': {'id': region['id'], 'name': region['name'], 'is_active': bool(region['is_active'])}}), 201


@settings_bp.route('/api/regions/<int:region_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_region(region_id):
    region = get_db().execute("SELECT * FROM regions WHERE id = %s", (region_id,)).fetchone()
    if not region:
        return jsonify({'error': 'Region not found'}), 404
    data = request.get_json()

    name = (data.get('name', region['name']) or '').strip()
    if not name:
        return jsonify({'error': 'Region name is required'}), 400

    duplicate = get_db().execute(
        "SELECT id FROM regions WHERE name = %s AND id != %s", (name, region_id)
    ).fetchone()
    if duplicate:
        return jsonify({'error': 'A region with this name already exists'}), 400

    is_active = 1 if data.get('is_active', region['is_active']) else 0
    old_name = region['name']

    execute("UPDATE regions SET name = %s, is_active = %s WHERE id = %s", (name, is_active, region_id))

    # Keep existing member/client records in sync if the region was renamed.
    if name != old_name:
        execute("UPDATE members SET region = %s WHERE region = %s", (name, old_name))
        execute("UPDATE clients SET region = %s WHERE region = %s", (name, old_name))

    updated = get_db().execute("SELECT * FROM regions WHERE id = %s", (region_id,)).fetchone()
    log_audit('UPDATE_REGION', 'region', region_id, old_values={'name': old_name})
    return jsonify({'message': 'Region updated', 'region': {'id': updated['id'], 'name': updated['name'], 'is_active': bool(updated['is_active'])}})


@settings_bp.route('/api/regions/<int:region_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_region(region_id):
    region = get_db().execute("SELECT * FROM regions WHERE id = %s", (region_id,)).fetchone()
    if not region:
        return jsonify({'error': 'Region not found'}), 404

    in_use = get_db().execute(
        "SELECT id FROM members WHERE region = %s UNION SELECT id FROM clients WHERE region = %s",
        (region['name'], region['name'])
    ).fetchone()
    if in_use:
        return jsonify({'error': 'Cannot delete a region assigned to existing members or clients. Deactivate it instead.'}), 400

    execute("DELETE FROM regions WHERE id = %s", (region_id,))
    log_audit('DELETE_REGION', 'region', region_id, old_values={'name': region['name']})
    return jsonify({'message': 'Region deleted successfully'})


# ==================== NOTIFICATIONS ====================

@notifications_bp.route('/')
@login_required
def index():
    return render_template('notifications/index.html', user=get_current_user())


@notifications_bp.route('/api', methods=['GET'])
@login_required
def list_notifications():
    user = get_current_user()
    page = request.args.get('page', 1, type=int)

    rows, total, pages = paginate(
        "SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC",
        "SELECT COUNT(*) FROM notifications WHERE user_id = %s",
        (user['id'],), page, 20
    )
    unread = get_db().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND is_read = 0", (user['id'],)
    ).fetchone()[0]

    return jsonify({
        'notifications': [notification_public(n) for n in rows],
        'unread': unread,
        'total': total,
        'pages': pages
    })


@notifications_bp.route('/api/mark-all-read', methods=['POST'])
@login_required
def mark_all_read():
    user = get_current_user()
    execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (user['id'],))
    return jsonify({'message': 'All marked as read'})


# ==================== DOCUMENTS ====================

@documents_bp.route('/')
@login_required
def index():
    return render_template('documents/index.html', user=get_current_user())


def _company_name():
    row = get_db().execute("SELECT value FROM company_settings WHERE key = 'company_name'").fetchone()
    return row['value'] if row and row['value'] else 'Jodala Microfinance'


def _company_logo_image(max_width=4*72, max_height=1.2*72):
    """Return a reportlab Image flowable for the uploaded company logo (stored
    as a base64 data URL in company_settings), scaled to fit within the given
    box while preserving aspect ratio. Returns None if no logo is set or it
    can't be decoded."""
    import base64
    from reportlab.platypus import Image
    from PIL import Image as PILImage

    row = get_db().execute("SELECT value FROM company_settings WHERE key = 'logo_image'").fetchone()
    data_url = row['value'] if row else ''
    if not data_url or not data_url.startswith('data:'):
        return None

    try:
        header, b64data = data_url.split(',', 1)
        raw = base64.b64decode(b64data)
        pil_img = PILImage.open(io.BytesIO(raw))
        width, height = pil_img.size
        scale = min(max_width / width, max_height / height, 1)
        img = Image(io.BytesIO(raw), width=width * scale, height=height * scale)
        img.hAlign = 'CENTER'
        return img
    except Exception:
        return None


@documents_bp.route('/loan-agreement/<int:loan_id>')
@login_required
def loan_agreement(loan_id):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_CENTER

    db = get_db()
    loan = db.execute(
        """SELECT loans.*, loan_products.name AS product_name FROM loans
           LEFT JOIN loan_products ON loan_products.id = loans.product_id
           WHERE loans.id = %s""", (loan_id,)
    ).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404

    company_name = _company_name()
    borrower, borrower_phone = 'N/A', ''
    if loan['member_id']:
        m = db.execute("SELECT * FROM members WHERE id = %s", (loan['member_id'],)).fetchone()
        if m:
            borrower, borrower_phone = member_full_name(m), m['phone']
    elif loan['client_id']:
        c = db.execute("SELECT * FROM clients WHERE id = %s", (loan['client_id'],)).fetchone()
        if c:
            borrower, borrower_phone = client_full_name(c), c['phone']

    application_date = date.fromisoformat(loan['application_date']).strftime('%d %B %Y') if loan['application_date'] else ''

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=18, textColor=colors.HexColor('#1B4332'), alignment=TA_CENTER)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#1B4332'))
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=10, leading=16)

    story = [
        Paragraph(company_name, title_style),
        Paragraph("LOAN AGREEMENT", title_style),
        HRFlowable(width="100%", thickness=2, color=colors.HexColor('#1B4332')),
        Spacer(1, 0.3*inch),
    ]

    logo_img = _company_logo_image()
    if logo_img:
        story = [logo_img, Spacer(1, 0.15*inch)] + story

    loan_data = [
        ['Loan Number:', loan['loan_number'], 'Date:', application_date],
        ['Borrower:', borrower, 'Phone:', borrower_phone],
        ['Loan Product:', loan['product_name'] or '', 'Status:', (loan['status'] or '').upper()],
        ['Principal Amount:', f"Ksh {loan['principal_amount']:,.2f}", 'Interest Rate:', f"{loan['interest_rate']}%"],
        ['Loan Term:', f"{loan['term']} {loan['repayment_frequency']} installments", 'Total Repayable:', f"Ksh {loan['total_repayable']:,.2f}"],
        ['Insurance Fee:', f"Ksh {loan['insurance_fee']:,.2f}", 'Purpose:', loan['purpose'] or 'N/A'],
    ]

    table = Table(loan_data, colWidths=[4*cm, 6*cm, 3.5*cm, 4*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#D8F3DC')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#D8F3DC')),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.3*inch))

    story.append(Paragraph("TERMS AND CONDITIONS", heading_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    story.append(Spacer(1, 0.2*inch))

    terms = [
        "1. The Borrower agrees to repay the loan amount plus interest as per the agreed schedule.",
        "2. Failure to make timely repayments will attract a penalty charge as per the loan product terms.",
        "3. The Lender reserves the right to demand immediate repayment in case of default.",
        "4. The Borrower shall notify the Lender of any change in contact or employment details.",
        "5. This agreement shall be governed by the laws of the applicable jurisdiction.",
        "6. Any dispute arising from this agreement shall be resolved through arbitration.",
        "7. The Borrower confirms that all information provided is accurate and complete.",
    ]
    for term in terms:
        story.append(Paragraph(term, normal_style))
        story.append(Spacer(1, 0.1*inch))

    story.append(Spacer(1, 0.5*inch))

    sig_data = [
        ['_________________________', '', '_________________________'],
        ['Borrower Signature', '', 'Authorized Officer'],
        [borrower, '', company_name],
        ['Date: ___________________', '', 'Date: ___________________'],
    ]
    sig_table = Table(sig_data, colWidths=[6*cm, 3*cm, 6*cm])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
    ]))
    story.append(sig_table)

    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True,
                      download_name=f"loan_agreement_{loan['loan_number']}.pdf",
                      mimetype='application/pdf')


@documents_bp.route('/repayment-receipt/<int:repayment_id>')
@login_required
def repayment_receipt_pdf(repayment_id):
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_CENTER

    db = get_db()
    repayment = db.execute("SELECT * FROM repayments WHERE id = %s", (repayment_id,)).fetchone()
    if not repayment:
        return jsonify({'error': 'Repayment not found'}), 404
    loan = db.execute("SELECT * FROM loans WHERE id = %s", (repayment['loan_id'],)).fetchone()

    company_name = _company_name()
    borrower = 'N/A'
    if loan and loan['member_id']:
        m = db.execute("SELECT * FROM members WHERE id = %s", (loan['member_id'],)).fetchone()
        if m:
            borrower = member_full_name(m)
    elif loan and loan['client_id']:
        c = db.execute("SELECT * FROM clients WHERE id = %s", (loan['client_id'],)).fetchone()
        if c:
            borrower = client_full_name(c)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A5, rightMargin=1.5*cm, leftMargin=1.5*cm,
                             topMargin=1.5*cm, bottomMargin=1.5*cm)

    styles = getSampleStyleSheet()
    center = ParagraphStyle('Center', parent=styles['Normal'], alignment=TA_CENTER, fontSize=11)

    story = [
        Paragraph(f"<b>{company_name}</b>", center),
        Paragraph("PAYMENT RECEIPT", ParagraphStyle('Title', parent=styles['Title'], fontSize=14, alignment=TA_CENTER, textColor=colors.HexColor('#1B4332'))),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor('#1B4332')),
        Spacer(1, 0.2*cm),
    ]

    payment_date_str = date.fromisoformat(repayment['payment_date']).strftime('%d %B %Y') if repayment['payment_date'] else ''
    data = [
        ['Receipt No:', repayment['receipt_number']],
        ['Borrower:', borrower],
        ['Loan No:', loan['loan_number'] if loan else ''],
        ['Amount Paid:', f"Ksh {repayment['amount']:,.2f}"],
        ['Principal:', f"Ksh {repayment['principal_portion']:,.2f}"],
        ['Interest:', f"Ksh {repayment['interest_portion']:,.2f}"],
        ['Method:', (repayment['payment_method'] or '').replace('_', ' ').title()],
        ['Date:', payment_date_str],
        ['Balance After:', f"Ksh {loan['outstanding_balance']:,.2f}" if loan else ''],
    ]

    table = Table(data, colWidths=[5*cm, 7*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#D8F3DC')),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("Thank you for your payment!", center))
    story.append(Paragraph("This is a computer-generated receipt.", center))

    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True,
                      download_name=f"receipt_{repayment['receipt_number']}.pdf",
                      mimetype='application/pdf')


# ==================== USERS ====================

@users_bp.route('/')
@login_required
@role_required('admin')
def index():
    return render_template('settings/users.html', user=get_current_user())


@users_bp.route('/api', methods=['GET'])
@login_required
@role_required('admin')
def list_users():
    users = get_db().execute("SELECT * FROM users").fetchall()
    return jsonify([user_public(u) for u in users])


@users_bp.route('/api', methods=['POST'])
@login_required
@role_required('admin')
def create_user():
    data = request.get_json()
    db = get_db()

    if db.execute("SELECT id FROM users WHERE username = %s", (data.get('username'),)).fetchone():
        return jsonify({'error': 'Username already taken'}), 400
    if db.execute("SELECT id FROM users WHERE email = %s", (data.get('email'),)).fetchone():
        return jsonify({'error': 'Email already registered'}), 400

    now = utcnow()
    cur = execute(
        """INSERT INTO users (username, email, password_hash, full_name, role, phone,
               is_active, must_change_password, totp_enabled, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 1, 1, 0, %s, %s)""",
        (data.get('username'), data.get('email'),
         hash_password(data.get('password', 'Jodala@2024')),
         data.get('full_name'), data.get('role', 'loan_officer'), data.get('phone'), now, now)
    )
    new_user = db.execute("SELECT * FROM users WHERE id = %s", (cur.lastrowid,)).fetchone()

    temp_password = data.get('password', 'Jodala@2024')
    notify(
        new_user['id'],
        'Welcome to Jodala Microfinance',
        f"Your account has been created with the role '{new_user['role']}'.",
        notification_type='info', related_type='user', related_id=new_user['id'],
        email=new_user['email'],
        email_subject='Your Jodala Microfinance account',
        email_body_html=(
            f"<p>Dear {new_user['full_name']},</p>"
            f"<p>An account has been created for you on Jodala Microfinance.</p>"
            f"<p>Username: <strong>{new_user['username']}</strong><br>"
            f"Temporary password: <strong>{temp_password}</strong><br>"
            f"Role: <strong>{new_user['role']}</strong></p>"
            f"<p>Please log in and change your password as soon as possible.</p>"
            f"<p>You'll be asked to set a new password the first time you log in.</p>"
        )
    )

    return jsonify({'message': 'User created', 'user': user_public(new_user)}), 201


@users_bp.route('/api/<int:user_id>', methods=['PUT'])
@login_required
def update_user(user_id):
    current = get_current_user()
    if current['role'] != 'admin' and current['id'] != user_id:
        return jsonify({'error': 'Unauthorized'}), 403

    target = get_db().execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not target:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json()

    full_name = data.get('full_name', target['full_name'])
    email = data.get('email', target['email'])
    phone = data.get('phone', target['phone'])
    role = target['role']
    is_active = target['is_active']

    if current['role'] == 'admin':
        role = data.get('role', role)
        is_active = 1 if data.get('is_active', is_active) else 0

    if data.get('password'):
        password_hash = hash_password(data['password'])
        # A password set by someone else (an admin resetting another user's
        # password) is a temporary/known password just like account
        # creation -- force a change on next login. A user setting their
        # own password here already knows it, so clear the flag instead.
        force_change = 1 if current['id'] != user_id else 0
        execute(
            "UPDATE users SET full_name=%s, email=%s, phone=%s, role=%s, is_active=%s, password_hash=%s, "
            "must_change_password=%s, updated_at=%s WHERE id=%s",
            (full_name, email, phone, role, is_active, password_hash, force_change, utcnow(), user_id)
        )
    else:
        execute(
            "UPDATE users SET full_name=%s, email=%s, phone=%s, role=%s, is_active=%s, updated_at=%s WHERE id=%s",
            (full_name, email, phone, role, is_active, utcnow(), user_id)
        )

    updated = get_db().execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    return jsonify({'message': 'User updated', 'user': user_public(updated)})


@users_bp.route('/api/<int:user_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_user(user_id):
    current = get_current_user()
    if current['id'] == user_id:
        return jsonify({'error': 'Cannot delete your own account'}), 400

    target = get_db().execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not target:
        return jsonify({'error': 'User not found'}), 404

    db = get_db()
    activity_checks = [
        ("SELECT id FROM members WHERE created_by = %s", 'registered members'),
        ("SELECT id FROM clients WHERE created_by = %s", 'registered clients'),
        ("SELECT id FROM loans WHERE loan_officer_id = %s OR approved_by = %s OR disbursed_by = %s", 'loans'),
        ("SELECT id FROM repayments WHERE collected_by = %s", 'repayments'),
        ("SELECT id FROM savings_accounts WHERE created_by = %s", 'savings accounts'),
        ("SELECT id FROM savings_transactions WHERE processed_by = %s", 'savings transactions'),
        ("SELECT id FROM journal_entries WHERE created_by = %s", 'journal entries'),
        ("SELECT id FROM income WHERE recorded_by = %s", 'income records'),
        ("SELECT id FROM expenses WHERE recorded_by = %s OR approved_by = %s", 'expense records'),
        ("SELECT id FROM audit_logs WHERE user_id = %s", 'audit log history'),
    ]
    for query, label in activity_checks:
        param_count = query.count('%s')
        if db.execute(query, (user_id,) * param_count).fetchone():
            return jsonify({
                'error': f'Cannot delete a user with existing {label}. Deactivate the account instead.'
            }), 400

    old_data = user_public(target)
    execute("DELETE FROM notifications WHERE user_id = %s", (user_id,))
    execute("DELETE FROM users WHERE id = %s", (user_id,))
    log_audit('DELETE_USER', 'user', user_id, old_values=old_data)

    return jsonify({'message': 'User deleted successfully'})


@users_bp.route('/api/audit-logs', methods=['GET'])
@login_required
@role_required('admin')
def audit_logs():
    page = request.args.get('page', 1, type=int)
    rows, total, pages = paginate(
        """SELECT audit_logs.*, users.full_name AS user_name FROM audit_logs
           LEFT JOIN users ON users.id = audit_logs.user_id ORDER BY audit_logs.created_at DESC""",
        "SELECT COUNT(*) FROM audit_logs",
        (), page, 50
    )
    return jsonify({
        'logs': [audit_log_public(l) for l in rows],
        'total': total,
        'pages': pages
    })
