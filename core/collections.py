"""
Automated collections escalation ladder.

The existing daily job (send_overdue_reminders, core/scheduler.py) just
re-sends an SMS/email every day a loan has an overdue installment -- it
doesn't track progression or give staff anything to act on beyond that
message. This adds a day-threshold ladder on top of it:

    day 1  -> SMS reminder            (already handled by send_overdue_reminders)
    day 7  -> "Call borrower" task
    day 30 -> "Field visit" task
    day 60 -> "Write-off recommendation" task

run_collections_escalation() is meant to run once a day, right alongside
send_overdue_reminders -- see core/scheduler.py and
send_overdue_reminders.py for where it's wired in. It's idempotent: a task
is only ever created once per (loan, stage) thanks to the UNIQUE(loan_id,
stage) constraint on collection_tasks, so re-running it daily doesn't spam
duplicates, and a loan that's already past day 30 when this first runs
gets both its day-7 and day-30 tasks created immediately (nothing to
"catch up" on later).

Stages are keyed by the *maximum* days-overdue across a loan's overdue
installments, using the oldest unpaid installment, since that's the
figure a collections stage should actually escalate against.
"""
import os
from datetime import date

from core.database import get_db, execute, utcnow
from core.utils import log_audit, notify, format_currency


# (stage_key, min_days_overdue, human label, task type for notify()/UI)
# Ordered from earliest to latest -- run_collections_escalation() walks
# this in order and creates every stage the loan now qualifies for that
# doesn't already have a task, so a loan that jumps straight past an
# earlier threshold (e.g. first checked at day 45) still gets its day-7
# task created, not just day-30.
COLLECTION_LADDER = [
    {'stage': 'day7_call', 'min_days': 7,
     'label': 'Call borrower', 'action': 'Phone the borrower to confirm they know about the overdue payment and agree a plan.'},
    {'stage': 'day30_field_visit', 'min_days': 30,
     'label': 'Field visit', 'action': 'Visit the borrower in person -- phone follow-up alone has not resolved this.'},
    {'stage': 'day60_writeoff_review', 'min_days': 60,
     'label': 'Write-off recommendation', 'action': 'Review this loan for a formal write-off recommendation.'},
]


def _oldest_overdue_days(loan_id, today):
    """Days overdue on the single oldest unpaid/partial installment for
    this loan -- the figure the ladder escalates against."""
    row = get_db().execute(
        """SELECT MIN(due_date) AS oldest_due FROM loan_schedules
           WHERE loan_id = %s AND due_date < %s AND status IN ('pending', 'partial')""",
        (loan_id, today.isoformat())
    ).fetchone()
    if not row or not row['oldest_due']:
        return None
    oldest_due = date.fromisoformat(row['oldest_due'])
    return (today - oldest_due).days


def run_collections_escalation():
    """Walk every active loan with an overdue installment and create any
    collection_tasks rows it now qualifies for but doesn't already have.
    Safe to call repeatedly (e.g. once a day) -- existing tasks are never
    duplicated or recreated. Returns a summary dict."""
    from core.utils import get_overdue_loan_ids

    today = date.today()
    loan_ids = get_overdue_loan_ids()
    created, skipped = 0, 0

    for loan_id in loan_ids:
        loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
        if not loan or loan['status'] != 'active':
            continue

        days_overdue = _oldest_overdue_days(loan_id, today)
        if days_overdue is None:
            continue

        for rung in COLLECTION_LADDER:
            if days_overdue < rung['min_days']:
                continue

            existing = get_db().execute(
                "SELECT id FROM collection_tasks WHERE loan_id = %s AND stage = %s",
                (loan_id, rung['stage'])
            ).fetchone()
            if existing:
                skipped += 1
                continue

            now = utcnow()
            execute(
                """INSERT INTO collection_tasks
                       (loan_id, stage, days_overdue_at_creation, outstanding_at_creation,
                        status, assigned_to, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'open', %s, %s, %s)""",
                (loan_id, rung['stage'], days_overdue, loan['outstanding_balance'],
                 loan['loan_officer_id'], now, now)
            )
            created += 1

            log_audit(
                'COLLECTION_TASK_CREATED', 'loan', loan_id,
                new_values={'stage': rung['stage'], 'days_overdue': days_overdue,
                            'outstanding': loan['outstanding_balance']}
            )

            if loan['loan_officer_id']:
                notify(
                    loan['loan_officer_id'],
                    f"Collections: {rung['label']}",
                    f"Loan {loan['loan_number']} is {days_overdue} days overdue "
                    f"({format_currency(loan['outstanding_balance'])} outstanding). {rung['action']}",
                    notification_type='warning', related_type='loan', related_id=loan_id,
                )

    return {'loans_checked': len(loan_ids), 'tasks_created': created, 'tasks_already_open': skipped}


def collections_ladder_public():
    """The ladder definition, for the front-end to render stage labels/order."""
    return [{'stage': r['stage'], 'min_days': r['min_days'], 'label': r['label'], 'action': r['action']}
            for r in COLLECTION_LADDER]
