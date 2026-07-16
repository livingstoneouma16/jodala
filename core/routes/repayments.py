from flask import Blueprint, request, jsonify, render_template, abort
from datetime import date

from core.database import get_db, execute, utcnow
from core.auth import login_required, get_current_user
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

    where, params = [], []
    if loan_id:
        where.append("repayments.loan_id = ?")
        params.append(int(loan_id))
    if date_from:
        where.append("repayments.payment_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("repayments.payment_date <= ?")
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
    loan = get_db().execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        raise _RepaymentError('Loan not found', 404)
    if loan['status'] not in ('active', 'disbursed'):
        raise _RepaymentError('Loan is not active', 400)

    amount = float(amount or 0)
    if amount <= 0:
        raise _RepaymentError('Amount must be positive', 400)

    today = date.today()

    schedules = get_db().execute(
        """SELECT * FROM loan_schedules WHERE loan_id = ? AND status IN ('pending', 'partial')
           ORDER BY due_date""", (loan['id'],)
    ).fetchall()
    schedule_dicts = [dict(s) for s in schedules]

    updates = allocate_payment(amount, schedule_dicts)
    for u in updates:
        if u['fully_paid']:
            execute(
                """UPDATE loan_schedules SET principal_paid = principal_paid + ?, interest_paid = interest_paid + ?,
                       total_paid = total_due, status = 'paid', paid_date = ? WHERE id = ?""",
                (u['principal_paid_delta'], u['interest_paid_delta'], today.isoformat(), u['schedule_id'])
            )
        else:
            execute(
                """UPDATE loan_schedules SET principal_paid = principal_paid + ?, interest_paid = interest_paid + ?,
                       total_paid = total_paid + ?, status = 'partial' WHERE id = ?""",
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
        """UPDATE loans SET total_paid = ?, outstanding_balance = ?, status = ?, actual_end_date = ?, updated_at = ?
           WHERE id = ?""",
        (new_total_paid, new_outstanding, new_status, actual_end_date, utcnow(), loan['id'])
    )

    now = utcnow()
    cur = execute(
        """INSERT INTO repayments (receipt_number, loan_id, amount, principal_portion, interest_portion,
               penalty_portion, payment_method, reference_number, payment_date, notes, collected_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_receipt_number(), loan['id'], amount, principal_portion, interest_portion, penalty_portion,
         payment_method, reference_number, payment_date or today.isoformat(), notes, user_id, now)
    )
    log_audit('REPAYMENT_RECORDED', 'repayment', cur.lastrowid)
    adjust_main_account_balance(amount)
    adjust_account_balance('1100', -principal_portion)
    adjust_account_balance('4000', interest_portion)

    repayment = get_db().execute(
        """SELECT repayments.*, loans.loan_number FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id WHERE repayments.id = ?""",
        (cur.lastrowid,)
    ).fetchone()

    db = get_db()
    borrower_name, borrower_email = None, None
    if loan['member_id']:
        p = db.execute("SELECT first_name, last_name, email FROM members WHERE id = ?",
                        (loan['member_id'],)).fetchone()
    elif loan['client_id']:
        p = db.execute("SELECT first_name, last_name, email FROM clients WHERE id = ?",
                        (loan['client_id'],)).fetchone()
    else:
        p = None
    if p:
        borrower_name, borrower_email = f"{p['first_name']} {p['last_name']}", p['email']

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
        )
    )

    return repayment, new_outstanding


@repayments_bp.route('/api/<int:repayment_id>', methods=['GET'])
@login_required
def get_repayment(repayment_id):
    repayment = get_db().execute(
        """SELECT repayments.*, loans.loan_number, loans.member_id, loans.client_id
           FROM repayments LEFT JOIN loans ON loans.id = repayments.loan_id
           WHERE repayments.id = ?""", (repayment_id,)
    ).fetchone()
    if not repayment:
        return jsonify({'error': 'Repayment not found'}), 404

    data = repayment_public(repayment)
    if repayment['member_id']:
        member = get_db().execute("SELECT * FROM members WHERE id = ?", (repayment['member_id'],)).fetchone()
        data['borrower_name'] = member_full_name(member) if member else 'N/A'
    elif repayment['client_id']:
        client = get_db().execute("SELECT * FROM clients WHERE id = ?", (repayment['client_id'],)).fetchone()
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
           WHERE repayments.id = ?""",
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
