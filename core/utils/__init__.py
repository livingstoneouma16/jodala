from datetime import datetime, date, timezone
from flask import request, g
import json
import os

import requests

from core.database import get_db, query_one, execute, utcnow


def _next_number(table, prefix, width, year_in_number=False, dashed=False):
    row = query_one(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1")
    num = (row['id'] + 1) if row else 1
    if dashed:
        return f"{prefix}-{datetime.now(timezone.utc).year}-{num:0{width}d}"
    if year_in_number:
        return f"{prefix}{datetime.now(timezone.utc).year}{num:0{width}d}"
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
    return f"{prefix}{datetime.now(timezone.utc).year}{num:02d}"


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


def _ai_notification_text(event_type, facts, fallback):
    """Generate alternate wording for a system notification via the
    Anthropic API. `facts` is a dict of concrete values (names, amounts,
    dates, loan numbers, etc.) already computed by the caller -- the model
    is instructed to use ONLY these values and never invent numbers or
    dates of its own. `fallback` is the dict of static strings this app
    already sends today: {title, message, sms_message, email_subject,
    email_body_html}.

    Returns `fallback` unchanged if AI_NOTIFICATIONS isn't enabled, the API
    key isn't set, the call fails, times out, or the response can't be
    parsed -- callers never need their own try/except and existing
    notifications keep working exactly as before if anything goes wrong.
    """
    if os.getenv('AI_NOTIFICATIONS', 'false').strip().lower() not in ('1', 'true', 'yes'):
        return fallback
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return fallback

    system_prompt = (
        "You write short, professional notification text for a microfinance "
        "institution called Jodala Microfinance. You will be given an event "
        "type and a JSON object of facts (names, amounts, dates, account/loan "
        "numbers). Use ONLY the facts given -- never invent or guess any "
        "number, date, name, or figure not present in the facts. "
        "Respond with ONLY a JSON object (no markdown, no preamble) with "
        "these keys: title (a few words, in-app notification heading), "
        "message (1 sentence, in-app notification body, staff-facing/neutral "
        "tone), sms_message (1-2 short sentences, customer-facing, under 300 "
        "characters), email_subject (one line, customer-facing), "
        "email_body_html (a short customer-facing HTML email body using "
        "<p> tags, warm and professional, addressing the customer by name "
        "if a name fact is given)."
    )
    user_content = json.dumps({'event_type': event_type, 'facts': facts})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 600,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=5,
        )
        resp.raise_for_status()
        result = resp.json()
        text = "".join(
            block.get('text', '') for block in result.get('content', []) if block.get('type') == 'text'
        ).strip()
        text = text.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        parsed = json.loads(text)

        return {
            'title': parsed.get('title') or fallback.get('title'),
            'message': parsed.get('message') or fallback.get('message'),
            'sms_message': parsed.get('sms_message') or fallback.get('sms_message'),
            'email_subject': parsed.get('email_subject') or fallback.get('email_subject'),
            'email_body_html': parsed.get('email_body_html') or fallback.get('email_body_html'),
        }
    except Exception:
        # Any failure (network, timeout, bad JSON, rate limit, etc.) --
        # fall back silently to the existing static text rather than
        # blocking or breaking the loan/payment/savings action in progress.
        return fallback


def notify(user_id, title, message, notification_type='info', related_type=None,
           related_id=None, email=None, email_subject=None, email_body_html=None,
           notify_user_email=True, phone=None, sms_message=None,
           ai_event_type=None, ai_facts=None):
    """
    Central notification helper: always writes an in-app notification row.

    Two separate email audiences can be reached from one call:
      - Every active staff user, automatically whenever
        notify_user_email=True (the default). Each receives an individual
        plain notification email built from `title`/`message`.
      - An optional customer-facing recipient (`email`), for a member,
        client, or other non-staff address -- used for the nicer branded
        HTML in `email_body_html` (e.g. "Dear Jane, your loan was
        approved..."). Pass `email`/`email_subject`/`email_body_html`
        together for this.
    If both happen to be the same address (e.g. creating a new staff
    account email matches a staff email), only the customer-facing version
    is sent once -- not both.

    A customer-facing SMS can be sent alongside (or instead of) the email
    by passing `phone` -- the member/client's phone number on file. Uses
    `sms_message` if given, otherwise falls back to `message` (kept short
    since SMS has no HTML/formatting). Safe to pass even when SMS isn't
    configured/enabled -- core/sms.py send_sms_async() silently logs and
    no-ops in that case rather than failing the request. This is a
    customer channel only (mirroring `email`) -- staff aren't texted.

    user_id may be None for system-wide events that aren't tied to a
    dashboard user. In that case no notification row is written, but active
    staff users still receive the email copy.

    Pass ai_event_type (a short string like 'loan_approved') and ai_facts
    (a dict of concrete values already known to the caller) to have the
    text regenerated by AI for this send. Only takes effect when the
    AI_NOTIFICATIONS env var is enabled; otherwise, or on any failure, the
    static title/message/sms_message/email_subject/email_body_html passed
    in above are used exactly as given.
    """
    if ai_event_type:
        rewritten = _ai_notification_text(
            ai_event_type, ai_facts or {},
            fallback={
                'title': title, 'message': message, 'sms_message': sms_message,
                'email_subject': email_subject, 'email_body_html': email_body_html,
            }
        )
        title = rewritten['title']
        message = rewritten['message']
        sms_message = rewritten['sms_message']
        email_subject = rewritten['email_subject']
        email_body_html = rewritten['email_body_html']

    if user_id is not None:
        execute(
            """INSERT INTO notifications (user_id, title, message, notification_type,
                   related_type, related_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, title, message, notification_type, related_type, related_id, utcnow())
        )

    if notify_user_email:
        from core.mailer import send_email_async
        staff_users = get_db().execute(
            """SELECT email, full_name FROM users
               WHERE is_active = 1 AND email IS NOT NULL AND TRIM(email) <> ''"""
        ).fetchall()
        customer_email = (email or '').strip().casefold()
        for staff_user in staff_users:
            staff_email = staff_user['email'].strip()
            if staff_email.casefold() == customer_email:
                continue
            greeting = staff_user['full_name'] or 'there'
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

    if phone:
        from core.sms import send_sms_async
        send_sms_async(phone, sms_message or message)


def get_notification_recipient_ids():
    """Users configured (Settings > Notifications > Email Recipients) to
    receive email copies of system-wide notifications. Falls back to every
    active admin if nothing has been explicitly configured yet, so existing
    installs keep their current behaviour until an admin picks specific
    recipients."""
    row = get_db().execute(
        "SELECT value FROM company_settings WHERE key = 'notification_recipient_ids'"
    ).fetchone()
    raw = (row['value'] if row else '') or ''
    ids = [int(x) for x in raw.split(',') if x.strip().isdigit()]
    if ids:
        return ids
    admins = get_db().execute("SELECT id FROM users WHERE role = 'admin' AND is_active = 1").fetchall()
    return [a['id'] for a in admins]


def notify_admins(title, message, notification_type='info', related_type=None, related_id=None):
    """Write an in-app notification (and, via notify()'s default
    notify_user_email=True, an email) for whichever users are configured as
    email/system notification recipients (Settings > Notifications) -- all
    admins by default if nothing's been configured."""
    for user_id in get_notification_recipient_ids():
        notify(user_id, title, message, notification_type, related_type, related_id)



def paginate(base_sql, count_sql, params, page, per_page):
    """Runs count_sql/base_sql (base_sql must already ORDER BY) with LIMIT/OFFSET,
    returns (rows, total, pages)."""
    total_row = get_db().execute(count_sql, params).fetchone()
    total = total_row[0] if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    rows = get_db().execute(base_sql + " LIMIT %s OFFSET %s", params + (per_page, offset)).fetchall()
    return rows, total, pages
