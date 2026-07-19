from flask import Blueprint, render_template, jsonify, request, url_for
from datetime import date

from core.database import get_db, execute
from core.auth import login_required, get_current_user
from core.serializers import audit_log_public, notification_public, member_full_name, client_full_name
from core.calculator import add_months

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@login_required
def index():
    return render_template('dashboard/index.html', user=get_current_user())


@dashboard_bp.route('/stats')
@login_required
def stats():
    db = get_db()
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    today_iso = today.isoformat()

    total_members = db.execute("SELECT COUNT(*) FROM members WHERE status = 'active'").fetchone()[0]
    total_clients = db.execute("SELECT COUNT(*) FROM clients WHERE status = 'active'").fetchone()[0]

    active_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'active'").fetchone()[0]
    pending_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
    total_disbursed = db.execute(
        "SELECT COALESCE(SUM(amount_disbursed), 0) FROM loans WHERE status IN ('active', 'completed')"
    ).fetchone()[0]
    total_outstanding = db.execute(
        "SELECT COALESCE(SUM(outstanding_balance), 0) FROM loans WHERE status = 'active'"
    ).fetchone()[0]

    due_today = db.execute(
        "SELECT COUNT(*) FROM loan_schedules WHERE due_date = %s AND status IN ('pending', 'partial')",
        (today_iso,)
    ).fetchone()[0]

    overdue_loan_ids = [r[0] for r in db.execute(
        """SELECT DISTINCT loan_id FROM loan_schedules
           WHERE due_date < %s AND status IN ('pending', 'partial')""", (today_iso,)
    ).fetchall()]

    overdue_balance = 0
    if overdue_loan_ids:
        placeholders = ','.join(['%s'] * len(overdue_loan_ids))
        overdue_balance = db.execute(
            f"SELECT COALESCE(SUM(outstanding_balance), 0) FROM loans WHERE id IN ({placeholders})",
            overdue_loan_ids
        ).fetchone()[0]
    par = (overdue_balance / total_outstanding * 100) if total_outstanding > 0 else 0

    monthly_collections = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM repayments WHERE payment_date >= %s AND payment_date <= %s",
        (month_start, today_iso)
    ).fetchone()[0]
    monthly_income_manual = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM income WHERE income_date >= %s AND income_date <= %s",
        (month_start, today_iso)
    ).fetchone()[0]
    monthly_income_repayments = db.execute(
        """SELECT COALESCE(SUM(interest_portion + penalty_portion), 0)
           FROM repayments WHERE payment_date >= %s AND payment_date <= %s""",
        (month_start, today_iso)
    ).fetchone()[0]
    monthly_income = monthly_income_manual + monthly_income_repayments
    monthly_expenses = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE expense_date >= %s AND expense_date <= %s",
        (month_start, today_iso)
    ).fetchone()[0]
    total_savings = db.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM savings_accounts WHERE status = 'active'"
    ).fetchone()[0]

    # Main Account (Cash & Bank) — read the same running balance shown and
    # maintained in Settings > Company Profile, so the two numbers always
    # match. That value already starts from the admin-set opening balance
    # and is kept current automatically on every real cash movement
    # (repayments, savings deposits/withdrawals, disbursements, income,
    # expenses) plus any manual top-ups, so we don't recompute it here.
    main_account_row = db.execute(
        "SELECT value FROM company_settings WHERE key = 'main_account_opening_balance'"
    ).fetchone()
    main_account_balance = float(main_account_row['value']) if main_account_row and main_account_row['value'] else 0

    return jsonify({
        'total_members': total_members,
        'total_clients': total_clients,
        'active_loans': active_loans,
        'pending_loans': pending_loans,
        'total_disbursed': round(total_disbursed, 2),
        'total_outstanding': round(total_outstanding, 2),
        'due_today': due_today,
        'overdue_loans': len(overdue_loan_ids),
        'par': round(par, 2),
        'monthly_collections': round(monthly_collections, 2),
        'monthly_income': round(monthly_income, 2),
        'monthly_expenses': round(monthly_expenses, 2),
        'monthly_profit': round(monthly_income - monthly_expenses, 2),
        'total_savings': round(total_savings, 2),
        'main_account_balance': round(main_account_balance, 2)
    })


def _month_window(n_months):
    """Returns (window_start_date, [first-of-month date, oldest..newest]) for
    the trailing n_months window ending this month -- shared by the trend
    endpoints below so they can each fetch their whole window in one or two
    GROUP BY queries instead of looping month-by-month with a fresh query
    (or three) per iteration."""
    today = date.today()
    months = [add_months(today.replace(day=1), -i) for i in range(n_months - 1, -1, -1)]
    return months[0], months


@dashboard_bp.route('/loan-trend')
@login_required
def loan_trend():
    """Monthly loan disbursement trend - last 12 months.

    Date columns are stored as 'YYYY-MM-DD' text (see database.py), so
    SUBSTRING(col, 1, 7) directly gives the 'YYYY-MM' bucket to GROUP BY --
    no date cast needed, and NULL dates (e.g. a loan that's never been
    disbursed) drop out on their own since they can't satisfy `>= %s`
    against an actual date string."""
    db = get_db()
    window_start, months = _month_window(12)

    disbursed_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(disbursement_date, 1, 7) AS ym, COALESCE(SUM(amount_disbursed), 0)
           FROM loans WHERE disbursement_date >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}
    collected_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(payment_date, 1, 7) AS ym, COALESCE(SUM(amount), 0)
           FROM repayments WHERE payment_date >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}

    data = [{
        'month': m.strftime('%b %Y'),
        'disbursed': round(disbursed_by_month.get(m.strftime('%Y-%m'), 0), 2),
        'collected': round(collected_by_month.get(m.strftime('%Y-%m'), 0), 2),
    } for m in months]

    return jsonify(data)


@dashboard_bp.route('/loan-status-distribution')
@login_required
def loan_status_distribution():
    rows = get_db().execute("SELECT status, COUNT(*) FROM loans GROUP BY status").fetchall()
    return jsonify([{'status': r[0], 'count': r[1]} for r in rows])


@dashboard_bp.route('/income-expense-trend')
@login_required
def income_expense_trend():
    """Monthly income vs expenses - last 6 months (see loan_trend's docstring
    for why SUBSTRING-on-text is used for the month bucketing here)."""
    db = get_db()
    window_start, months = _month_window(6)

    manual_income_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(income_date, 1, 7) AS ym, COALESCE(SUM(amount), 0)
           FROM income WHERE income_date >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}
    repayment_income_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(payment_date, 1, 7) AS ym, COALESCE(SUM(interest_portion + penalty_portion), 0)
           FROM repayments WHERE payment_date >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}
    expenses_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(expense_date, 1, 7) AS ym, COALESCE(SUM(amount), 0)
           FROM expenses WHERE expense_date >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}

    data = [{
        'month': m.strftime('%b %Y'),
        'income': round(manual_income_by_month.get(m.strftime('%Y-%m'), 0)
                         + repayment_income_by_month.get(m.strftime('%Y-%m'), 0), 2),
        'expenses': round(expenses_by_month.get(m.strftime('%Y-%m'), 0), 2),
    } for m in months]

    return jsonify(data)


@dashboard_bp.route('/member-growth')
@login_required
def member_growth():
    """New members registered per month - last 6 months (see loan_trend's
    docstring for why SUBSTRING-on-text is used for the month bucketing)."""
    db = get_db()
    window_start, months = _month_window(6)

    members_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(created_at, 1, 7) AS ym, COUNT(*)
           FROM members WHERE created_at >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}
    clients_by_month = {r[0]: r[1] for r in db.execute(
        """SELECT SUBSTRING(created_at, 1, 7) AS ym, COUNT(*)
           FROM clients WHERE created_at >= %s GROUP BY ym""",
        (window_start.isoformat(),)
    ).fetchall()}

    data = [{
        'month': m.strftime('%b %Y'),
        'members': members_by_month.get(m.strftime('%Y-%m'), 0),
        'clients': clients_by_month.get(m.strftime('%Y-%m'), 0),
    } for m in months]

    return jsonify(data)


@dashboard_bp.route('/recent-activities')
@login_required
def recent_activities():
    logs = get_db().execute(
        """SELECT audit_logs.*, users.full_name AS user_name FROM audit_logs
           LEFT JOIN users ON users.id = audit_logs.user_id
           ORDER BY audit_logs.created_at DESC LIMIT 20"""
    ).fetchall()
    return jsonify([audit_log_public(l) for l in logs])


def _borrower_names(db, member_ids, client_ids):
    """Batch-resolves borrower display names for a page of loans/schedules --
    one IN(...) query against members and one against clients, instead of a
    query per row. Returns (members_by_id, clients_by_id) dicts of id ->
    full name, so callers can just do
    member_names.get(member_id) or client_names.get(client_id) or 'N/A'."""
    member_ids = [i for i in set(member_ids) if i]
    client_ids = [i for i in set(client_ids) if i]

    members_by_id = {}
    if member_ids:
        placeholders = ','.join(['%s'] * len(member_ids))
        for m in db.execute(f"SELECT * FROM members WHERE id IN ({placeholders})", member_ids).fetchall():
            members_by_id[m['id']] = member_full_name(m)

    clients_by_id = {}
    if client_ids:
        placeholders = ','.join(['%s'] * len(client_ids))
        for c in db.execute(f"SELECT * FROM clients WHERE id IN ({placeholders})", client_ids).fetchall():
            clients_by_id[c['id']] = client_full_name(c)

    return members_by_id, clients_by_id


@dashboard_bp.route('/due-today')
@login_required
def due_today():
    db = get_db()
    today = date.today().isoformat()
    schedules = db.execute(
        """SELECT loan_schedules.*, loans.loan_number, loans.member_id, loans.client_id
           FROM loan_schedules LEFT JOIN loans ON loans.id = loan_schedules.loan_id
           WHERE loan_schedules.due_date = %s AND loan_schedules.status IN ('pending', 'partial')
           LIMIT 10""", (today,)
    ).fetchall()

    member_names, client_names = _borrower_names(
        db, [s['member_id'] for s in schedules], [s['client_id'] for s in schedules])

    result = []
    for s in schedules:
        borrower = member_names.get(s['member_id']) or client_names.get(s['client_id']) or 'N/A'
        result.append({
            'loan_number': s['loan_number'],
            'borrower': borrower,
            'amount_due': s['total_due'],
            'installment': s['installment_number']
        })

    return jsonify(result)


@dashboard_bp.route('/overdue-loans')
@login_required
def overdue_loans():
    db = get_db()
    today = date.today()
    loan_ids = [r[0] for r in db.execute(
        """SELECT DISTINCT loan_id FROM loan_schedules
           WHERE due_date < %s AND status IN ('pending', 'partial')""", (today.isoformat(),)
    ).fetchall()]

    result = []
    if loan_ids:
        placeholders = ','.join(['%s'] * len(loan_ids))
        loans = db.execute(
            f"SELECT * FROM loans WHERE id IN ({placeholders}) AND status = 'active' LIMIT 10", loan_ids
        ).fetchall()

        member_names, client_names = _borrower_names(
            db, [l['member_id'] for l in loans], [l['client_id'] for l in loans])

        # One grouped query for every displayed loan's earliest overdue date,
        # instead of a separate MIN(due_date) round trip per loan.
        earliest_by_loan = {}
        if loans:
            loan_placeholders = ','.join(['%s'] * len(loans))
            rows = db.execute(
                f"""SELECT loan_id, MIN(due_date) FROM loan_schedules
                    WHERE loan_id IN ({loan_placeholders}) AND due_date < %s AND status IN ('pending', 'partial')
                    GROUP BY loan_id""",
                [l['id'] for l in loans] + [today.isoformat()]
            ).fetchall()
            earliest_by_loan = {r[0]: r[1] for r in rows}

        for loan in loans:
            borrower = member_names.get(loan['member_id']) or client_names.get(loan['client_id']) or 'N/A'
            earliest_overdue = earliest_by_loan.get(loan['id'])
            overdue_days = (today - date.fromisoformat(earliest_overdue)).days if earliest_overdue else 0
            result.append({
                'loan_number': loan['loan_number'],
                'borrower': borrower,
                'outstanding': loan['outstanding_balance'],
                'overdue_days': overdue_days
            })

    return jsonify(result)


@dashboard_bp.route('/search')
@login_required
def search():
    """Global quick-search backing the navbar search box (static/js/app.js,
    initGlobalSearch). Matches members and clients by name/number/phone/
    national ID, and loans by loan number or borrower name -- each capped
    to a handful of results so the dropdown stays scannable. ILIKE (rather
    than the case-sensitive LIKE the per-page list filters use) so casing
    doesn't matter for a box meant for fast, casual lookups."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'members': [], 'clients': [], 'loans': []})

    db = get_db()
    like = f'%{q}%'
    limit = 6

    member_rows = db.execute(
        """SELECT id, member_number, first_name, middle_name, last_name, phone, status
           FROM members
           WHERE first_name ILIKE %s OR last_name ILIKE %s OR member_number ILIKE %s
              OR phone ILIKE %s OR national_id ILIKE %s
           ORDER BY created_at DESC LIMIT %s""",
        (like, like, like, like, like, limit)
    ).fetchall()

    client_rows = db.execute(
        """SELECT id, client_number, first_name, last_name, phone, status
           FROM clients
           WHERE first_name ILIKE %s OR last_name ILIKE %s OR client_number ILIKE %s
              OR phone ILIKE %s OR national_id ILIKE %s
           ORDER BY created_at DESC LIMIT %s""",
        (like, like, like, like, like, limit)
    ).fetchall()

    loan_rows = db.execute(
        """SELECT loans.id, loans.loan_number, loans.status, loans.outstanding_balance,
                  COALESCE(
                      NULLIF(TRIM(members.first_name || ' ' || COALESCE(members.middle_name, '') || ' ' || members.last_name), ''),
                      NULLIF(TRIM(clients.first_name || ' ' || clients.last_name), '')
                  ) AS borrower_name
           FROM loans
           LEFT JOIN members ON members.id = loans.member_id
           LEFT JOIN clients ON clients.id = loans.client_id
           WHERE loans.loan_number ILIKE %s
              OR members.first_name ILIKE %s OR members.last_name ILIKE %s
              OR clients.first_name ILIKE %s OR clients.last_name ILIKE %s
           ORDER BY loans.created_at DESC LIMIT %s""",
        (like, like, like, like, like, limit)
    ).fetchall()

    return jsonify({
        'members': [{
            'id': r['id'],
            'full_name': member_full_name(r),
            'member_number': r['member_number'],
            'phone': r['phone'],
            'status': r['status'],
            'url': url_for('members.detail', member_id=r['id'])
        } for r in member_rows],
        'clients': [{
            'id': r['id'],
            'full_name': client_full_name(r),
            'client_number': r['client_number'],
            'phone': r['phone'],
            'status': r['status'],
            'url': url_for('clients.detail', client_id=r['id'])
        } for r in client_rows],
        'loans': [{
            'id': r['id'],
            'loan_number': r['loan_number'],
            'borrower_name': r['borrower_name'] or 'N/A',
            'outstanding_balance': r['outstanding_balance'],
            'status': r['status'],
            'url': url_for('loans.detail', loan_id=r['id'])
        } for r in loan_rows]
    })


@dashboard_bp.route('/notifications')
@login_required
def notifications():
    user = get_current_user()
    notifs = get_db().execute(
        """SELECT * FROM notifications WHERE user_id = %s AND is_read = 0
           ORDER BY created_at DESC LIMIT 10""", (user['id'],)
    ).fetchall()
    return jsonify([notification_public(n) for n in notifs])


@dashboard_bp.route('/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_read(notif_id):
    user = get_current_user()
    execute("UPDATE notifications SET is_read = 1 WHERE id = %s AND user_id = %s", (notif_id, user['id']))
    return jsonify({'message': 'Marked as read'})


@dashboard_bp.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def mark_all_read():
    user = get_current_user()
    execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (user['id'],))
    return jsonify({'message': 'All marked as read'})
