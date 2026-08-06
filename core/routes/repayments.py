from flask import Blueprint, request, jsonify, render_template, abort
from datetime import date
import json

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, get_current_user
from core.calculator import allocate_payment
from core.serializers import repayment_public, member_full_name, client_full_name
from core.utils import (generate_receipt_number, log_audit, paginate, adjust_main_account_balance,
                        adjust_account_balance, notify, format_currency)
from core.routes.loans import _borrower_name_sql

repayments_bp = Blueprint('repayments', __name__)


@repayments_bp.route('/')
@login_required
def index():
    return render_template('repayments/index.html', user=get_current_user())


@repayments_bp.route('/record')
@login_required
def record_page():
    db = get_db()
    loans = db.execute(
        _borrower_name_sql() +
        " WHERE loans.status = 'active' AND loans.outstanding_balance > 0"
        " ORDER BY borrower_name"
    ).fetchall()
    members = db.execute("SELECT * FROM members WHERE status = 'active' ORDER BY first_name").fetchall()
    clients = db.execute("SELECT * FROM clients WHERE status = 'active' ORDER BY first_name").fetchall()
    return render_template('repayments/record.html', user=get_current_user(),
                            loans=loans, members=members, clients=clients)


@repayments_bp.route('/api', methods=['GET'])
@login_required
def list_repayments():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    loan_id = request.args.get('loan_id')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    include_voided = request.args.get('include_voided', 'false').lower() == 'true'

    where, params = [], []
    if not include_voided:
        where.append("repayments.voided_at IS NULL")
    if loan_id:
        where.append("repayments.loan_id = %s")
        params.append(int(loan_id))
    if date_from:
        where.append("repayments.payment_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("repayments.payment_date <= %s")
        params.append(date_to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base = f"""SELECT repayments.*, loans.loan_number FROM repayments
               LEFT JOIN loans ON loans.id = repayments.loan_id{where_sql}
               ORDER BY repayments.created_at DESC"""
    count_sql = f"SELECT COUNT(*) FROM repayments{where_sql}"

    rows, total, pages = paginate(base, count_sql, tuple(params), page, per_page)

    return jsonify({
        'repayments': [repayment_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@repayments_bp.route('/api', methods=['POST'])
@login_required
def record_repayment():
    data = request.get_json()
    user = get_current_user()

    try:
        repayment, new_outstanding = _record_repayment(
            loan_id=data.get('loan_id'),
            amount=data.get('amount', 0),
            payment_method=data.get('payment_method', 'cash'),
            reference_number=data.get('reference_number'),
            payment_date=data.get('payment_date'),
            notes=data.get('notes'),
            user_id=user['id'],
        )
    except _RepaymentError as e:
        return jsonify({'error': str(e)}), e.status_code

    return jsonify({
        'message': 'Repayment recorded',
        'repayment': repayment_public(repayment),
        'loan_balance': new_outstanding
    }), 201


class _RepaymentError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _record_repayment(loan_id, amount, payment_method='cash', reference_number=None,
                       payment_date=None, notes=None, user_id=None):
    """Core repayment-recording logic, shared by the manual "Record Repayment"
    API endpoint and the M-Pesa STK Push callback (app/routes/mpesa.py) so a
    payment collected via M-Pesa is applied to the loan schedule exactly the
    same way as one entered by staff. Raises _RepaymentError on validation
    failure; returns (repayment_row, new_outstanding_balance) on success."""
    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        raise _RepaymentError('Loan not found', 404)
    if loan['status'] not in ('active', 'disbursed'):
        raise _RepaymentError('Loan is not active', 400)

    amount = float(amount or 0)
    if amount <= 0:
        raise _RepaymentError('Amount must be positive', 400)

    today = date.today()

    schedules = get_db().execute(
        """SELECT * FROM loan_schedules WHERE loan_id = %s AND status IN ('pending', 'partial')
           ORDER BY due_date""", (loan['id'],)
    ).fetchall()
    schedule_dicts = [dict(s) for s in schedules]

    updates = allocate_payment(amount, schedule_dicts)
    for u in updates:
        if u['fully_paid']:
            execute(
                """UPDATE loan_schedules SET principal_paid = principal_paid + %s, interest_paid = interest_paid + %s,
                       total_paid = total_due, status = 'paid', paid_date = %s WHERE id = %s""",
                (u['principal_paid_delta'], u['interest_paid_delta'], today.isoformat(), u['schedule_id'])
            )
        else:
            execute(
                """UPDATE loan_schedules SET principal_paid = principal_paid + %s, interest_paid = interest_paid + %s,
                       total_paid = total_paid + %s, status = 'partial' WHERE id = %s""",
                (u['principal_paid_delta'], u['interest_paid_delta'], u['total_paid_delta'], u['schedule_id'])
            )

    # Split the payment itself into principal/interest portions proportional
    # to the loan's overall interest ratio (penalties tracked separately).
    interest_ratio = (loan['total_interest'] / loan['total_repayable']) if loan['total_repayable'] else 0
    interest_portion = round(amount * interest_ratio, 2)
    principal_portion = round(amount - interest_portion, 2)
    penalty_portion = 0

    new_total_paid = (loan['total_paid'] or 0) + amount
    new_outstanding = max(0, loan['outstanding_balance'] - amount)
    new_status = loan['status']
    actual_end_date = loan['actual_end_date']
    if new_outstanding <= 0:
        new_status = 'completed'
        actual_end_date = today.isoformat()

    execute(
        """UPDATE loans SET total_paid = %s, outstanding_balance = %s, status = %s, actual_end_date = %s, updated_at = %s
           WHERE id = %s""",
        (new_total_paid, new_outstanding, new_status, actual_end_date, utcnow(), loan['id'])
    )

    now = utcnow()
    cur = execute(
        """INSERT INTO repayments (receipt_number, loan_id, amount, principal_portion, interest_portion,
               penalty_portion, payment_method, reference_number, payment_date, notes, collected_by, created_at,
               allocation_snapshot)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (generate_receipt_number(), loan['id'], amount, principal_portion, interest_portion, penalty_portion,
         payment_method, reference_number, payment_date or today.isoformat(), notes, user_id, now,
         json.dumps(updates))
    )
    log_audit('REPAYMENT_RECORDED', 'repayment', cur.lastrowid)
    adjust_main_account_balance(amount)
    adjust_account_balance('1100', -principal_portion)
    adjust_account_balance('4000', interest_portion)

    repayment = get_db().execute(
        """SELECT repayments.*, loans.loan_number FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id WHERE repayments.id = %s""",
        (cur.lastrowid,)
    ).fetchone()

    db = get_db()
    borrower_name, borrower_email, borrower_phone = None, None, None
    if loan['member_id']:
        p = db.execute("SELECT first_name, last_name, email, phone FROM members WHERE id = %s",
                        (loan['member_id'],)).fetchone()
    elif loan['client_id']:
        p = db.execute("SELECT first_name, last_name, email, phone FROM clients WHERE id = %s",
                        (loan['client_id'],)).fetchone()
    else:
        p = None
    if p:
        borrower_name, borrower_email, borrower_phone = f"{p['first_name']} {p['last_name']}", p['email'], p['phone']

    notify(
        user_id,
        'Repayment Recorded',
        f"Repayment of {format_currency(amount)} recorded for loan {loan['loan_number']} "
        f"(receipt {repayment['receipt_number']}).",
        notification_type='success', related_type='repayment', related_id=cur.lastrowid,
        email=borrower_email,
        email_subject=f"Payment received - Receipt {repayment['receipt_number']}",
        email_body_html=(
            f"<p>Dear {borrower_name or 'Customer'},</p>"
            f"<p>We've received your payment of <strong>{format_currency(amount)}</strong> "
            f"for loan <strong>{loan['loan_number']}</strong>.</p>"
            f"<p>Receipt number: <strong>{repayment['receipt_number']}</strong><br>"
            f"Remaining balance: <strong>{format_currency(new_outstanding)}</strong></p>"
            f"<p>Thank you.</p>"
        ),
        phone=borrower_phone,
        sms_message=(
            f"Dear {borrower_name or 'Customer'}, we've received your payment of "
            f"{format_currency(amount)} for loan {loan['loan_number']}. "
            f"Receipt: {repayment['receipt_number']}. "
            f"Outstanding balance: {format_currency(new_outstanding)}. "
            f"Thank you - Jodala Microfinance."
        ),
        ai_event_type='repayment_recorded',
        ai_facts={
            'borrower_name': borrower_name, 'loan_number': loan['loan_number'],
            'amount_paid': format_currency(amount),
            'receipt_number': repayment['receipt_number'],
            'remaining_balance': format_currency(new_outstanding),
        }
    )

    return repayment, new_outstanding


@repayments_bp.route('/api/<int:repayment_id>/void', methods=['POST'])
@login_required
@role_required('admin')
def void_repayment(repayment_id):
    """Reverses a mis-entered repayment (wrong loan, wrong amount, duplicate
    M-Pesa callback, etc). This is a VOID, not a delete: the repayment row
    stays in place -- receipt number and all -- marked with voided_at/
    voided_by/void_reason, so there's still a record it happened and was
    later corrected. Admin-only, and a reason is required, because this
    touches the loan schedule, the loan balance, and the accounting ledger
    all at once."""
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': 'A reason is required to void a repayment'}), 400

    try:
        repayment, new_outstanding = _void_repayment(
            repayment_id=repayment_id, reason=reason, user_id=get_current_user()['id']
        )
    except _RepaymentError as e:
        return jsonify({'error': str(e)}), e.status_code

    return jsonify({
        'message': 'Repayment voided',
        'repayment': repayment_public(repayment),
        'loan_balance': new_outstanding
    })


def _void_repayment(repayment_id, reason, user_id=None):
    """Reverses everything _record_repayment did for this repayment:
    schedule allocations, the loan's total_paid/outstanding_balance/status,
    and the ledger/main-account postings. The repayment row itself is kept
    and marked voided rather than deleted, for audit purposes -- see the
    /void route docstring. Raises _RepaymentError on failure; returns
    (repayment_row, new_outstanding_balance) on success."""
    db = get_db()
    repayment = db.execute("SELECT * FROM repayments WHERE id = %s", (repayment_id,)).fetchone()
    if not repayment:
        raise _RepaymentError('Repayment not found', 404)
    if repayment['voided_at']:
        raise _RepaymentError('Repayment has already been voided', 400)

    loan = db.execute("SELECT * FROM loans WHERE id = %s", (repayment['loan_id'],)).fetchone()
    if not loan:
        raise _RepaymentError('Loan for this repayment no longer exists', 404)

    amount = repayment['amount']

    # Reverse the exact per-schedule-row deltas this repayment applied, in
    # the exact order they were applied, so partial-payment rows are
    # unwound to precisely what they were before -- not re-derived from
    # today's schedule state, which could differ if other payments (or a
    # restructure that rebuilt the schedule) have happened since.
    allocation = json.loads(repayment['allocation_snapshot']) if repayment['allocation_snapshot'] else []
    for u in allocation:
        schedule = db.execute(
            "SELECT * FROM loan_schedules WHERE id = %s", (u['schedule_id'],)
        ).fetchone()
        if not schedule:
            # Schedule was rebuilt (e.g. a restructure/extend) since this
            # payment was recorded -- nothing to unwind on a row that no
            # longer exists; the loan-level totals below are still
            # reversed correctly regardless.
            continue
        new_principal_paid = max(0, schedule['principal_paid'] - u['principal_paid_delta'])
        new_interest_paid = max(0, schedule['interest_paid'] - u['interest_paid_delta'])
        new_total_paid = max(0, schedule['total_paid'] - u['total_paid_delta'])
        new_status = 'paid' if new_total_paid >= schedule['total_due'] - 0.0001 else (
            'partial' if new_total_paid > 0 else 'pending'
        )
        execute(
            """UPDATE loan_schedules SET principal_paid = %s, interest_paid = %s, total_paid = %s,
                   status = %s, paid_date = %s WHERE id = %s""",
            (new_principal_paid, new_interest_paid, new_total_paid,
             new_status, None if new_status != 'paid' else schedule['paid_date'], schedule['id'])
        )

    today = date.today()
    new_total_paid_loan = max(0, (loan['total_paid'] or 0) - amount)
    new_outstanding = round(loan['outstanding_balance'] + amount, 2)
    new_status = loan['status']
    actual_end_date = loan['actual_end_date']
    # A loan that had been auto-completed by this payment goes back to
    # active now that the payment is voided; an already-written-off or
    # otherwise-closed loan is left alone since voiding a payment doesn't
    # undo that separate decision.
    if loan['status'] == 'completed' and new_outstanding > 0:
        new_status = 'active'
        actual_end_date = None

    execute(
        """UPDATE loans SET total_paid = %s, outstanding_balance = %s, status = %s, actual_end_date = %s,
               updated_at = %s WHERE id = %s""",
        (new_total_paid_loan, new_outstanding, new_status, actual_end_date, utcnow(), loan['id'])
    )

    # Reverse the ledger/main-account postings _record_repayment made,
    # using the same portions actually recorded on this repayment (not
    # recomputed from the loan's current interest ratio, which may have
    # drifted since).
    adjust_main_account_balance(-amount)
    adjust_account_balance('1100', repayment['principal_portion'])
    adjust_account_balance('4000', -repayment['interest_portion'])

    now = utcnow()
    execute(
        "UPDATE repayments SET voided_at = %s, voided_by = %s, void_reason = %s WHERE id = %s",
        (now, user_id, reason, repayment['id'])
    )
    log_audit('REPAYMENT_VOIDED', 'repayment', repayment['id'],
              old_values={'amount': amount, 'loan_id': loan['id']},
              new_values={'void_reason': reason})

    repayment = db.execute(
        """SELECT repayments.*, loans.loan_number FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id WHERE repayments.id = %s""",
        (repayment['id'],)
    ).fetchone()

    borrower_name, borrower_email, borrower_phone = None, None, None
    if loan['member_id']:
        p = db.execute("SELECT first_name, last_name, email, phone FROM members WHERE id = %s",
                        (loan['member_id'],)).fetchone()
    elif loan['client_id']:
        p = db.execute("SELECT first_name, last_name, email, phone FROM clients WHERE id = %s",
                        (loan['client_id'],)).fetchone()
    else:
        p = None
    if p:
        borrower_name, borrower_email, borrower_phone = f"{p['first_name']} {p['last_name']}", p['email'], p['phone']

    notify(
        user_id,
        'Repayment Voided',
        f"Repayment of {format_currency(amount)} on loan {loan['loan_number']} "
        f"(receipt {repayment['receipt_number']}) was voided. Reason: {reason}",
        notification_type='warning', related_type='repayment', related_id=repayment['id'],
        email=borrower_email,
        email_subject=f"Payment correction - Receipt {repayment['receipt_number']}",
        email_body_html=(
            f"<p>Dear {borrower_name or 'Customer'},</p>"
            f"<p>Your payment of <strong>{format_currency(amount)}</strong> "
            f"for loan <strong>{loan['loan_number']}</strong> (receipt "
            f"<strong>{repayment['receipt_number']}</strong>) has been reversed.</p>"
            f"<p>Updated outstanding balance: <strong>{format_currency(new_outstanding)}</strong></p>"
            f"<p>If you believe this is an error, please contact us.</p>"
        ),
        phone=borrower_phone,
        sms_message=(
            f"Dear {borrower_name or 'Customer'}, your payment of {format_currency(amount)} "
            f"for loan {loan['loan_number']} (receipt {repayment['receipt_number']}) has been "
            f"reversed. Updated balance: {format_currency(new_outstanding)}. "
            f"Contact us if you believe this is an error - Jodala Microfinance."
        ),
        ai_event_type='repayment_voided',
        ai_facts={
            'borrower_name': borrower_name, 'loan_number': loan['loan_number'],
            'amount_reversed': format_currency(amount),
            'receipt_number': repayment['receipt_number'],
            'reason': reason,
            'new_outstanding_balance': format_currency(new_outstanding),
        }
    )

    return repayment, new_outstanding


@repayments_bp.route('/api/<int:repayment_id>', methods=['GET'])
@login_required
def get_repayment(repayment_id):
    repayment = get_db().execute(
        """SELECT repayments.*, loans.loan_number, loans.member_id, loans.client_id
           FROM repayments LEFT JOIN loans ON loans.id = repayments.loan_id
           WHERE repayments.id = %s""", (repayment_id,)
    ).fetchone()
    if not repayment:
        return jsonify({'error': 'Repayment not found'}), 404

    data = repayment_public(repayment)
    if repayment['member_id']:
        member = get_db().execute("SELECT * FROM members WHERE id = %s", (repayment['member_id'],)).fetchone()
        data['borrower_name'] = member_full_name(member) if member else 'N/A'
    elif repayment['client_id']:
        client = get_db().execute("SELECT * FROM clients WHERE id = %s", (repayment['client_id'],)).fetchone()
        data['borrower_name'] = client_full_name(client) if client else 'N/A'
    else:
        data['borrower_name'] = 'N/A'

    return jsonify(data)


@repayments_bp.route('/<int:repayment_id>/receipt')
@login_required
def receipt_page(repayment_id):
    row = get_db().execute(
        """SELECT repayments.*, loans.loan_number AS _loan_number,
                  loans.outstanding_balance AS _loan_outstanding_balance,
                  COALESCE(
                      TRIM(members.first_name || ' ' || COALESCE(members.middle_name, '') || ' ' || members.last_name),
                      TRIM(clients.first_name || ' ' || clients.last_name)
                  ) AS _borrower_name
           FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id
           LEFT JOIN members ON members.id = loans.member_id
           LEFT JOIN clients ON clients.id = loans.client_id
           WHERE repayments.id = %s""",
        (repayment_id,)
    ).fetchone()
    if not row:
        abort(404)

    repayment = dict(row)
    loan_number = repayment.pop('_loan_number')
    outstanding_balance = repayment.pop('_loan_outstanding_balance')
    repayment['borrower_name'] = repayment.pop('_borrower_name') or 'N/A'
    repayment['loan'] = (
        {'loan_number': loan_number, 'outstanding_balance': outstanding_balance}
        if loan_number is not None else None
    )

    return render_template('repayments/receipt.html', user=get_current_user(), repayment=repayment)
