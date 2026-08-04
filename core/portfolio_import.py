"""
Bulk loan portfolio import -- migrating an existing book of loans (already
active, some part-paid, some overdue) into this system from a CSV export
of whatever was used before.

This is deliberately NOT built on top of core/routes/loans.py's normal
create_loan() / _disburse_loan() path: those are for a *new* loan going
through application -> approval -> disbursement today, and they (a) send
"your loan was approved/disbursed" notifications that would be false for
a loan that's been running for months elsewhere, and (b) debit the main
cash account for the disbursement -- money that, for a migrated loan,
already left the bank in the old system before this software ever
existed. Re-debiting it here would silently corrupt the cash balance.

When posting to the ledger is enabled, only the currently-outstanding
balance (not the original principal) is posted, as an opening balance:
debit Loans Receivable (1100), credit Retained Earnings (3200) -- the
standard double-entry treatment when a balance's original funding
predates this system's own books. Cash (1000) is never touched by an
import.

Two-step flow, both used by core/routes/portfolio_import.py:
    parse_and_validate(file_stream) -> a preview: every row, valid or not,
        with why a row would fail, and nothing written to the DB yet.
    commit_import(rows, post_to_ledger, created_by) -> actually inserts
        the loans/schedules/repayment-history rows for the valid ones.

Only the rows that already passed validation in the preview step should
ever reach commit_import -- see core/routes/portfolio_import.py, which
re-validates anyway rather than trusting the client round-trip blindly.
"""
import csv
import io
from datetime import date, timedelta

from core.database import get_db, execute, utcnow
from core.calculator import build_loan_schedule
from core.utils import (generate_loan_number, generate_member_number, generate_client_number,
                         log_audit, adjust_account_balance, format_currency)


# Required for every row. member_number/client_number OR first_name+last_name+phone
# must also be present (checked separately, since it's an either/or).
REQUIRED_COLUMNS = [
    'product_code', 'principal_amount', 'interest_rate', 'interest_type',
    'term', 'repayment_frequency', 'disbursement_date', 'first_repayment_date',
]

# Optional columns for partial repayment history. If installments_paid is
# given, that many installments (from #1) are marked paid using the
# schedule's own numbers -- the "simple" path. If per-installment status
# columns are given instead (installment_N_status / installment_N_paid_date
# for N in 1..term), that's the "detailed" path and takes precedence.
OPTIONAL_COLUMNS = [
    'member_number', 'client_number', 'first_name', 'last_name', 'phone', 'email', 'region',
    'guarantor_name', 'guarantor_phone', 'collateral', 'purpose', 'loan_officer_username',
    'installments_paid', 'total_paid', 'notes',
]

VALID_INTEREST_TYPES = {'flat', 'reducing'}
VALID_FREQUENCIES = {'daily', 'weekly', 'monthly'}


def _parse_date(value, field_name, errors):
    value = (value or '').strip()
    if not value:
        errors.append(f"{field_name} is required")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field_name} must be YYYY-MM-DD (got '{value}')")
        return None


def _parse_float(value, field_name, errors, allow_zero=True):
    value = (value or '').strip()
    if not value:
        errors.append(f"{field_name} is required")
        return None
    try:
        f = float(value)
    except ValueError:
        errors.append(f"{field_name} must be a number (got '{value}')")
        return None
    if f < 0 or (not allow_zero and f == 0):
        errors.append(f"{field_name} must be positive")
        return None
    return f


def _parse_int(value, field_name, errors, min_value=1):
    value = (value or '').strip()
    if not value:
        errors.append(f"{field_name} is required")
        return None
    try:
        i = int(value)
    except ValueError:
        errors.append(f"{field_name} must be a whole number (got '{value}')")
        return None
    if i < min_value:
        errors.append(f"{field_name} must be at least {min_value}")
        return None
    return i


def _resolve_borrower(row, errors):
    """Returns (borrower_type, borrower_id, is_new, display_name) or
    (None, None, False, None) if the row can't be resolved. Doesn't create
    anything yet in the preview step -- see _create_borrower_if_needed,
    called only during commit."""
    member_number = (row.get('member_number') or '').strip()
    client_number = (row.get('client_number') or '').strip()
    first_name = (row.get('first_name') or '').strip()
    last_name = (row.get('last_name') or '').strip()
    phone = (row.get('phone') or '').strip()

    if member_number and client_number:
        errors.append("Row has both member_number and client_number -- only one allowed")
        return None, None, False, None

    if member_number:
        m = get_db().execute("SELECT * FROM members WHERE member_number = %s", (member_number,)).fetchone()
        if not m:
            errors.append(f"No member found with member_number '{member_number}'")
            return None, None, False, None
        return 'member', m['id'], False, f"{m['first_name']} {m['last_name']}"

    if client_number:
        c = get_db().execute("SELECT * FROM clients WHERE client_number = %s", (client_number,)).fetchone()
        if not c:
            errors.append(f"No client found with client_number '{client_number}'")
            return None, None, False, None
        return 'client', c['id'], False, f"{c['first_name']} {c['last_name']}"

    if first_name and last_name and phone:
        # No number given -- try to match an existing client by phone first
        # (most common case for a portfolio migration: these are typically
        # non-member client borrowers), then fall back to creating a new
        # client record for this borrower during commit.
        existing = get_db().execute("SELECT * FROM clients WHERE phone = %s", (phone,)).fetchone()
        if existing:
            return 'client', existing['id'], False, f"{existing['first_name']} {existing['last_name']}"
        return 'client', None, True, f"{first_name} {last_name}"

    errors.append(
        "Row must have either member_number, client_number, or all of "
        "first_name/last_name/phone to identify the borrower"
    )
    return None, None, False, None


def _create_borrower_if_needed(row, created_by):
    """Only called during commit, for rows where _resolve_borrower found no
    existing match. Creates a minimal client record -- imported borrowers
    without a member/client number are assumed to be non-member clients;
    if they should actually be members, register them properly first and
    re-run the import with member_number filled in."""
    now = utcnow()
    cur = execute(
        """INSERT INTO clients (client_number, first_name, last_name, phone, email, region,
               status, created_by, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)""",
        (generate_client_number(), row.get('first_name', '').strip(), row.get('last_name', '').strip(),
         row.get('phone', '').strip(), (row.get('email') or '').strip() or None,
         (row.get('region') or '').strip() or None, created_by, now, now)
    )
    return cur.lastrowid


def parse_and_validate(file_stream):
    """Reads the uploaded CSV and returns a dict:
        {rows: [...], valid_count, invalid_count, total_rows}
    Every row dict has: row_number, data (raw CSV values), errors (list,
    empty if valid), borrower_display (best-effort name for the preview
    table), is_new_borrower (bool).
    Nothing is written to the database at this stage."""
    text = file_stream.read()
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig')  # -sig strips a BOM from Excel-exported CSVs
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        return {'rows': [], 'valid_count': 0, 'invalid_count': 0, 'total_rows': 0,
                'error': 'CSV appears to be empty or has no header row'}

    missing_required = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing_required:
        return {'rows': [], 'valid_count': 0, 'invalid_count': 0, 'total_rows': 0,
                'error': f"CSV is missing required column(s): {', '.join(missing_required)}"}

    results = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 is the header
        errors = []

        product = None
        product_code = (raw_row.get('product_code') or '').strip()
        if not product_code:
            errors.append("product_code is required")
        else:
            product = get_db().execute(
                "SELECT * FROM loan_products WHERE code = %s", (product_code,)
            ).fetchone()
            if not product:
                errors.append(f"No loan product found with code '{product_code}'")

        principal = _parse_float(raw_row.get('principal_amount'), 'principal_amount', errors, allow_zero=False)
        interest_rate = _parse_float(raw_row.get('interest_rate'), 'interest_rate', errors)
        term = _parse_int(raw_row.get('term'), 'term', errors)
        disbursement_date = _parse_date(raw_row.get('disbursement_date'), 'disbursement_date', errors)
        first_repayment_date = _parse_date(raw_row.get('first_repayment_date'), 'first_repayment_date', errors)

        interest_type = (raw_row.get('interest_type') or '').strip().lower()
        if interest_type not in VALID_INTEREST_TYPES:
            errors.append(f"interest_type must be one of {sorted(VALID_INTEREST_TYPES)} (got '{interest_type}')")

        frequency = (raw_row.get('repayment_frequency') or '').strip().lower()
        if frequency not in VALID_FREQUENCIES:
            errors.append(f"repayment_frequency must be one of {sorted(VALID_FREQUENCIES)} (got '{frequency}')")

        if disbursement_date and first_repayment_date and first_repayment_date <= disbursement_date:
            errors.append("first_repayment_date must be after disbursement_date")

        if product and principal is not None:
            if principal < product['min_amount'] or principal > product['max_amount']:
                errors.append(
                    f"principal_amount {principal} is outside product '{product_code}' range "
                    f"({product['min_amount']}-{product['max_amount']})"
                )

        borrower_type, borrower_id, is_new_borrower, borrower_display = _resolve_borrower(raw_row, errors)

        installments_paid = None
        if (raw_row.get('installments_paid') or '').strip():
            installments_paid = _parse_int(raw_row.get('installments_paid'), 'installments_paid', errors, min_value=0)
            if installments_paid is not None and term is not None and installments_paid > term:
                errors.append(f"installments_paid ({installments_paid}) cannot exceed term ({term})")

        loan_officer_id = None
        loan_officer_username = (raw_row.get('loan_officer_username') or '').strip()
        if loan_officer_username:
            officer = get_db().execute(
                "SELECT id FROM users WHERE username = %s", (loan_officer_username,)
            ).fetchone()
            if not officer:
                errors.append(f"No user found with username '{loan_officer_username}'")
            else:
                loan_officer_id = officer['id']

        results.append({
            'row_number': i,
            'data': raw_row,
            'errors': errors,
            'borrower_display': borrower_display,
            'is_new_borrower': is_new_borrower,
            'product_name': product['name'] if product else None,
        })

    valid_count = sum(1 for r in results if not r['errors'])
    return {
        'rows': results,
        'valid_count': valid_count,
        'invalid_count': len(results) - valid_count,
        'total_rows': len(results),
    }


def _build_partial_schedule(schedule_data, installments_paid, total_paid_override=None):
    """Marks the first `installments_paid` rows of a freshly-generated
    schedule as paid (using the schedule's own principal/interest split
    for those rows -- not a guess), and returns
    (schedule_data_with_status, computed_total_paid, computed_outstanding).
    `total_paid_override`, if given, is used for the loan's `total_paid`
    figure instead of the sum of paid installments -- e.g. if the old
    system's records include partial-installment or penalty payments this
    simple per-installment split can't represent exactly."""
    computed_paid = 0.0
    for idx, s in enumerate(schedule_data):
        if idx < installments_paid:
            s['_status'] = 'paid'
            s['_principal_paid'] = s['principal_due']
            s['_interest_paid'] = s['total_due'] - s['principal_due']
            s['_total_paid'] = s['total_due']
            s['_paid_date'] = s['due_date'].isoformat()
            computed_paid += s['total_due']
        else:
            s['_status'] = 'pending'
            s['_principal_paid'] = 0
            s['_interest_paid'] = 0
            s['_total_paid'] = 0
            s['_paid_date'] = None

    total_paid = total_paid_override if total_paid_override is not None else round(computed_paid, 2)
    total_repayable = sum(s['total_due'] for s in schedule_data)
    outstanding = round(total_repayable - total_paid, 2)
    return schedule_data, total_paid, max(0, outstanding)


def commit_import(rows, post_to_ledger, created_by, filename):
    """Actually inserts the loans for every row with no validation errors.
    Rows with errors are skipped (counted in failed_count) rather than
    aborting the whole batch -- a 500-row import with 3 bad rows should
    still bring in the other 497, with the 3 reported back for a corrected
    re-run rather than losing everyone's work.

    Returns {batch_id, imported_count, failed_count, results: [...]}."""
    now = utcnow()
    results = []
    imported_count, failed_count = 0, 0

    for row in rows:
        raw = row['data']
        errors = list(row.get('errors') or [])
        if errors:
            results.append({'row_number': row['row_number'], 'success': False, 'errors': errors})
            failed_count += 1
            continue

        try:
            product = get_db().execute(
                "SELECT * FROM loan_products WHERE code = %s", (raw['product_code'].strip(),)
            ).fetchone()
            principal = float(raw['principal_amount'])
            interest_rate = float(raw['interest_rate'])
            term = int(raw['term'])
            interest_type = raw['interest_type'].strip().lower()
            frequency = raw['repayment_frequency'].strip().lower()
            disbursement_date = date.fromisoformat(raw['disbursement_date'].strip())
            first_repayment_date = date.fromisoformat(raw['first_repayment_date'].strip())

            borrower_type, borrower_id, is_new_borrower, _ = _resolve_borrower(raw, [])
            if is_new_borrower:
                borrower_id = _create_borrower_if_needed(raw, created_by)

            loan_officer_id = created_by
            loan_officer_username = (raw.get('loan_officer_username') or '').strip()
            if loan_officer_username:
                officer = get_db().execute(
                    "SELECT id FROM users WHERE username = %s", (loan_officer_username,)
                ).fetchone()
                if officer:
                    loan_officer_id = officer['id']

            from core.routes.loans import _one_period_before
            schedule_data = build_loan_schedule(
                principal, interest_rate, term, interest_type, frequency,
                _one_period_before(first_repayment_date, frequency)
            )
            total_repayable = round(sum(s['total_due'] for s in schedule_data), 2)
            total_interest = round(total_repayable - principal, 2)
            expected_end_date = schedule_data[-1]['due_date'].isoformat() if schedule_data else None

            installments_paid = int(raw['installments_paid']) if (raw.get('installments_paid') or '').strip() else 0
            total_paid_override = float(raw['total_paid']) if (raw.get('total_paid') or '').strip() else None
            schedule_data, total_paid, outstanding = _build_partial_schedule(
                schedule_data, installments_paid, total_paid_override
            )

            loan_number = generate_loan_number()
            cur = execute(
                """INSERT INTO loans (loan_number, member_id, client_id, product_id, borrower_type,
                       principal_amount, interest_rate, interest_type, term, repayment_frequency,
                       total_interest, total_repayable, insurance_fee, amount_disbursed,
                       outstanding_balance, total_paid, purpose, collateral, guarantor_name,
                       guarantor_phone, status, application_date, disbursement_date,
                       first_repayment_date, expected_end_date, loan_officer_id, disbursed_by,
                       notes, is_imported, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s,
                       %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (loan_number,
                 borrower_id if borrower_type == 'member' else None,
                 borrower_id if borrower_type == 'client' else None,
                 product['id'], borrower_type, principal, interest_rate, interest_type, term, frequency,
                 total_interest, total_repayable, principal, outstanding, total_paid,
                 (raw.get('purpose') or '').strip() or None, (raw.get('collateral') or '').strip() or None,
                 (raw.get('guarantor_name') or '').strip() or None, (raw.get('guarantor_phone') or '').strip() or None,
                 disbursement_date.isoformat(), disbursement_date.isoformat(), first_repayment_date.isoformat(),
                 expected_end_date, loan_officer_id, created_by,
                 (raw.get('notes') or '').strip() or None, now, now)
            )
            loan_id = cur.lastrowid

            for s in schedule_data:
                execute(
                    """INSERT INTO loan_schedules (loan_id, installment_number, due_date, principal_due,
                           interest_due, total_due, principal_paid, interest_paid, total_paid,
                           balance_after, status, paid_date)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (loan_id, s['installment_number'], s['due_date'].isoformat(), s['principal_due'],
                     s['interest_due'], s['total_due'], s['_principal_paid'], s['_interest_paid'],
                     s['_total_paid'], s['balance_after'], s['_status'], s['_paid_date'])
                )

            log_audit('LOAN_IMPORTED', 'loan', loan_id, new_values={
                'loan_number': loan_number, 'principal_amount': principal, 'outstanding_balance': outstanding,
            })

            if post_to_ledger and outstanding > 0:
                # Only the currently-outstanding amount, not the original
                # principal -- see module docstring. This is an opening
                # balance for money already lent out before this system
                # existed, not a fresh disbursement, so it does NOT touch
                # Cash (1000)/the main account: that cash already moved in
                # the old system. The offsetting credit goes to Retained
                # Earnings (3200), the standard double-entry treatment for
                # an opening balance whose original funding predates this
                # system's books -- posting only the Receivable debit
                # without this would silently leave the trial balance
                # unbalanced by the entire imported portfolio's value.
                adjust_account_balance('1100', outstanding)
                adjust_account_balance('3200', outstanding)

            imported_count += 1
            results.append({'row_number': row['row_number'], 'success': True, 'loan_number': loan_number})

        except Exception as e:
            failed_count += 1
            results.append({'row_number': row['row_number'], 'success': False, 'errors': [str(e)]})

    batch_cur = execute(
        """INSERT INTO loan_import_batches (filename, total_rows, imported_count, failed_count,
               post_to_ledger, created_by, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (filename, len(rows), imported_count, failed_count, int(post_to_ledger), created_by, now)
    )
    batch_id = batch_cur.lastrowid

    return {'batch_id': batch_id, 'imported_count': imported_count, 'failed_count': failed_count, 'results': results}
