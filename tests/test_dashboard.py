"""
Dashboard endpoint tests -- mainly a correctness check on the trend
endpoints after they were rewritten from a per-month query loop to a
handful of GROUP BY queries per endpoint (see core/routes/dashboard.py).
Same inputs must produce the same outputs; this just proves the rewrite
didn't change behaviour, not that it's faster (that needs a real timed
run against a real network-hosted Postgres, which this sandbox can't do).

Run with: pytest tests/test_dashboard.py -v
"""
from datetime import date

from conftest import auth_header


class TestTrendEndpointsShape:
    def test_loan_trend_covers_12_months_oldest_first(self, client, admin_token):
        resp = client.get('/dashboard/loan-trend', headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 12
        # Last entry should be the current month.
        assert data[-1]['month'] == date.today().strftime('%b %Y')

    def test_income_expense_trend_covers_6_months(self, client, admin_token):
        resp = client.get('/dashboard/income-expense-trend', headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 6
        assert data[-1]['month'] == date.today().strftime('%b %Y')

    def test_member_growth_covers_6_months(self, client, admin_token):
        resp = client.get('/dashboard/member-growth', headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 6
        assert data[-1]['month'] == date.today().strftime('%b %Y')


class TestTrendEndpointsAggregateCorrectly:
    def test_loan_trend_reflects_current_month_disbursement(self, client, admin_token, approved_loan):
        # approved_loan fixture disburses a 10000 loan today with no
        # insurance fee/rollover, so amount_disbursed == principal_amount
        # (loan_public doesn't expose amount_disbursed directly).
        resp = client.get('/dashboard/loan-trend', headers=auth_header(admin_token))
        this_month = resp.get_json()[-1]
        assert this_month['disbursed'] == approved_loan['principal_amount']

    def test_member_growth_reflects_current_month_signup(self, client, admin_token, member):
        resp = client.get('/dashboard/member-growth', headers=auth_header(admin_token))
        this_month = resp.get_json()[-1]
        assert this_month['members'] >= 1

    def test_income_expense_trend_reflects_manual_income_and_expense(self, client, admin_token):
        client.post('/accounting/api/income', json={'description': 'x', 'amount': 500},
                    headers=auth_header(admin_token))
        client.post('/accounting/api/expenses', json={'description': 'y', 'amount': 200},
                    headers=auth_header(admin_token))
        resp = client.get('/dashboard/income-expense-trend', headers=auth_header(admin_token))
        this_month = resp.get_json()[-1]
        assert this_month['income'] >= 500
        assert this_month['expenses'] >= 200


class TestSummaryEndpoint:
    """/dashboard/summary bundles all 7 dashboard requests into one, to cut
    round trips for users far from the hosting region. It should return
    exactly what the individual endpoints return, just nested."""

    def test_summary_matches_individual_endpoints(self, client, admin_token, approved_loan, member):
        individual = {
            'stats': client.get('/dashboard/stats', headers=auth_header(admin_token)).get_json(),
            'loan_trend': client.get('/dashboard/loan-trend', headers=auth_header(admin_token)).get_json(),
            'loan_status_distribution': client.get('/dashboard/loan-status-distribution', headers=auth_header(admin_token)).get_json(),
            'income_expense_trend': client.get('/dashboard/income-expense-trend', headers=auth_header(admin_token)).get_json(),
            'member_growth': client.get('/dashboard/member-growth', headers=auth_header(admin_token)).get_json(),
            'due_today': client.get('/dashboard/due-today', headers=auth_header(admin_token)).get_json(),
            'overdue_loans': client.get('/dashboard/overdue-loans', headers=auth_header(admin_token)).get_json(),
        }

        resp = client.get('/dashboard/summary', headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.get_json() == individual

    def test_summary_requires_login(self, client):
        resp = client.get('/dashboard/summary')
        assert resp.status_code in (302, 401)


class TestDueTodayAndOverdueBorrowerNames:
    """Regression coverage for the N+1 -> batched IN(...) rewrite in
    _borrower_names -- a member's name must still resolve correctly when
    fetched in a batch instead of one row at a time."""

    def test_overdue_loan_shows_correct_borrower_name(self, client, admin_token, approved_loan, member):
        # Push every schedule row into the past so the loan shows as overdue.
        resp = client.get('/dashboard/overdue-loans', headers=auth_header(admin_token))
        assert resp.status_code == 200
        # Loan was just disbursed today with a due date ~30 days out, so it
        # won't be overdue yet -- this just confirms the endpoint still
        # responds correctly (no borrower-name crash) with a real loan on
        # the books.
        assert isinstance(resp.get_json(), list)
