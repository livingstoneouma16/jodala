"""
Tests for core/calculator.py -- the pure loan-math module (schedule
generation, summaries, penalties, payment allocation). No db, no Flask app
context needed; these run in isolation.

Run with:
    pytest tests/test_calculator.py -v
"""
import os
import importlib.util
from datetime import date

# core/calculator.py has zero dependencies (no db, no Flask -- see its own
# module docstring), but `import core.calculator` would run core/__init__.py
# first as part of importing the parent package, which *does* need Flask,
# flask_cors, etc. installed. Load the module directly by file path instead,
# so this test only needs pytest -- matching the module's own "pure
# functions ... easy to unit test" design intent.
_calc_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core', 'calculator.py')
_spec = importlib.util.spec_from_file_location('calculator', _calc_path)
calculator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calculator)

add_months = calculator.add_months
next_due_date = calculator.next_due_date
loan_summary = calculator.loan_summary
build_loan_schedule = calculator.build_loan_schedule
calculate_penalty = calculator.calculate_penalty
allocate_payment = calculator.allocate_payment


# ---------------------------------------------------------------------------
# add_months / next_due_date
# ---------------------------------------------------------------------------
class TestAddMonths:
    def test_simple_add(self):
        assert add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)

    def test_year_rollover(self):
        assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)

    def test_month_end_clamps_to_shorter_month(self):
        # Jan 31 + 1 month -> Feb has no 31st, should clamp to Feb 28/29.
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_clamps_into_leap_february(self):
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)  # 2028 is a leap year

    def test_zero_months_is_identity(self):
        assert add_months(date(2026, 6, 10), 0) == date(2026, 6, 10)

    def test_multi_year_add(self):
        assert add_months(date(2026, 1, 1), 25) == date(2028, 2, 1)


class TestNextDueDate:
    def test_daily(self):
        start = date(2026, 1, 1)
        assert next_due_date(start, 5, 'daily') == date(2026, 1, 6)

    def test_weekly(self):
        start = date(2026, 1, 1)
        assert next_due_date(start, 2, 'weekly') == date(2026, 1, 15)

    def test_monthly_default(self):
        start = date(2026, 1, 31)
        assert next_due_date(start, 1, 'monthly') == date(2026, 2, 28)

    def test_unknown_frequency_falls_back_to_monthly(self):
        start = date(2026, 1, 1)
        assert next_due_date(start, 1, 'fortnightly') == date(2026, 2, 1)


# ---------------------------------------------------------------------------
# loan_summary
# ---------------------------------------------------------------------------
class TestLoanSummaryFlat:
    def test_basic_flat_interest(self):
        # 10,000 at 5%/period flat over 12 periods -> interest = 10000*0.05*12 = 6000
        s = loan_summary(10000, 5, 12, interest_type='flat')
        assert s['total_interest'] == 6000
        assert s['total_repayable'] == 16000
        assert s['installment_amount'] == round(16000 / 12, 2)

    def test_zero_interest_rate(self):
        s = loan_summary(12000, 0, 12, interest_type='flat')
        assert s['total_interest'] == 0
        assert s['installment_amount'] == 1000

    def test_insurance_fee_reduces_net_disbursement_not_principal(self):
        s = loan_summary(10000, 5, 12, interest_type='flat', insurance_fee_pct=2)
        assert s['insurance_fee'] == 200
        assert s['principal'] == 10000  # principal itself is untouched
        assert s['net_disbursement'] == 9800  # only disbursement is reduced

    def test_single_term_period(self):
        s = loan_summary(1000, 10, 1, interest_type='flat')
        assert s['total_interest'] == 100
        assert s['installment_amount'] == 1100


class TestLoanSummaryReducingBalance:
    def test_reducing_balance_interest_less_than_flat(self):
        # Reducing balance should always charge less total interest than flat
        # for the same nominal rate, since interest accrues on a shrinking balance.
        flat = loan_summary(10000, 5, 12, interest_type='flat')
        reducing = loan_summary(10000, 5, 12, interest_type='reducing')
        assert reducing['total_interest'] < flat['total_interest']

    def test_reducing_balance_zero_rate(self):
        s = loan_summary(12000, 0, 12, interest_type='reducing')
        assert s['total_interest'] == 0
        assert s['installment_amount'] == 1000

    def test_reducing_balance_repayable_covers_principal(self):
        s = loan_summary(5000, 3, 6, interest_type='reducing')
        assert s['total_repayable'] >= s['principal']


# ---------------------------------------------------------------------------
# build_loan_schedule
# ---------------------------------------------------------------------------
class TestBuildLoanScheduleFlat:
    def test_schedule_length_matches_term(self):
        sched = build_loan_schedule(10000, 5, 12, 'flat', 'monthly', date(2026, 1, 1))
        assert len(sched) == 12

    def test_final_balance_is_zero(self):
        sched = build_loan_schedule(10000, 5, 12, 'flat', 'monthly', date(2026, 1, 1))
        assert sched[-1]['balance_after'] == 0

    def test_installment_numbers_sequential(self):
        sched = build_loan_schedule(10000, 5, 6, 'flat', 'monthly', date(2026, 1, 1))
        assert [row['installment_number'] for row in sched] == list(range(1, 7))

    def test_due_dates_use_monthly_frequency(self):
        sched = build_loan_schedule(10000, 5, 3, 'flat', 'monthly', date(2026, 1, 31))
        assert sched[0]['due_date'] == date(2026, 2, 28)
        assert sched[1]['due_date'] == date(2026, 3, 31)

    def test_principal_dues_sum_to_principal(self):
        sched = build_loan_schedule(9999, 7, 11, 'flat', 'monthly', date(2026, 1, 1))
        total_principal = round(sum(row['principal_due'] for row in sched), 2)
        assert total_principal == 9999


class TestBuildLoanScheduleReducingBalance:
    def test_final_balance_is_zero(self):
        sched = build_loan_schedule(10000, 5, 12, 'reducing', 'monthly', date(2026, 1, 1))
        assert sched[-1]['balance_after'] == 0

    def test_interest_due_decreases_over_time(self):
        # On reducing balance, each period's interest should be <= the previous
        # period's (balance only shrinks or holds, rate is fixed).
        sched = build_loan_schedule(10000, 5, 12, 'reducing', 'monthly', date(2026, 1, 1))
        interest_amounts = [row['interest_due'] for row in sched]
        assert all(interest_amounts[i] >= interest_amounts[i + 1] for i in range(len(interest_amounts) - 1))

    def test_balance_never_goes_negative(self):
        sched = build_loan_schedule(10000, 5, 12, 'reducing', 'monthly', date(2026, 1, 1))
        assert all(row['balance_after'] >= 0 for row in sched)

    def test_weekly_frequency_schedule(self):
        sched = build_loan_schedule(5000, 2, 4, 'reducing', 'weekly', date(2026, 1, 1))
        assert sched[0]['due_date'] == date(2026, 1, 8)
        assert sched[-1]['balance_after'] == 0


# ---------------------------------------------------------------------------
# calculate_penalty
# ---------------------------------------------------------------------------
class TestCalculatePenalty:
    def test_no_penalty_when_not_overdue(self):
        assert calculate_penalty(10000, 1, 0) == 0.0

    def test_no_penalty_for_negative_days(self):
        assert calculate_penalty(10000, 1, -5) == 0.0

    def test_penalty_scales_with_days_overdue(self):
        assert calculate_penalty(10000, 1, 5) == 500.0

    def test_penalty_scales_with_rate(self):
        assert calculate_penalty(10000, 0.5, 10) == 500.0


# ---------------------------------------------------------------------------
# allocate_payment
# ---------------------------------------------------------------------------
class TestAllocatePayment:
    def _sched(self, id, total_due, total_paid, principal_due, interest_due):
        return {
            'id': id, 'total_due': total_due, 'total_paid': total_paid,
            'principal_due': principal_due, 'interest_due': interest_due,
        }

    def test_exact_payment_fully_settles_one_schedule(self):
        schedules = [self._sched(1, 1000, 0, 800, 200)]
        updates = allocate_payment(1000, schedules)
        assert len(updates) == 1
        assert updates[0]['fully_paid'] is True
        assert updates[0]['total_paid_delta'] == 1000

    def test_partial_payment_leaves_schedule_unpaid(self):
        schedules = [self._sched(1, 1000, 0, 800, 200)]
        updates = allocate_payment(400, schedules)
        assert updates[0]['fully_paid'] is False
        assert updates[0]['total_paid_delta'] == 400

    def test_overpayment_spills_to_next_schedule_oldest_first(self):
        schedules = [
            self._sched(1, 500, 0, 400, 100),
            self._sched(2, 500, 0, 400, 100),
        ]
        updates = allocate_payment(700, schedules)
        assert len(updates) == 2
        assert updates[0]['schedule_id'] == 1
        assert updates[0]['fully_paid'] is True
        assert updates[1]['schedule_id'] == 2
        assert updates[1]['total_paid_delta'] == 200
        assert updates[1]['fully_paid'] is False

    def test_already_fully_paid_schedules_are_skipped(self):
        schedules = [
            self._sched(1, 500, 500, 400, 100),  # already settled
            self._sched(2, 500, 0, 400, 100),
        ]
        updates = allocate_payment(500, schedules)
        assert len(updates) == 1
        assert updates[0]['schedule_id'] == 2

    def test_payment_larger_than_total_outstanding_only_allocates_what_is_due(self):
        schedules = [self._sched(1, 500, 0, 400, 100)]
        updates = allocate_payment(10000, schedules)
        assert len(updates) == 1
        assert updates[0]['total_paid_delta'] == 500

    def test_principal_interest_split_proportional_to_partial_payment(self):
        schedules = [self._sched(1, 1000, 0, 800, 200)]
        updates = allocate_payment(500, schedules)
        # ratio = 500/1000 = 0.5 -> principal 400, interest 100
        assert updates[0]['principal_paid_delta'] == 400
        assert updates[0]['interest_paid_delta'] == 100

    def test_zero_payment_produces_no_updates(self):
        schedules = [self._sched(1, 1000, 0, 800, 200)]
        updates = allocate_payment(0, schedules)
        assert updates == []
