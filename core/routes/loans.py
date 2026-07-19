import json

from flask import Blueprint, request, jsonify, render_template
from datetime import date, timedelta

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, get_current_user
from core.calculator import loan_summary, build_loan_schedule, add_months
from core.serializers import loan_public, loan_schedule_public, repayment_public, member_full_name, client_full_name
from core.utils import (generate_loan_number, log_audit, adjust_main_account_balance,
                        adjust_account_balance, notify, format_currency)


def _borrower_contact(loan_row):
    """Resolve (name, email) for whoever this loan belongs to -- a member or a client."""
    db = get_db()
    if loan_row['member_id']:
        p = db.execute("SELECT first_name, last_name, email FROM members WHERE id = %s",
                        (loan_row['member_id'],)).fetchone()
    elif loan_row['client_id']:
        p = db.execute("SELECT first_name, last_name, email FROM clients WHERE id = %s",
                        (loan_row['client_id'],)).fetchone()
    else:
        return None, None
    if not p:
        return None, None
    return f"{p['first_name']} {p['last_name']}", p['email']

loans_bp = Blueprint('loans', __name__)


def _one_period_before(d, frequency):
    """One repayment period before date `d`. For monthly loans this must be
    calendar-month-aware (via add_months), not a flat 30-day shift -- a flat
    shift drifts by 1-3 days in any month that isn't exactly 30 days long,
    which throws off the first generated schedule row so it no longer lines
    up with the loan's own first_repayment_date."""
    if frequency == 'daily':
        return d - timedelta(days=1)
    elif frequency == 'weekly':
        return d - timedelta(weeks=1)
    return add_months(d, -1)


def _borrower_name_sql():
    """SELECT fragment producing a joined borrower_name + product_name for a loan row."""
    return """
        SELECT loans.*,
               COALESCE(
                   TRIM(members.first_name || ' ' || COALESCE(members.middle_name, '') || ' ' || members.last_name),
                   TRIM(clients.first_name || ' ' || clients.last_name)
               ) AS borrower_name,
               loan_products.name AS product_name
        FROM loans
        LEFT JOIN members ON members.id = loans.member_id
        LEFT JOIN clients ON clients.id = loans.client_id
        LEFT JOIN loan_products ON loan_products.id = loans.product_id
    """


@loans_bp.route('/')
@login_required
def index():
    products = get_db().execute("SELECT * FROM loan_products WHERE is_active = 1").fetchall()
    return render_template('loans/index.html', user=get_current_user(),
                            products=products)


@loans_bp.route('/apply')
@login_required
def apply_page():
    db = get_db()
    products = db.execute("SELECT * FROM loan_products WHERE is_active = 1").fetchall()
    members = db.execute("SELECT * FROM members WHERE status = 'active'").fetchall()
    clients = db.execute("SELECT * FROM clients WHERE status = 'active'").fetchall()
    return render_template('loans/apply.html', user=get_current_user(),
                            products=products, members=members, clients=clients)


@loans_bp.route('/api/quote', methods=['POST'])
@login_required
def quote_loan():
    """Preview the summary numbers (installment, total interest/repayable, fees)
    for a proposed loan before submitting the application."""
    data = request.get_json()
    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (data.get('product_id'),)).fetchone()
    if not product:
        return jsonify({'error': 'Invalid loan product'}), 400

    principal = float(data.get('principal_amount', 0))
    term = int(data.get('term', 0))

    if principal < product['min_amount'] or principal > product['max_amount']:
        return jsonify({'error': f"Amount must be between {product['min_amount']} and {product['max_amount']}"}), 400
    if term < product['min_term'] or term > product['max_term']:
        return jsonify({'error': f"Term must be between {product['min_term']} and {product['max_term']}"}), 400

    summary = loan_summary(principal, product['interest_rate'], term, product['interest_type'],
                            product['insurance_fee'])
    return jsonify(summary)


@loans_bp.route('/api', methods=['GET'])
@login_required
def list_loans():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    product_id = request.args.get('product_id', '')

    where, params = [], []
    if search:
        where.append("loans.loan_number LIKE %s")
        params.append(f'%{search}%')
    if status:
        where.append("loans.status = %s")
        params.append(status)
    if product_id:
        where.append("loans.product_id = %s")
        params.append(int(product_id))

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base = _borrower_name_sql() + where_sql + " ORDER BY loans.created_at DESC"

    total = get_db().execute(f"SELECT COUNT(*) FROM loans{where_sql}", tuple(params)).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    rows = get_db().execute(base + " LIMIT %s OFFSET %s", tuple(params) + (per_page, offset)).fetchall()

    return jsonify({
        'loans': [loan_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@loans_bp.route('/api', methods=['POST'])
@login_required
def create_loan():
    data = request.get_json()
    user = get_current_user()

    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (data.get('product_id'),)).fetchone()
    if not product:
        return jsonify({'error': 'Invalid loan product'}), 400

    principal = float(data.get('principal_amount', 0))
    term = int(data.get('term', 0))

    if principal < product['min_amount'] or principal > product['max_amount']:
        return jsonify({'error': f"Amount must be between {product['min_amount']} and {product['max_amount']}"}), 400
    if term < product['min_term'] or term > product['max_term']:
        return jsonify({'error': f"Term must be between {product['min_term']} and {product['max_term']}"}), 400

    summary = loan_summary(principal, product['interest_rate'], term, product['interest_type'],
                            product['insurance_fee'])

    borrower_type = data.get('borrower_type', 'member')
    now = utcnow()

    cur = execute(
        """INSERT INTO loans (loan_number, member_id, client_id, product_id, borrower_type,
               principal_amount, interest_rate, interest_type, term, repayment_frequency,
               total_interest, total_repayable, insurance_fee, outstanding_balance,
               purpose, collateral, guarantor_name, guarantor_phone, status, application_date,
               loan_officer_id, notes, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)""",
        (generate_loan_number(),
         data.get('member_id') if borrower_type == 'member' else None,
         data.get('client_id') if borrower_type == 'client' else None,
         product['id'], borrower_type, principal, product['interest_rate'], product['interest_type'],
         term, product['repayment_frequency'], summary['total_interest'], summary['total_repayable'],
         summary['insurance_fee'], summary['total_repayable'],
         data.get('purpose'), data.get('collateral'), data.get('guarantor_name'), data.get('guarantor_phone'),
         date.today().isoformat(), user['id'], data.get('notes'), now, now)
    )
    loan = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (cur.lastrowid,)).fetchone()
    log_audit('LOAN_APPLICATION', 'loan', loan['id'])

    return jsonify({'message': 'Loan application submitted', 'loan': loan_public(loan)}), 201


@loans_bp.route('/api/<int:loan_id>', methods=['GET'])
@login_required
def get_loan(loan_id):
    loan = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404

    data = loan_public(loan)

    schedule = get_db().execute(
        "SELECT * FROM loan_schedules WHERE loan_id = %s ORDER BY installment_number", (loan_id,)
    ).fetchall()
    data['schedule'] = [loan_schedule_public(s) for s in schedule]

    repayments = get_db().execute(
        "SELECT * FROM repayments WHERE loan_id = %s ORDER BY created_at", (loan_id,)
    ).fetchall()
    data['repayments'] = [repayment_public({**dict(r), 'loan_number': loan['loan_number']}) for r in repayments]

    if loan['member_id']:
        member = get_db().execute("SELECT * FROM members WHERE id = %s", (loan['member_id'],)).fetchone()
        if member:
            data['borrower_details'] = {
                'type': 'member', 'name': member_full_name(member),
                'phone': member['phone'], 'member_number': member['member_number']
            }
    elif loan['client_id']:
        client = get_db().execute("SELECT * FROM clients WHERE id = %s", (loan['client_id'],)).fetchone()
        if client:
            data['borrower_details'] = {
                'type': 'client', 'name': client_full_name(client),
                'phone': client['phone'], 'client_number': client['client_number']
            }

    return jsonify(data)


@loans_bp.route('/api/<int:loan_id>/approve', methods=['POST'])
@login_required
@role_required('admin', 'loan_officer')
def approve_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'pending':
        return jsonify({'error': 'Loan is not in pending status'}), 400

    user = get_current_user()
    execute(
        "UPDATE loans SET status = 'approved', approval_date = %s, approved_by = %s, updated_at = %s WHERE id = %s",
        (date.today().isoformat(), user['id'], utcnow(), loan_id)
    )
    log_audit('LOAN_APPROVED', 'loan', loan_id)
    updated = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()

    borrower_name, borrower_email = _borrower_contact(loan)
    notify(
        loan['loan_officer_id'],
        'Loan Approved',
        f"Loan {loan['loan_number']} for {borrower_name or 'the borrower'} has been approved.",
        notification_type='success', related_type='loan', related_id=loan_id,
        email=borrower_email,
        email_subject=f"Your loan {loan['loan_number']} has been approved",
        email_body_html=(
            f"<p>Dear {borrower_name or 'Customer'},</p>"
            f"<p>Good news -- your loan application <strong>{loan['loan_number']}</strong> "
            f"for <strong>{format_currency(loan['principal_amount'])}</strong> has been "
            f"<strong>approved</strong>. It will be disbursed shortly.</p>"
            f"<p>Thank you for banking with us.</p>"
        )
    )
    return jsonify({'message': 'Loan approved', 'loan': loan_public(updated)})


@loans_bp.route('/api/<int:loan_id>/reject', methods=['POST'])
@login_required
def reject_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] not in ('pending', 'approved'):
        return jsonify({'error': 'Cannot reject this loan'}), 400

    data = request.get_json()
    execute(
        "UPDATE loans SET status = 'rejected', rejection_reason = %s, updated_at = %s WHERE id = %s",
        (data.get('reason', ''), utcnow(), loan_id)
    )
    log_audit('LOAN_REJECTED', 'loan', loan_id)

    borrower_name, borrower_email = _borrower_contact(loan)
    reason = data.get('reason', '')
    notify(
        loan['loan_officer_id'],
        'Loan Rejected',
        f"Loan {loan['loan_number']} for {borrower_name or 'the borrower'} was rejected." +
        (f" Reason: {reason}" if reason else ''),
        notification_type='warning', related_type='loan', related_id=loan_id,
        email=borrower_email,
        email_subject=f"Update on your loan application {loan['loan_number']}",
        email_body_html=(
            f"<p>Dear {borrower_name or 'Customer'},</p>"
            f"<p>We're sorry to inform you that your loan application "
            f"<strong>{loan['loan_number']}</strong> was not approved."
            + (f"<br>Reason: {reason}</p>" if reason else "</p>")
            + "<p>Please contact us if you have any questions.</p>"
        )
    )
    return jsonify({'message': 'Loan rejected'})


@loans_bp.route('/api/<int:loan_id>/disburse', methods=['POST'])
@login_required
def disburse_loan(loan_id):
    data = request.get_json() or {}
    user = get_current_user()
    try:
        updated = _disburse_loan(
            loan_id=loan_id,
            user_id=user['id'],
            disbursement_method=data.get('disbursement_method', 'cash'),
            disbursement_date=data.get('disbursement_date'),
            first_repayment_date=data.get('first_repayment_date'),
        )
    except _DisbursementError as e:
        return jsonify({'error': str(e)}), e.status_code
    return jsonify({'message': 'Loan disbursed successfully', 'loan': loan_public(updated)})


class _DisbursementError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _disburse_loan(loan_id, user_id, disbursement_method='cash', disbursement_date=None,
                    first_repayment_date=None, mpesa_receipt=None):
    """Core disbursement logic, shared by the manual "Disburse" API endpoint
    and the M-Pesa B2C result callback (app/routes/mpesa.py) so a loan sent
    out via M-Pesa posts to the schedule and accounting exactly the same way
    as one disbursed by cash/bank. Raises _DisbursementError on validation
    failure; returns the updated loan row on success."""
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        raise _DisbursementError('Loan not found', 404)
    if loan['status'] != 'approved':
        raise _DisbursementError('Loan must be approved before disbursement', 400)

    disbursement_date = date.fromisoformat(disbursement_date or date.today().isoformat())
    first_repayment = date.fromisoformat(
        first_repayment_date or (disbursement_date + timedelta(days=30)).isoformat()
    )
    # For a top-up loan, part of principal_amount is just the parent loan's
    # unpaid balance rolled over on paper -- no new cash moves for that
    # portion, so it must not be deducted from the main account again.
    amount_disbursed = (loan['principal_amount'] - loan['rollover_amount']
                         - loan['insurance_fee'])

    schedule_data = build_loan_schedule(
        loan['principal_amount'], loan['interest_rate'], loan['term'], loan['interest_type'],
        loan['repayment_frequency'], _one_period_before(first_repayment, loan['repayment_frequency'])
    )
    expected_end_date = schedule_data[-1]['due_date'].isoformat() if schedule_data else None

    execute(
        """UPDATE loans SET status = 'active', disbursement_date = %s, first_repayment_date = %s,
               amount_disbursed = %s, disbursed_by = %s, disbursement_method = %s, expected_end_date = %s,
               updated_at = %s
           WHERE id = %s""",
        (disbursement_date.isoformat(), first_repayment.isoformat(), amount_disbursed,
         user_id, disbursement_method, expected_end_date, utcnow(), loan_id)
    )

    for s in schedule_data:
        execute(
            """INSERT INTO loan_schedules (loan_id, installment_number, due_date, principal_due,
                   interest_due, total_due, balance_after, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')""",
            (loan_id, s['installment_number'], s['due_date'].isoformat(),
             s['principal_due'], s['interest_due'], s['total_due'], s['balance_after'])
        )

    log_audit('LOAN_DISBURSED', 'loan', loan_id)
    adjust_main_account_balance(-amount_disbursed)
    # Loans Receivable only grows by the *new* money owed -- for a top-up,
    # the rollover portion was already on the books from the parent loan's
    # own disbursement, so re-adding the full new principal would double
    # count it. The insurance fee is money the SACCO earns up-front
    # (deducted from cash disbursed rather than paid out), so it posts as
    # Fee Income; the remainder is the genuine new receivable.
    receivable_increase = loan['principal_amount'] - loan['rollover_amount']
    fees = loan['insurance_fee'] or 0
    adjust_account_balance('1100', receivable_increase)
    if fees:
        adjust_account_balance('4100', fees)
    updated = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()

    borrower_name, borrower_email = _borrower_contact(loan)
    receipt_note = f" (M-Pesa receipt {mpesa_receipt})" if mpesa_receipt else ""
    notify(
        loan['loan_officer_id'],
        'Loan Disbursed',
        f"Loan {loan['loan_number']} for {borrower_name or 'the borrower'} "
        f"({format_currency(amount_disbursed)}) has been disbursed{receipt_note}.",
        notification_type='success', related_type='loan', related_id=loan_id,
        email=borrower_email,
        email_subject=f"Your loan {loan['loan_number']} has been disbursed",
        email_body_html=(
            f"<p>Dear {borrower_name or 'Customer'},</p>"
            f"<p><strong>{format_currency(amount_disbursed)}</strong> has been disbursed to you "
            f"for loan <strong>{loan['loan_number']}</strong> on {disbursement_date.isoformat()}.</p>"
            f"<p>Your first repayment is due on <strong>{first_repayment.isoformat()}</strong>.</p>"
            f"<p>Thank you for banking with us.</p>"
        )
    )
    return updated


def send_overdue_reminders():
    """Email every borrower with at least one overdue installment on an
    active loan. Safe to call repeatedly (e.g. once a day via cron/scheduler)
    -- it just re-sends a reminder each time it's run for loans still
    overdue, so callers should only invoke this once per day.
    Returns a summary dict.
    """
    from core.utils import get_overdue_loan_ids
    db = get_db()
    loan_ids = get_overdue_loan_ids()
    sent, skipped = 0, 0
    for loan_id in loan_ids:
        loan = db.execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
        if not loan or loan['status'] != 'active':
            continue
        overdue_total = db.execute(
            """SELECT COALESCE(SUM(total_due - total_paid), 0) AS amt, COUNT(*) AS cnt
               FROM loan_schedules WHERE loan_id = %s AND due_date < %s AND status IN ('pending', 'partial')""",
            (loan_id, date.today().isoformat())
        ).fetchone()
        borrower_name, borrower_email = _borrower_contact(loan)
        if not borrower_email:
            skipped += 1
            continue
        notify(
            loan['loan_officer_id'],
            'Loan Overdue',
            f"Loan {loan['loan_number']} for {borrower_name or 'the borrower'} has "
            f"{overdue_total['cnt']} overdue installment(s) totalling {format_currency(overdue_total['amt'])}.",
            notification_type='warning', related_type='loan', related_id=loan_id,
            email=borrower_email,
            email_subject=f"Overdue payment reminder - Loan {loan['loan_number']}",
            email_body_html=(
                f"<p>Dear {borrower_name or 'Customer'},</p>"
                f"<p>This is a reminder that loan <strong>{loan['loan_number']}</strong> has "
                f"<strong>{overdue_total['cnt']}</strong> overdue installment(s) totalling "
                f"<strong>{format_currency(overdue_total['amt'])}</strong>.</p>"
                f"<p>Please make payment as soon as possible to avoid penalties.</p>"
            )
        )
        sent += 1
    return {'loans_checked': len(loan_ids), 'reminders_sent': sent, 'skipped_no_email': skipped}


@loans_bp.route('/api/send-overdue-reminders', methods=['POST'])
@login_required
@role_required('admin')
def trigger_overdue_reminders():
    """Manually trigger overdue-reminder emails (also runnable via
    `python send_overdue_reminders.py` / cron -- see that script)."""
    result = send_overdue_reminders()
    log_audit('OVERDUE_REMINDERS_SENT', 'loan', None, new_values=result)
    return jsonify({'message': 'Overdue reminders processed', **result})


@loans_bp.route('/api/<int:loan_id>/topup', methods=['POST'])
@login_required
def topup_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'active':
        return jsonify({'error': 'Can only top-up active loans'}), 400

    product = get_db().execute("SELECT * FROM loan_products WHERE id = %s", (loan['product_id'],)).fetchone()
    data = request.get_json()
    user = get_current_user()

    topup_amount = float(data.get('topup_amount', 0))
    if topup_amount <= 0:
        return jsonify({'error': 'Top-up amount must be greater than zero'}), 400

    # Top-up rule: the new principal absorbs the *remaining principal* plus
    # the extra cash out. outstanding_balance is total_repayable - total_paid,
    # which still includes unpaid interest -- using it directly overstates
    # the new principal by whatever interest hasn't been paid yet. The true
    # remaining principal is the sum of each schedule row's unpaid principal.
    remaining_principal_row = get_db().execute(
        "SELECT COALESCE(SUM(principal_due - principal_paid), 0) AS remaining "
        "FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()
    remaining_principal = remaining_principal_row['remaining']
    new_principal = remaining_principal + topup_amount

    # Terms (term length, interest rate/type, repayment frequency) stay
    # exactly as they were originally applied -- NOT re-pulled from the
    # product (which may have changed since) and NOT overridable via the
    # request body.
    term = loan['term']
    interest_rate = loan['interest_rate']
    interest_type = loan['interest_type']
    repayment_frequency = loan['repayment_frequency']

    summary = loan_summary(new_principal, interest_rate, term, interest_type,
                            product['insurance_fee'])
    now = utcnow()
    today = date.today()

    # Rebuild the repayment schedule from today for the remaining term,
    # replacing whatever was left of the old one -- the loan keeps its
    # original id/loan_number, it's simply re-amortized on the bigger
    # principal instead of spawning a new loan record.
    execute("DELETE FROM loan_schedules WHERE loan_id = %s AND status IN ('pending', 'partial')", (loan_id,))
    already_paid_principal = get_db().execute(
        "SELECT COALESCE(SUM(principal_paid), 0) AS p FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()['p']
    already_paid_interest = get_db().execute(
        "SELECT COALESCE(SUM(interest_paid), 0) AS i FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()['i']

    schedule_data = build_loan_schedule(
        new_principal, interest_rate, term, interest_type, repayment_frequency,
        _one_period_before(today, repayment_frequency)
    )
    for s in schedule_data:
        execute(
            """INSERT INTO loan_schedules (loan_id, installment_number, due_date, principal_due,
                   interest_due, total_due, balance_after, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')""",
            (loan_id, s['installment_number'], s['due_date'].isoformat(),
             s['principal_due'], s['interest_due'], s['total_due'], s['balance_after'])
        )
    expected_end_date = schedule_data[-1]['due_date'].isoformat() if schedule_data else loan['expected_end_date']

    new_total_repayable = summary['total_repayable']
    new_total_paid = round(already_paid_principal + already_paid_interest, 2)
    new_outstanding = round(max(0, new_total_repayable - new_total_paid), 2)

    execute(
        """UPDATE loans SET principal_amount = %s, total_interest = %s, total_repayable = %s,
               insurance_fee = %s, outstanding_balance = %s, total_paid = %s,
               amount_disbursed = amount_disbursed + %s, expected_end_date = %s,
               is_topup = 1, rollover_amount = %s, updated_at = %s
           WHERE id = %s""",
        (new_principal, summary['total_interest'], new_total_repayable,
         summary['insurance_fee'], new_outstanding, new_total_paid,
         topup_amount, expected_end_date, remaining_principal, now, loan_id)
    )

    log_audit('LOAN_TOPUP', 'loan', loan_id,
              old_values={'principal_amount': loan['principal_amount'], 'outstanding_balance': loan['outstanding_balance']},
              new_values={'topup_amount': topup_amount, 'new_principal': new_principal})

    # Cash goes out for the extra amount, and Loans Receivable grows by the
    # same amount -- the rollover portion was already on the books, so only
    # the genuinely new money moves here.
    adjust_main_account_balance(-topup_amount)
    adjust_account_balance('1100', topup_amount)

    updated = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()
    return jsonify({'message': 'Loan topped up', 'loan': loan_public(updated)}), 200


@loans_bp.route('/api/<int:loan_id>/restructure', methods=['POST'])
@login_required
@role_required('admin', 'loan_officer')
def restructure_loan(loan_id):
    """Formal restructuring for a borrower in genuine distress: term, interest
    rate, interest type and repayment frequency can all change together, and
    the remaining principal is re-amortized over the new terms -- unlike
    top-up (adds fresh cash) or extend (term only, nothing else changes).

    Unlike top-up/extend, a `reason` is mandatory and a full before/after
    snapshot (old terms + the schedule rows being replaced) is written to
    loan_restructures so there's a durable record of what the loan looked
    like pre-restructure, not just a one-line audit_logs entry.

    No cash moves and no accounting entries are posted -- restructuring
    re-negotiates terms on money already disbursed, it doesn't disburse
    anything new."""
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'active':
        return jsonify({'error': 'Can only restructure active loans'}), 400

    data = request.get_json() or {}
    user = get_current_user()

    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': 'A reason is required to restructure a loan'}), 400

    new_term = int(data.get('new_term') or loan['term'])
    new_interest_rate = float(data.get('new_interest_rate') or loan['interest_rate'])
    new_interest_type = data.get('new_interest_type') or loan['interest_type']
    new_repayment_frequency = data.get('new_repayment_frequency') or loan['repayment_frequency']

    if new_term <= 0:
        return jsonify({'error': 'New term must be greater than zero'}), 400
    if new_interest_rate < 0:
        return jsonify({'error': 'New interest rate cannot be negative'}), 400
    if new_interest_type not in ('flat', 'reducing'):
        return jsonify({'error': "New interest type must be 'flat' or 'reducing'"}), 400
    if new_repayment_frequency not in ('daily', 'weekly', 'monthly'):
        return jsonify({'error': "New repayment frequency must be 'daily', 'weekly' or 'monthly'"}), 400

    # Same "true remaining principal" logic top-up uses: outstanding_balance
    # still includes unpaid interest, so it overstates what's actually owed
    # in principal. Sum each schedule row's unpaid principal instead.
    remaining_principal_row = get_db().execute(
        "SELECT COALESCE(SUM(principal_due - principal_paid), 0) AS remaining "
        "FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()
    remaining_principal = remaining_principal_row['remaining']
    if remaining_principal <= 0:
        return jsonify({'error': 'Loan has no remaining principal to restructure'}), 400

    # Snapshot the schedule rows about to be replaced (pending/partial only --
    # fully-paid rows are left untouched in loan_schedules, same as top-up)
    # so the exact pre-restructure schedule is recoverable later.
    old_schedule_rows = get_db().execute(
        "SELECT * FROM loan_schedules WHERE loan_id = %s AND status IN ('pending', 'partial') "
        "ORDER BY installment_number", (loan_id,)
    ).fetchall()
    old_schedule_snapshot = json.dumps([dict(r) for r in old_schedule_rows], default=str)

    already_paid_principal = get_db().execute(
        "SELECT COALESCE(SUM(principal_paid), 0) AS p FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()['p']
    already_paid_interest = get_db().execute(
        "SELECT COALESCE(SUM(interest_paid), 0) AS i FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()['i']

    now = utcnow()
    today = date.today()

    execute("DELETE FROM loan_schedules WHERE loan_id = %s AND status IN ('pending', 'partial')", (loan_id,))

    schedule_data = build_loan_schedule(
        remaining_principal, new_interest_rate, new_term, new_interest_type, new_repayment_frequency,
        _one_period_before(today, new_repayment_frequency)
    )
    for s in schedule_data:
        execute(
            """INSERT INTO loan_schedules (loan_id, installment_number, due_date, principal_due,
                   interest_due, total_due, balance_after, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')""",
            (loan_id, s['installment_number'], s['due_date'].isoformat(),
             s['principal_due'], s['interest_due'], s['total_due'], s['balance_after'])
        )
    new_expected_end_date = schedule_data[-1]['due_date'].isoformat() if schedule_data else loan['expected_end_date']

    # Insurance fee isn't re-charged -- no new money is being disbursed, only
    # the remaining principal is being re-amortized -- so it carries over
    # unchanged rather than being recalculated off the new principal.
    new_total_interest = round(sum(s['interest_due'] for s in schedule_data), 2)
    new_total_repayable = round(remaining_principal + new_total_interest + (loan['insurance_fee'] or 0), 2)
    new_total_paid = round(already_paid_principal + already_paid_interest, 2)
    new_outstanding = round(max(0, new_total_repayable - new_total_paid), 2)

    new_notes = (loan['notes'] or '') + f"\nRestructured: {reason}"

    execute(
        """UPDATE loans SET term = %s, interest_rate = %s, interest_type = %s,
               repayment_frequency = %s, total_interest = %s, total_repayable = %s,
               outstanding_balance = %s, total_paid = %s, expected_end_date = %s,
               is_restructured = 1, restructure_count = COALESCE(restructure_count, 0) + 1,
               notes = %s, updated_at = %s
           WHERE id = %s""",
        (new_term, new_interest_rate, new_interest_type, new_repayment_frequency,
         new_total_interest, new_total_repayable, new_outstanding, new_total_paid,
         new_expected_end_date, new_notes, now, loan_id)
    )

    execute(
        """INSERT INTO loan_restructures (loan_id, reason, old_principal_outstanding, old_term,
               old_interest_rate, old_interest_type, old_repayment_frequency, old_expected_end_date,
               old_schedule_snapshot, new_term, new_interest_rate, new_interest_type,
               new_repayment_frequency, new_expected_end_date, restructured_by, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (loan_id, reason, remaining_principal, loan['term'], loan['interest_rate'],
         loan['interest_type'], loan['repayment_frequency'], loan['expected_end_date'],
         old_schedule_snapshot, new_term, new_interest_rate, new_interest_type,
         new_repayment_frequency, new_expected_end_date, user['id'], now)
    )

    log_audit(
        'LOAN_RESTRUCTURED', 'loan', loan_id,
        old_values={'term': loan['term'], 'interest_rate': loan['interest_rate'],
                    'interest_type': loan['interest_type'],
                    'repayment_frequency': loan['repayment_frequency']},
        new_values={'term': new_term, 'interest_rate': new_interest_rate,
                    'interest_type': new_interest_type,
                    'repayment_frequency': new_repayment_frequency, 'reason': reason}
    )

    updated = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()
    return jsonify({'message': 'Loan restructured', 'loan': loan_public(updated)}), 200


@loans_bp.route('/api/<int:loan_id>/restructures', methods=['GET'])
@login_required
def loan_restructure_history(loan_id):
    """History of past restructures for one loan, most recent first --
    what changed, why, and who approved it."""
    loan = get_db().execute("SELECT id FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404

    rows = get_db().execute(
        """SELECT loan_restructures.*, users.full_name AS restructured_by_name
           FROM loan_restructures
           LEFT JOIN users ON users.id = loan_restructures.restructured_by
           WHERE loan_id = %s ORDER BY created_at DESC""", (loan_id,)
    ).fetchall()

    history = []
    for r in rows:
        d = dict(r)
        d['old_schedule_snapshot'] = json.loads(d['old_schedule_snapshot']) if d.get('old_schedule_snapshot') else []
        history.append(d)

    return jsonify({'restructures': history})


@loans_bp.route('/api/<int:loan_id>/extend', methods=['POST'])
@login_required
def extend_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'active':
        return jsonify({'error': 'Can only extend active loans'}), 400

    data = request.get_json()
    extension_periods = int(data.get('extension_periods', 1))
    new_term = loan['term'] + extension_periods

    new_end_date = None
    if loan['expected_end_date']:
        end_date = date.fromisoformat(loan['expected_end_date'])
        if loan['repayment_frequency'] == 'monthly':
            new_end_date = add_months(end_date, extension_periods)
        elif loan['repayment_frequency'] == 'weekly':
            new_end_date = end_date + timedelta(weeks=extension_periods)
        else:
            new_end_date = end_date + timedelta(days=extension_periods)

    execute(
        "UPDATE loans SET term = %s, expected_end_date = %s, updated_at = %s WHERE id = %s",
        (new_term, new_end_date.isoformat() if new_end_date else loan['expected_end_date'], utcnow(), loan_id)
    )
    log_audit('LOAN_EXTENDED', 'loan', loan_id)

    updated = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()
    return jsonify({'message': f'Loan extended by {extension_periods} periods', 'loan': loan_public(updated)})


@loans_bp.route('/api/<int:loan_id>/write-off', methods=['POST'])
@login_required
@role_required('admin')
def write_off_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'active':
        return jsonify({'error': 'Can only write off active loans'}), 400

    data = request.get_json()

    # Loans Receivable (1100) only ever accumulates *principal* -- interest is
    # recognised as income when it's actually collected (see repayments.py),
    # never booked as a receivable up front -- so the amount still sitting in
    # 1100 for this loan is the unpaid principal, not the full
    # outstanding_balance (which also includes uncollected interest). That's
    # the figure that has to come off the books here, matching how top-up
    # and restructure already compute "true remaining principal".
    remaining_principal_row = get_db().execute(
        "SELECT COALESCE(SUM(principal_due - principal_paid), 0) AS remaining "
        "FROM loan_schedules WHERE loan_id = %s", (loan_id,)
    ).fetchone()
    remaining_principal = round(remaining_principal_row['remaining'], 2)

    new_notes = (loan['notes'] or '') + f"\nWritten off: {data.get('reason', '')}"
    execute(
        """UPDATE loans SET status = 'written_off', outstanding_balance = 0, notes = %s,
               actual_end_date = %s, updated_at = %s WHERE id = %s""",
        (new_notes, date.today().isoformat(), utcnow(), loan_id)
    )
    log_audit('LOAN_WRITTEN_OFF', 'loan', loan_id,
              old_values={'outstanding_balance': loan['outstanding_balance']},
              new_values={'reason': data.get('reason', ''), 'principal_written_off': remaining_principal})

    # No cash moves on a write-off -- it's a paper loss, not a payment -- so
    # unlike disbursement/repayment this never touches the main cash account.
    # It's booked as Debit Loan Write-offs (5100) / Credit Loans Receivable
    # (1100) so the bad debt hits the P&L and the receivable stops being
    # overstated by money that's no longer considered collectible.
    if remaining_principal > 0:
        adjust_account_balance('1100', -remaining_principal)
        adjust_account_balance('5100', remaining_principal)

    return jsonify({'message': 'Loan written off'})


@loans_bp.route('/api/<int:loan_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_loan(loan_id):
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] in ('active', 'completed', 'written_off'):
        return jsonify({'error': 'Cannot delete a disbursed loan. Write it off instead.'}), 400

    execute("DELETE FROM loan_schedules WHERE loan_id = %s", (loan_id,))
    execute("DELETE FROM repayments WHERE loan_id = %s", (loan_id,))
    execute("DELETE FROM loans WHERE id = %s", (loan_id,))
    log_audit('DELETE_LOAN', 'loan', loan_id)

    return jsonify({'message': 'Loan deleted successfully'})


@loans_bp.route('/<int:loan_id>')
@login_required
def detail(loan_id):
    loan = get_db().execute(_borrower_name_sql() + " WHERE loans.id = %s", (loan_id,)).fetchone()
    return render_template('loans/detail.html', user=get_current_user(), loan=loan)
