"""
Internal, rules-based credit/risk score for members and clients.

There is no external credit bureau integration in Kenya's microfinance
space that's affordable or practical for an operation this size, so this
computes a score purely from data already in this database: how this
borrower has repaid in the past, whether they're currently in arrears, and
how much of a track record they have. It is deliberately simple and fully
explainable (no ML/black box) -- a loan officer should be able to look at
the breakdown and see exactly why a borrower scored the way they did,
which matters more for triage decisions than a marginally more "accurate"
opaque model would.

Score is 0-100, built from three weighted components:

  1. Repayment history (55 pts) -- of every schedule installment that has
     become due across ALL of this borrower's loans (paid or not), what
     fraction were paid by their due date? This is the single strongest
     signal of whether someone pays as agreed, so it carries the most
     weight. A borrower with no due installments yet (brand new, nothing
     has come due) gets a neutral 55% of this component rather than 0 or
     full marks -- they haven't demonstrated anything either way.

  2. Current arrears (35 pts) -- installments currently overdue (due_date
     in the past, not yet fully paid) on any of the borrower's loans.
     This is scored separately from #1 (rather than folded in) because a
     borrower who paid perfectly for a year and just fell behind this
     month is a materially different risk than one with the same
     historical on-time rate but no active arrears -- a loan officer
     needs to see both. Scored on both count AND how overdue the oldest
     one is, since one installment 3 days late is not the same risk as
     one 90 days late.

  3. Track record (10 pts) -- number of loans previously taken to
     completion (status = 'completed') without being written off. A
     repeat borrower who has already closed out loans successfully is a
     lower-risk bet than a first-time applicant, all else equal, but this
     is intentionally the smallest component -- it should nudge the
     score, not dominate it the way arrears or payment history should.

Bands (thresholds are a judgment call, not a regulatory standard -- adjust
BANDS below if your organization's risk appetite differs):
  80-100  Excellent  -- strong candidate for higher amounts / better terms
  65-79   Good        -- standard approval track
  45-64   Fair         -- approve with normal scrutiny, no special terms
  25-44   Poor         -- approve only with added safeguards (guarantor,
                          collateral, smaller amount) if at all
  0-24    High Risk    -- flag for manual review before any approval

This is a decision-support signal for loan officers, not an automatic
approve/reject gate -- nothing in the loan application flow currently
blocks on this score, by design.
"""
from datetime import date

from core.database import get_db

BANDS = (
    (80, 'Excellent'),
    (65, 'Good'),
    (45, 'Fair'),
    (25, 'Poor'),
    (0, 'High Risk'),
)


def _band_for(score):
    for threshold, label in BANDS:
        if score >= threshold:
            return label
    return 'High Risk'


def _borrower_column(borrower_type):
    if borrower_type == 'client':
        return 'client_id'
    return 'member_id'


def compute_credit_score(borrower_type, borrower_id):
    """
    borrower_type: 'member' or 'client'. Returns a dict:
      {score, band, components: {history, arrears, track_record},
       details: {...raw counts used, for display}}
    Safe to call for a borrower with zero loans -- returns a neutral
    starting score rather than raising.
    """
    db = get_db()
    column = _borrower_column(borrower_type)
    today = date.today().isoformat()

    loan_rows = db.execute(
        f"SELECT id, status FROM loans WHERE {column} = %s", (borrower_id,)
    ).fetchall()
    loan_ids = [l['id'] for l in loan_rows]

    if not loan_ids:
        # No borrowing history at all -- nothing to score against. Return
        # a neutral midpoint rather than a misleadingly high or low number.
        return {
            'score': 50,
            'band': _band_for(50),
            'components': {'history': 27, 'arrears': 33, 'track_record': 0},
            'details': {
                'installments_due': 0, 'installments_on_time': 0,
                'installments_overdue': 0, 'oldest_overdue_days': 0,
                'loans_completed': 0, 'has_history': False,
            },
        }

    placeholders = ','.join(['%s'] * len(loan_ids))

    # -- Component 1: repayment history --
    due_row = db.execute(
        f"""SELECT COUNT(*) AS due_count,
                   COUNT(*) FILTER (WHERE status = 'paid' AND paid_date IS NOT NULL
                                     AND paid_date <= due_date) AS on_time_count
            FROM loan_schedules
            WHERE loan_id IN ({placeholders}) AND due_date <= %s""",
        tuple(loan_ids) + (today,)
    ).fetchone()
    due_count = due_row['due_count'] or 0
    on_time_count = due_row['on_time_count'] or 0

    if due_count == 0:
        history_pts = 30.0  # neutral -- nothing has come due yet
    else:
        history_pts = 55.0 * (on_time_count / due_count)

    # -- Component 2: current arrears --
    overdue_row = db.execute(
        f"""SELECT COUNT(*) AS overdue_count,
                   COALESCE(MIN(due_date), %s) AS oldest_due_date
            FROM loan_schedules
            WHERE loan_id IN ({placeholders}) AND due_date < %s
              AND status IN ('pending', 'partial')""",
        (today,) + tuple(loan_ids) + (today,)
    ).fetchone()
    overdue_count = overdue_row['overdue_count'] or 0
    oldest_overdue_days = 0
    if overdue_count:
        oldest_due = overdue_row['oldest_due_date']
        try:
            oldest_overdue_days = (date.today() - date.fromisoformat(str(oldest_due)[:10])).days
        except (ValueError, TypeError):
            oldest_overdue_days = 0

    if overdue_count == 0:
        arrears_pts = 35.0
    else:
        # Deduct for both how many installments are overdue and how old
        # the oldest one is -- capped so it can't go negative.
        count_penalty = min(20.0, overdue_count * 5.0)
        age_penalty = min(15.0, oldest_overdue_days * 0.5)
        arrears_pts = max(0.0, 35.0 - count_penalty - age_penalty)

    # -- Component 3: track record --
    completed = sum(1 for l in loan_rows if l['status'] == 'completed')
    track_record_pts = min(10.0, completed * 3.0)

    score = round(history_pts + arrears_pts + track_record_pts)
    score = max(0, min(100, score))

    return {
        'score': score,
        'band': _band_for(score),
        'components': {
            'history': round(history_pts, 1),
            'arrears': round(arrears_pts, 1),
            'track_record': round(track_record_pts, 1),
        },
        'details': {
            'installments_due': due_count,
            'installments_on_time': on_time_count,
            'installments_overdue': overdue_count,
            'oldest_overdue_days': oldest_overdue_days,
            'loans_completed': completed,
            'has_history': True,
        },
    }
