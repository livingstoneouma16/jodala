from flask import Blueprint, request, jsonify, render_template
from datetime import date

from app.database import get_db, execute, utcnow
from app.auth import login_required, get_current_user
from app.serializers import income_public, expense_public, journal_entry_public, account_public, member_full_name
from app.utils import (generate_journal_number, generate_income_reference,
                        generate_expense_reference, log_audit, paginate,
                        adjust_main_account_balance, adjust_account_balance)

accounting_bp = Blueprint('accounting', __name__)


@accounting_bp.route('/')
@login_required
def index():
    return render_template('accounting/index.html', user=get_current_user())


@accounting_bp.route('/cashbook')
@login_required
def cashbook():
    return render_template('accounting/cashbook.html', user=get_current_user())


@accounting_bp.route('/api/income', methods=['GET'])
@login_required
def list_income():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    category = request.args.get('category')

    where, params = [], []
    if date_from:
        where.append("income_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("income_date <= ?")
        params.append(date_to)
    if category:
        where.append("category = ?")
        params.append(category)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows, total, pages = paginate(
        f"SELECT * FROM income{where_sql} ORDER BY income_date DESC",
        f"SELECT COUNT(*) FROM income{where_sql}",
        tuple(params), page, per_page
    )
    total_amount = get_db().execute(f"SELECT COALESCE(SUM(amount), 0) FROM income{where_sql}", tuple(params)).fetchone()[0]

    return jsonify({
        'income': [income_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page,
        'total_amount': round(total_amount, 2)
    })


@accounting_bp.route('/api/income', methods=['POST'])
@login_required
def record_income():
    data = request.get_json()
    user = get_current_user()
    cur = execute(
        """INSERT INTO income (reference, description, category, amount, income_date, payment_method,
               recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_income_reference(), data.get('description', '').strip(), data.get('category', 'other'),
         float(data.get('amount', 0)), data.get('income_date', date.today().isoformat()),
         data.get('payment_method', 'cash'), user['id'], utcnow())
    )
    income = get_db().execute("SELECT * FROM income WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_audit('RECORD_INCOME', 'income', income['id'])
    adjust_main_account_balance(income['amount'])
    # Post to the matching ledger income account (falls back to Fee Income
    # for anything not specifically interest) so the Chart of Accounts /
    # Trial Balance reflect this the moment it's recorded.
    income_code = '4000' if income['category'] == 'interest' else '4100'
    adjust_account_balance(income_code, income['amount'])
    return jsonify({'message': 'Income recorded', 'income': income_public(income)}), 201


@accounting_bp.route('/api/expenses', methods=['GET'])
@login_required
def list_expenses():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    where, params = [], []
    if date_from:
        where.append("expense_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("expense_date <= ?")
        params.append(date_to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows, total, pages = paginate(
        f"SELECT * FROM expenses{where_sql} ORDER BY expense_date DESC",
        f"SELECT COUNT(*) FROM expenses{where_sql}",
        tuple(params), page, per_page
    )

    return jsonify({
        'expenses': [expense_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@accounting_bp.route('/api/expenses', methods=['POST'])
@login_required
def record_expense():
    data = request.get_json()
    user = get_current_user()
    cur = execute(
        """INSERT INTO expenses (reference, description, category, amount, expense_date, payment_method,
               vendor, receipt_ref, recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_expense_reference(), data.get('description', '').strip(), data.get('category', 'other'),
         float(data.get('amount', 0)), data.get('expense_date', date.today().isoformat()),
         data.get('payment_method', 'cash'), data.get('vendor'), data.get('receipt_ref'),
         user['id'], utcnow())
    )
    expense = get_db().execute("SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_audit('RECORD_EXPENSE', 'expense', expense['id'])
    adjust_main_account_balance(-expense['amount'])
    adjust_account_balance('5000', expense['amount'])
    return jsonify({'message': 'Expense recorded', 'expense': expense_public(expense)}), 201


@accounting_bp.route('/api/journal', methods=['GET'])
@login_required
def list_journal():
    entries = get_db().execute("SELECT * FROM journal_entries ORDER BY entry_date DESC LIMIT 50").fetchall()
    return jsonify([journal_entry_public(e) for e in entries])


@accounting_bp.route('/api/journal', methods=['POST'])
@login_required
def create_journal_entry():
    data = request.get_json()
    user = get_current_user()

    lines = data.get('lines', [])
    total_debit = sum(l.get('debit', 0) for l in lines)
    total_credit = sum(l.get('credit', 0) for l in lines)

    if abs(total_debit - total_credit) > 0.01:
        return jsonify({'error': 'Journal entry must balance (debits = credits)'}), 400

    cur = execute(
        """INSERT INTO journal_entries (entry_number, description, entry_date, reference, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (generate_journal_number(), data.get('description', ''), data.get('entry_date', date.today().isoformat()),
         data.get('reference'), user['id'], utcnow())
    )
    entry_id = cur.lastrowid

    for line in lines:
        execute(
            """INSERT INTO journal_entry_lines (entry_id, debit_account_id, credit_account_id, amount, description)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, line.get('debit_account_id'), line.get('credit_account_id'),
             float(line.get('amount', 0)), line.get('description', ''))
        )

    entry = get_db().execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    return jsonify({'message': 'Journal entry created', 'entry': journal_entry_public(entry)}), 201


@accounting_bp.route('/api/accounts', methods=['GET'])
@login_required
def chart_of_accounts():
    accounts = get_db().execute("SELECT * FROM accounts WHERE is_active = 1 ORDER BY code").fetchall()
    return jsonify([account_public(a) for a in accounts])


@accounting_bp.route('/api/trial-balance')
@login_required
def trial_balance():
    accounts = get_db().execute("SELECT * FROM accounts WHERE is_active = 1 ORDER BY code").fetchall()
    result = []
    total_debit = 0
    total_credit = 0

    for acc in accounts:
        debit = acc['balance'] if acc['account_type'] in ('asset', 'expense') else 0
        credit = acc['balance'] if acc['account_type'] in ('liability', 'equity', 'income') else 0
        total_debit += debit
        total_credit += credit
        result.append({'code': acc['code'], 'name': acc['name'], 'type': acc['account_type'],
                        'debit': debit, 'credit': credit})

    return jsonify({
        'accounts': result,
        'total_debit': round(total_debit, 2),
        'total_credit': round(total_credit, 2)
    })


@accounting_bp.route('/api/profit-loss')
@login_required
def profit_loss():
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    income_where, income_params = [], []
    expense_where, expense_params = [], []
    if date_from:
        income_where.append("income_date >= ?"); income_params.append(date_from)
        expense_where.append("expense_date >= ?"); expense_params.append(date_from)
    if date_to:
        income_where.append("income_date <= ?"); income_params.append(date_to)
        expense_where.append("expense_date <= ?"); expense_params.append(date_to)

    income_where_sql = (" WHERE " + " AND ".join(income_where)) if income_where else ""
    expense_where_sql = (" WHERE " + " AND ".join(expense_where)) if expense_where else ""

    income_by_category = get_db().execute(
        f"SELECT category, SUM(amount) FROM income{income_where_sql} GROUP BY category", tuple(income_params)
    ).fetchall()
    expense_by_category = get_db().execute(
        f"SELECT category, SUM(amount) FROM expenses{expense_where_sql} GROUP BY category", tuple(expense_params)
    ).fetchall()

    total_income = sum(r[1] for r in income_by_category)
    total_expenses = sum(r[1] for r in expense_by_category)
    net_profit = total_income - total_expenses

    repayment_interest = get_db().execute("SELECT COALESCE(SUM(interest_portion), 0) FROM repayments").fetchone()[0]

    return jsonify({
        'income': [{'category': r[0], 'amount': round(r[1], 2)} for r in income_by_category],
        'expenses': [{'category': r[0], 'amount': round(r[1], 2)} for r in expense_by_category],
        'total_income': round(total_income, 2),
        'total_expenses': round(total_expenses, 2),
        'net_profit': round(net_profit, 2),
        'interest_income': round(repayment_interest, 2)
    })


@accounting_bp.route('/api/cashbook-data')
@login_required
def cashbook_data():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    db = get_db()
    income = db.execute("SELECT * FROM income WHERE income_date >= ? AND income_date <= ?",
                         (date_from, date_to)).fetchall()
    expenses = db.execute("SELECT * FROM expenses WHERE expense_date >= ? AND expense_date <= ?",
                           (date_from, date_to)).fetchall()
    repayments = db.execute(
        """SELECT repayments.*, loans.loan_number, loans.member_id
           FROM repayments LEFT JOIN loans ON loans.id = repayments.loan_id
           WHERE repayments.payment_date >= ? AND repayments.payment_date <= ?""",
        (date_from, date_to)
    ).fetchall()

    entries = []
    for r in repayments:
        borrower = 'Client'
        if r['member_id']:
            member = db.execute("SELECT * FROM members WHERE id = ?", (r['member_id'],)).fetchone()
            if member:
                borrower = member_full_name(member)
        entries.append({
            'date': r['payment_date'],
            'description': f"Loan repayment - {r['loan_number'] or ''} ({borrower})",
            'reference': r['receipt_number'],
            'type': 'receipt',
            'amount': r['amount']
        })

    for i in income:
        entries.append({'date': i['income_date'], 'description': i['description'],
                         'reference': i['reference'], 'type': 'receipt', 'amount': i['amount']})

    for e in expenses:
        entries.append({'date': e['expense_date'], 'description': e['description'],
                         'reference': e['reference'], 'type': 'payment', 'amount': e['amount']})

    entries.sort(key=lambda x: x['date'])

    total_receipts = sum(e['amount'] for e in entries if e['type'] == 'receipt')
    total_payments = sum(e['amount'] for e in entries if e['type'] == 'payment')

    return jsonify({
        'entries': entries,
        'total_receipts': round(total_receipts, 2),
        'total_payments': round(total_payments, 2),
        'net_balance': round(total_receipts - total_payments, 2)
    })
