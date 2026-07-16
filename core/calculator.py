"""
calculator.py
-------------
All loan-math lives here: schedule generation, repayable/interest summaries,
and penalty calculation. Pure functions only -- no db, no Flask -- so they're
easy to unit test and reuse from both routes and any React /v3 API layer.
"""
from datetime import date, timedelta

DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def add_months(start_date, months):
    month = start_date.month + months
    year = start_date.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    max_day = DAYS_IN_MONTH[month - 1]
    if month == 2 and _is_leap(year):
        max_day = 29
    day = min(start_date.day, max_day)
    return date(year, month, day)


def next_due_date(start_date, installment_number, repayment_frequency):
    if repayment_frequency == 'daily':
        return start_date + timedelta(days=installment_number)
    if repayment_frequency == 'weekly':
        return start_date + timedelta(weeks=installment_number)
    return add_months(start_date, installment_number)  # monthly (default)


def loan_summary(principal, interest_rate, term, interest_type='flat',
                  processing_fee_pct=0, insurance_fee_pct=0):
    """
    Headline numbers for a proposed loan, before a day-by-day schedule
    is generated. interest_rate is a percentage per repayment period.
    """
    principal = float(principal)
    interest_rate = float(interest_rate)
    term = int(term)
    processing_fee_pct = float(processing_fee_pct or 0)
    insurance_fee_pct = float(insurance_fee_pct or 0)

    if interest_type == 'flat':
        total_interest = (principal * interest_rate / 100) * term
        installment_amount = round((principal + total_interest) / term, 2) if term else 0
    else:  # reducing balance
        period_rate = interest_rate / 100
        if period_rate == 0:
            installment_amount = round(principal / term, 2) if term else 0
        else:
            installment_amount = round(
                principal * (period_rate * (1 + period_rate) ** term) / ((1 + period_rate) ** term - 1), 2
            )
        total_interest = round((installment_amount * term) - principal, 2)

    processing_fee = round(principal * processing_fee_pct / 100, 2)
    insurance_fee = round(principal * insurance_fee_pct / 100, 2)
    total_repayable = round(principal + total_interest, 2)

    return {
        'principal': round(principal, 2),
        'interest_rate': interest_rate,
        'interest_type': interest_type,
        'term': term,
        'total_interest': round(total_interest, 2),
        'total_repayable': total_repayable,
        'installment_amount': installment_amount,
        'processing_fee': processing_fee,
        'insurance_fee': insurance_fee,
        'net_disbursement': round(principal - processing_fee - insurance_fee, 2),
    }


def build_loan_schedule(principal, interest_rate, term, interest_type,
                         repayment_frequency, start_date):
    """
    Generate the full installment-by-installment repayment schedule.
    Returns a list of dicts: installment_number, due_date (date), principal_due,
    interest_due, total_due, balance_after.
    """
    principal = float(principal)
    interest_rate = float(interest_rate)
    term = int(term)

    schedule = []
    balance = principal

    if interest_type == 'flat':
        total_interest = (principal * interest_rate / 100) * term
        period_interest = total_interest / term if term else 0
        period_principal = principal / term if term else 0
        period_payment = period_principal + period_interest
    else:  # reducing balance
        period_rate = interest_rate / 100
        if period_rate == 0:
            period_payment = principal / term if term else 0
        else:
            period_payment = principal * (period_rate * (1 + period_rate) ** term) / ((1 + period_rate) ** term - 1)

    for i in range(1, term + 1):
        due_date = next_due_date(start_date, i, repayment_frequency)

        if interest_type == 'flat':
            inst_principal = period_principal
            inst_interest = period_interest
        else:
            inst_interest = balance * (interest_rate / 100)
            inst_principal = period_payment - inst_interest
            if inst_principal > balance:
                inst_principal = balance

        balance_after = max(0, balance - inst_principal)

        schedule.append({
            'installment_number': i,
            'due_date': due_date,
            'principal_due': round(inst_principal, 2),
            'interest_due': round(inst_interest, 2),
            'total_due': round(inst_principal + inst_interest, 2),
            'balance_after': round(balance_after, 2),
        })
        balance = balance_after

    return schedule


def calculate_penalty(outstanding_balance, penalty_rate_pct, days_overdue):
    """penalty_rate_pct is a percentage-per-day rate applied to the outstanding balance."""
    if days_overdue <= 0:
        return 0.0
    return round(outstanding_balance * (penalty_rate_pct / 100) * days_overdue, 2)


def allocate_payment(amount, schedules):
    """
    Allocate an incoming repayment across a list of outstanding schedule rows
    (dicts with total_due, total_paid, principal_due, interest_due), oldest
    due_date first. Mutates nothing -- returns a list of per-schedule updates
    plus the totals to apply to the parent loan.

    Each update: {schedule_id, principal_paid_delta, interest_paid_delta,
                   total_paid_delta, new_status, paid_date_needed}
    """
    remaining = float(amount)
    updates = []

    for sched in schedules:
        if remaining <= 0:
            break

        total_due = sched['total_due']
        already_paid = sched['total_paid']
        outstanding_on_sched = total_due - already_paid
        if outstanding_on_sched <= 0:
            continue

        pay_now = min(remaining, outstanding_on_sched)
        ratio = pay_now / total_due if total_due else 0
        updates.append({
            'schedule_id': sched['id'],
            'principal_paid_delta': round(sched['principal_due'] * ratio, 2),
            'interest_paid_delta': round(sched['interest_due'] * ratio, 2),
            'total_paid_delta': pay_now,
            'fully_paid': pay_now >= outstanding_on_sched - 0.0001,
        })
        remaining -= pay_now

    return updates
