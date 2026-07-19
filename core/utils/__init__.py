from datetime import datetime, date
from flask import request, g
import json

from core.database import get_db, query_one, execute, utcnow


def _next_number(table, prefix, width, year_in_number=False, dashed=False):
    row = query_one(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1")
    num = (row['id'] + 1) if row else 1
    if dashed:
        return f"{prefix}-{datetime.utcnow().year}-{num:0{width}d}"
    if year_in_number:
        return f"{prefix}{datetime.utcnow().year}{num:0{width}d}"
    return f"{prefix}{num:0{width}d}"


def generate_member_number():
    return _next_number('members', 'MEM', 2, dashed=True)


def generate_client_number():
    return _next_number('clients', 'CLT', 2, dashed=True)


def generate_loan_number():
    row = query_one("SELECT value FROM company_settings WHERE key = 'loan_prefix'")
    prefix = row['value'] if row else 'LN'
    last = query_one("SELECT id FROM loans ORDER BY id DESC LIMIT 1")
    num = (last['id'] + 1) if last else 1
    return f"{prefix}{datetime.utcnow().year}{num:02d}"


def generate_receipt_number():
    return _next_number('repayments', 'RCP', 2)


def generate_savings_account_number():
    return _next_number('savings_accounts', 'SAV', 6)


def generate_savings_transaction_number():
    return _next_number('savings_transactions', 'TXN', 8)


def generate_journal_number():
    return _next_number('journal_entries', 'JNL', 7)


def generate_income_reference():
    return _next_number('income', 'INC', 7)


def generate_expense_reference():
    return _next_number('expenses', 'EXP', 7)


def adjust_account_balance(code, delta):
    """Adjust a chart-of-accounts ledger balance (by account `code`, e.g.
    '1000' for Cash and Bank) by `delta`. This is what powers the Chart of
    Accounts / Trial Balance screens -- without calling this, those pages
    stay at zero forever no matter how much real activity happens.
    Silently does nothing if the account code doesn't exist, so seeding
    differences across environments can't crash a request.

    `delta` is pre-signed by the caller to already mean "increase in this
    account's own normal-balance direction" -- e.g. +100 to an asset means
    a debit (cash coming in), +100 to a liability/equity/income account
    means a credit. Callers posting a plain debit/credit pair against
    accounts of unknown type (e.g. a user-entered manual journal line)
    should use post_journal_line() instead, which works out the correct
    sign for each side itself."""
    account = get_db().execute("SELECT id, balance FROM accounts WHERE code = %s", (code,)).fetchone()
    if not account:
        return None
    new_balance = round((account['balance'] or 0) + delta, 2)
    execute("UPDATE accounts SET balance = %s WHERE id = %s", (new_balance, account['id']))
    return new_balance


_DEBIT_NORMAL_TYPES = ('asset', 'expense')


def post_journal_line(debit_account_id, credit_account_id, amount):
    """Posts one manual journal line -- `amount` debited to `debit_account_id`
    and the same amount credited to `credit_account_id` -- onto the ledger,
    working out the correct sign for each account from its own
    account_type rather than assuming the caller already knows it (unlike
    adjust_account_balance, which takes a pre-signed delta). Debiting a
    debit-normal account (asset/expense) increases its balance; debiting a
    credit-normal account (liability/equity/income) decreases it, and vice
    versa for the credit side -- this is what keeps a manual entry that
    debits Cash and credits Equity, say, correctly increasing both, while
    one that debits an expense and credits Cash correctly decreases Cash.
    Returns False (posts nothing) if either account id doesn't exist, so a
    bad id from a stale dropdown can't partially post a one-sided entry."""
    db = get_db()
    debit_account = db.execute("SELECT id, account_type, balance FROM accounts WHERE id = %s", (debit_account_id,)).fetchone()
    credit_account = db.execute("SELECT id, account_type, balance FROM accounts WHERE id = %s", (credit_account_id,)).fetchone()
    if not debit_account or not credit_account:
        return False

    debit_sign = 1 if debit_account['account_type'] in _DEBIT_NORMAL_TYPES else -1
    credit_sign = -1 if credit_account['account_type'] in _DEBIT_NORMAL_TYPES else 1

    execute("UPDATE accounts SET balance = %s WHERE id = %s",
            (round((debit_account['balance'] or 0) + debit_sign * amount, 2), debit_account['id']))
    execute("UPDATE accounts SET balance = %s WHERE id = %s",
            (round((credit_account['balance'] or 0) + credit_sign * amount, 2), credit_account['id']))
    return True


def adjust_main_account_balance(delta):
    """Adjust the SACCO/chama main account balance (company_settings key
    'main_account_opening_balance') by `delta`. Positive delta = cash in
    (repayments, savings deposits, other income). Negative delta = cash out
    (loan disbursements, savings withdrawals, expenses). Returns the new
    balance.

    Every call here represents real cash moving, so it also posts the same
    delta to the 'Cash and Bank' (1000) ledger account -- keeping the
    Chart of Accounts / Trial Balance in sync with the headline balance
    shown in Settings, instead of the two silently drifting apart."""
    now = utcnow()
    row = get_db().execute(
        "SELECT value FROM company_settings WHERE key = 'main_account_opening_balance'"
    ).fetchone()
    current = float(row['value']) if row and row['value'] else 0
    new_balance = round(current + delta, 2)
    if row:
        execute(
            "UPDATE company_settings SET value = %s, updated_at = %s WHERE key = 'main_account_opening_balance'",
            (str(new_balance), now)
        )
    else:
        execute(
            "INSERT INTO company_settings (key, value, updated_at) VALUES ('main_account_opening_balance', %s, %s)",
            (str(new_balance), now)
        )
    adjust_account_balance('1000', delta)
    return new_balance


def log_audit(action, resource_type=None, resource_id=None, old_values=None, new_values=None):
    user_id = getattr(g, 'current_user_id', None)

    try:
        ip = request.remote_addr
        ua = request.user_agent.string[:255] if request.user_agent else ''
    except Exception:
        ip = '127.0.0.1'
        ua = ''

    execute(
        """INSERT INTO audit_logs (user_id, action, resource_type, resource_id,
                                    old_values, new_values, ip_address, user_agent, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (user_id, action, resource_type, resource_id,
         json.dumps(old_values) if old_values else None,
         json.dumps(new_values) if new_values else None,
         ip, ua, utcnow())
    )


def get_overdue_loan_ids():
    today = date.today().isoformat()
    rows = get_db().execute(
        """SELECT DISTINCT loan_id FROM loan_schedules
           WHERE due_date < %s AND status IN ('pending', 'partial')""",
        (today,)
    ).fetchall()
    return [r['loan_id'] for r in rows]


def format_currency(amount):
    return f"Ksh {amount:,.2f}"


def notify(user_id, title, message, notification_type='info', related_type=None,
           related_id=None, email=None, email_subject=None, email_body_html=None,
           notify_user_email=True):
    """
    Central notification helper: always writes an in-app notification row.

    Two separate email recipients can be reached from one call:
      - The staff user (`user_id`), via their `users.email` on file --
        automatic whenever notify_user_email=True (the default) and that
        user has an email address. Sent as a plain notification email
        built from `title`/`message`.
      - An optional customer-facing recipient (`email`), for a member,
        client, or other non-staff address -- used for the nicer branded
        HTML in `email_body_html` (e.g. "Dear Jane, your loan was
        approved..."). Pass `email`/`email_subject`/`email_body_html`
        together for this.
    If both happen to be the same address (e.g. creating a new staff
    account emails that same person), only the customer-facing version is
    sent once -- not both.

    user_id may be None for system-wide events that aren't tied to a
    dashboard user (e.g. a member/client's own confirmation email) -- in
    that case only the `email` recipient is used, no notification row is
    written and no staff email is sent.
    """
    staff_email = None
    if user_id is not None:
        execute(
            """INSERT INTO notifications (user_id, title, message, notification_type,
                   related_type, related_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, title, message, notification_type, related_type, related_id, utcnow())
        )

        if notify_user_email:
            user_row = get_db().execute(
                "SELECT email, full_name FROM users WHERE id = %s", (user_id,)
            ).fetchone()
            if user_row and user_row['email']:
                staff_email = user_row['email']

    if staff_email and staff_email != email:
        from core.mailer import send_email_async
        greeting = (user_row['full_name'] if user_row and user_row['full_name'] else 'there')
        send_email_async(
            staff_email,
            email_subject or title,
            message,
            f"<p>Dear {greeting},</p><p>{message}</p>"
        )

    if email:
        from core.mailer import send_email_async
        send_email_async(
            email,
            email_subject or title,
            message,
            email_body_html
        )


def notify_admins(title, message, notification_type='info', related_type=None, related_id=None):
    """Write an in-app notification for every admin user (no email -- admins
    see these in their dashboard bell; use `notify()` directly if a specific
    admin also needs an email)."""
    admins = get_db().execute("SELECT id FROM users WHERE role = 'admin' AND is_active = 1").fetchall()
    for a in admins:
        notify(a['id'], title, message, notification_type, related_type, related_id)



def paginate(base_sql, count_sql, params, page, per_page):
    """Runs count_sql/base_sql (base_sql must already ORDER BY) with LIMIT/OFFSET,
    returns (rows, total, pages)."""
    total_row = get_db().execute(count_sql, params).fetchone()
    total = total_row[0] if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    rows = get_db().execute(base_sql + " LIMIT %s OFFSET %s", params + (per_page, offset)).fetchall()
    return rows, total, pages
