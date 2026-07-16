from flask import Blueprint, request, jsonify, render_template
from datetime import date

from core.database import get_db, execute, utcnow
from core.auth import login_required, get_current_user
from core.serializers import savings_account_public, savings_transaction_public
from core.utils import (generate_savings_transaction_number, log_audit,
                        paginate, adjust_main_account_balance, adjust_account_balance)

savings_bp = Blueprint('savings', __name__)


@savings_bp.route('/')
@login_required
def index():
    db = get_db()
    members = db.execute("SELECT * FROM members WHERE status = 'active' ORDER BY first_name").fetchall()
    products = db.execute("SELECT * FROM savings_products WHERE is_active = 1").fetchall()
    return render_template('savings/index.html', user=get_current_user(),
                            members=members, products=products)


def _account_join_sql():
    return """SELECT savings_accounts.*,
                      TRIM(members.first_name || ' ' || COALESCE(members.middle_name, '') || ' ' || members.last_name) AS member_name,
                      savings_products.name AS product_name
               FROM savings_accounts
               LEFT JOIN members ON members.id = savings_accounts.member_id
               LEFT JOIN savings_products ON savings_products.id = savings_accounts.product_id"""


@savings_bp.route('/api/accounts', methods=['GET'])
@login_required
def list_accounts():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')

    where, params = '', ()
    if search:
        like = f'%{search}%'
        where = " WHERE (savings_accounts.account_number LIKE ? OR members.first_name LIKE ? OR members.last_name LIKE ?)"
        params = (like, like, like)

    base = _account_join_sql() + where + " ORDER BY savings_accounts.opened_at DESC"
    count_sql = f"""SELECT COUNT(*) FROM savings_accounts
                     LEFT JOIN members ON members.id = savings_accounts.member_id{where}"""

    rows, total, pages = paginate(base, count_sql, params, page, per_page)

    return jsonify({
        'accounts': [savings_account_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@savings_bp.route('/api/accounts', methods=['POST'])
@login_required
def open_account():
    data = request.get_json()
    user = get_current_user()

    member = get_db().execute("SELECT * FROM members WHERE id = ?", (data.get('member_id'),)).fetchone()
    if not member:
        return jsonify({'error': 'Member not found'}), 404

    product = get_db().execute("SELECT * FROM savings_products WHERE id = ?", (data.get('product_id'),)).fetchone()
    if not product:
        return jsonify({'error': 'Invalid savings product'}), 400

    existing = get_db().execute(
        "SELECT id FROM savings_accounts WHERE account_number = ?", (member['member_number'],)
    ).fetchone()
    if existing:
        return jsonify({'error': 'This member already has a savings account'}), 400

    cur = execute(
        """INSERT INTO savings_accounts (account_number, member_id, product_id, balance, status, opened_at, created_by)
           VALUES (?, ?, ?, 0, 'active', ?, ?)""",
        (member['member_number'], member['id'], product['id'], utcnow(), user['id'])
    )
    account_id = cur.lastrowid

    initial_deposit = float(data.get('initial_deposit', 0) or 0)
    if initial_deposit > 0:
        _create_transaction(account_id, 'deposit', initial_deposit, data.get('payment_method', 'cash'), user['id'])

    log_audit('OPEN_SAVINGS_ACCOUNT', 'savings_account', account_id)
    account = get_db().execute(_account_join_sql() + " WHERE savings_accounts.id = ?", (account_id,)).fetchone()
    return jsonify({'message': 'Savings account opened', 'account': savings_account_public(account)}), 201


@savings_bp.route('/api/accounts/<int:account_id>', methods=['GET'])
@login_required
def get_account(account_id):
    account = get_db().execute(_account_join_sql() + " WHERE savings_accounts.id = ?", (account_id,)).fetchone()
    if not account:
        return jsonify({'error': 'Account not found'}), 404

    data = savings_account_public(account)
    txns = get_db().execute(
        """SELECT savings_transactions.*, savings_accounts.account_number FROM savings_transactions
           LEFT JOIN savings_accounts ON savings_accounts.id = savings_transactions.account_id
           WHERE savings_transactions.account_id = ? ORDER BY savings_transactions.created_at DESC LIMIT 50""",
        (account_id,)
    ).fetchall()
    data['transactions'] = [savings_transaction_public(t) for t in txns]
    return jsonify(data)


@savings_bp.route('/api/deposit', methods=['POST'])
@login_required
def deposit():
    data = request.get_json()
    user = get_current_user()

    account = get_db().execute("SELECT * FROM savings_accounts WHERE id = ?", (data.get('account_id'),)).fetchone()
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    if account['status'] != 'active':
        return jsonify({'error': 'Account is not active'}), 400

    amount = float(data.get('amount', 0))
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400

    txn, new_balance = _create_transaction(account['id'], 'deposit', amount, data.get('payment_method', 'cash'),
                                            user['id'], data.get('reference'), data.get('notes'))

    return jsonify({'message': 'Deposit recorded', 'transaction': savings_transaction_public(txn),
                     'balance': new_balance}), 201


@savings_bp.route('/api/withdraw', methods=['POST'])
@login_required
def withdraw():
    data = request.get_json()
    user = get_current_user()

    account = get_db().execute("SELECT * FROM savings_accounts WHERE id = ?", (data.get('account_id'),)).fetchone()
    if not account:
        return jsonify({'error': 'Account not found'}), 404

    amount = float(data.get('amount', 0))
    if account['balance'] < amount:
        return jsonify({'error': 'Insufficient balance'}), 400

    product = get_db().execute("SELECT * FROM savings_products WHERE id = ?", (account['product_id'],)).fetchone()
    if product and product['min_balance'] and (account['balance'] - amount) < product['min_balance']:
        return jsonify({'error': f"Minimum balance of {product['min_balance']} must be maintained"}), 400

    txn, new_balance = _create_transaction(account['id'], 'withdrawal', amount, data.get('payment_method', 'cash'),
                                            user['id'], data.get('reference'), data.get('notes'))

    return jsonify({'message': 'Withdrawal processed', 'transaction': savings_transaction_public(txn),
                     'balance': new_balance}), 201


def _create_transaction(account_id, txn_type, amount, method, user_id, reference=None, notes=None):
    account = get_db().execute("SELECT * FROM savings_accounts WHERE id = ?", (account_id,)).fetchone()
    balance_before = account['balance']
    balance_after = balance_before + amount if txn_type in ('deposit', 'interest') else balance_before - amount

    execute("UPDATE savings_accounts SET balance = ? WHERE id = ?", (balance_after, account_id))

    if txn_type == 'deposit':
        adjust_main_account_balance(amount)
        adjust_account_balance('2000', amount)
    elif txn_type == 'withdrawal':
        adjust_main_account_balance(-amount)
        adjust_account_balance('2000', -amount)
    # 'interest' postings are an internal book entry (no new cash in/out), so
    # they don't move the main cash account.

    now = utcnow()
    cur = execute(
        """INSERT INTO savings_transactions (transaction_number, account_id, transaction_type, amount,
               balance_before, balance_after, payment_method, reference, notes, transaction_date, processed_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_savings_transaction_number(), account_id, txn_type, amount, balance_before, balance_after,
         method, reference, notes, date.today().isoformat(), user_id, now)
    )
    txn = get_db().execute(
        """SELECT savings_transactions.*, savings_accounts.account_number FROM savings_transactions
           LEFT JOIN savings_accounts ON savings_accounts.id = savings_transactions.account_id
           WHERE savings_transactions.id = ?""", (cur.lastrowid,)
    ).fetchone()
    return txn, balance_after


@savings_bp.route('/api/transactions', methods=['GET'])
@login_required
def list_transactions():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    account_id = request.args.get('account_id')

    where, params = '', ()
    if account_id:
        where = " WHERE savings_transactions.account_id = ?"
        params = (int(account_id),)

    base = f"""SELECT savings_transactions.*, savings_accounts.account_number FROM savings_transactions
               LEFT JOIN savings_accounts ON savings_accounts.id = savings_transactions.account_id{where}
               ORDER BY savings_transactions.created_at DESC"""
    count_sql = f"SELECT COUNT(*) FROM savings_transactions{where}"

    rows, total, pages = paginate(base, count_sql, params, page, per_page)

    return jsonify({
        'transactions': [savings_transaction_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@savings_bp.route('/api/products', methods=['GET'])
@login_required
def list_products():
    products = get_db().execute("SELECT * FROM savings_products WHERE is_active = 1").fetchall()
    return jsonify([{
        'id': p['id'], 'name': p['name'], 'code': p['code'],
        'interest_rate': p['interest_rate'], 'min_balance': p['min_balance']
    } for p in products])
