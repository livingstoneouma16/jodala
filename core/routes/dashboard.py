from flask import Blueprint, render_template, jsonify
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


@dashboard_bp.route('/loan-trend')
@login_required
def loan_trend():
    """Monthly loan disbursement trend - last 12 months"""
    db = get_db()
    today = date.today()
    data = []

    for i in range(11, -1, -1):
        month_start = add_months(today.replace(day=1), -i)
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)

        disbursed = db.execute(
            "SELECT COALESCE(SUM(amount_disbursed), 0) FROM loans WHERE disbursement_date >= %s AND disbursement_date < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]
        collected = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM repayments WHERE payment_date >= %s AND payment_date < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]

        data.append({
            'month': month_start.strftime('%b %Y'),
            'disbursed': round(disbursed, 2),
            'collected': round(collected, 2)
        })

    return jsonify(data)


@dashboard_bp.route('/loan-status-distribution')
@login_required
def loan_status_distribution():
    rows = get_db().execute("SELECT status, COUNT(*) FROM loans GROUP BY status").fetchall()
    return jsonify([{'status': r[0], 'count': r[1]} for r in rows])


@dashboard_bp.route('/income-expense-trend')
@login_required
def income_expense_trend():
    """Monthly income vs expenses - last 6 months"""
    db = get_db()
    today = date.today()
    data = []

    for i in range(5, -1, -1):
        month_start = add_months(today.replace(day=1), -i)
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)

        manual_income = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM income WHERE income_date >= %s AND income_date < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]
        repayment_income = db.execute(
            """SELECT COALESCE(SUM(interest_portion + penalty_portion), 0)
               FROM repayments WHERE payment_date >= %s AND payment_date < %s""",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]
        expenses = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE expense_date >= %s AND expense_date < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]

        data.append({
            'month': month_start.strftime('%b %Y'),
            'income': round(manual_income + repayment_income, 2),
            'expenses': round(expenses, 2)
        })

    return jsonify(data)


@dashboard_bp.route('/member-growth')
@login_required
def member_growth():
    """New members registered per month - last 6 months"""
    db = get_db()
    today = date.today()
    data = []

    for i in range(5, -1, -1):
        month_start = add_months(today.replace(day=1), -i)
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)

        members = db.execute(
            "SELECT COUNT(*) FROM members WHERE created_at >= %s AND created_at < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]
        clients = db.execute(
            "SELECT COUNT(*) FROM clients WHERE created_at >= %s AND created_at < %s",
            (month_start.isoformat(), next_month.isoformat())
        ).fetchone()[0]

        data.append({
            'month': month_start.strftime('%b %Y'),
            'members': members,
            'clients': clients
        })

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

    result = []
    for s in schedules:
        borrower = 'N/A'
        if s['member_id']:
            m = db.execute("SELECT * FROM members WHERE id = %s", (s['member_id'],)).fetchone()
            if m:
                borrower = member_full_name(m)
        elif s['client_id']:
            c = db.execute("SELECT * FROM clients WHERE id = %s", (s['client_id'],)).fetchone()
            if c:
                borrower = client_full_name(c)
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
        for loan in loans:
            borrower = 'N/A'
            if loan['member_id']:
                m = db.execute("SELECT * FROM members WHERE id = %s", (loan['member_id'],)).fetchone()
                if m:
                    borrower = member_full_name(m)
            elif loan['client_id']:
                c = db.execute("SELECT * FROM clients WHERE id = %s", (loan['client_id'],)).fetchone()
                if c:
                    borrower = client_full_name(c)
            earliest_overdue = db.execute(
                """SELECT MIN(due_date) FROM loan_schedules
                   WHERE loan_id = %s AND due_date < %s AND status IN ('pending', 'partial')""",
                (loan['id'], today.isoformat())
            ).fetchone()[0]
            overdue_days = (today - date.fromisoformat(earliest_overdue)).days if earliest_overdue else 0
            result.append({
                'loan_number': loan['loan_number'],
                'borrower': borrower,
                'outstanding': loan['outstanding_balance'],
                'overdue_days': overdue_days
            })

    return jsonify(result)


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
