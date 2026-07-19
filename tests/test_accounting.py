"""
Ledger/general-ledger tests: chart of accounts, trial balance, manual
journal entries, and the accounting side-effects of income/expense/loan
write-off. These exercise core/routes/accounting.py and the
adjust_account_balance / post_journal_line helpers in core/utils.

Run with: pytest tests/test_accounting.py -v
"""
from conftest import auth_header


def _account_id(client, admin_token, code):
    accounts = client.get('/accounting/api/accounts', headers=auth_header(admin_token)).get_json()
    return next(a['id'] for a in accounts if a['code'] == code)


def _trial_balance_row(client, admin_token, code):
    tb = client.get('/accounting/api/trial-balance', headers=auth_header(admin_token)).get_json()
    return next(r for r in tb['accounts'] if r['code'] == code)


class TestIncomeExpensePostToLedger:
    def test_income_increases_cash_and_income_account(self, client, admin_token):
        before_cash = _trial_balance_row(client, admin_token, '1000')['debit']
        before_fee = _trial_balance_row(client, admin_token, '4100')['credit']

        resp = client.post('/accounting/api/income', json={
            'description': 'Membership fee', 'category': 'fees', 'amount': 500,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)

        assert _trial_balance_row(client, admin_token, '1000')['debit'] == round(before_cash + 500, 2)
        assert _trial_balance_row(client, admin_token, '4100')['credit'] == round(before_fee + 500, 2)

    def test_expense_decreases_cash_increases_expense_account(self, client, admin_token):
        before_cash = _trial_balance_row(client, admin_token, '1000')['debit']
        before_exp = _trial_balance_row(client, admin_token, '5000')['debit']

        resp = client.post('/accounting/api/expenses', json={
            'description': 'Office rent', 'category': 'rent', 'amount': 300,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)

        assert _trial_balance_row(client, admin_token, '1000')['debit'] == round(before_cash - 300, 2)
        assert _trial_balance_row(client, admin_token, '5000')['debit'] == round(before_exp + 300, 2)

    def test_trial_balance_always_balances(self, client, admin_token):
        client.post('/accounting/api/income', json={'description': 'x', 'amount': 777}, headers=auth_header(admin_token))
        client.post('/accounting/api/expenses', json={'description': 'y', 'amount': 111}, headers=auth_header(admin_token))
        tb = client.get('/accounting/api/trial-balance', headers=auth_header(admin_token)).get_json()
        assert tb['total_debit'] == tb['total_credit']


class TestManualJournalEntry:
    def test_valid_entry_posts_to_ledger(self, client, admin_token):
        cash_id = _account_id(client, admin_token, '1000')
        equity_id = _account_id(client, admin_token, '3000')
        before_cash = _trial_balance_row(client, admin_token, '1000')['debit']
        before_equity = _trial_balance_row(client, admin_token, '3000')['credit']

        resp = client.post('/accounting/api/journal', json={
            'description': 'Owner capital injection',
            'lines': [{'debit_account_id': cash_id, 'credit_account_id': equity_id, 'amount': 1000}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)

        # This is the fix: a manual entry used to write journal_entries /
        # journal_entry_lines rows but never touch accounts.balance at all.
        assert _trial_balance_row(client, admin_token, '1000')['debit'] == round(before_cash + 1000, 2)
        assert _trial_balance_row(client, admin_token, '3000')['credit'] == round(before_equity + 1000, 2)

        entries = client.get('/accounting/api/journal', headers=auth_header(admin_token)).get_json()
        assert entries[0]['lines'][0]['debit_account_code'] == '1000'
        assert entries[0]['lines'][0]['credit_account_code'] == '3000'

    def test_rejects_missing_description(self, client, admin_token):
        cash_id = _account_id(client, admin_token, '1000')
        equity_id = _account_id(client, admin_token, '3000')
        resp = client.post('/accounting/api/journal', json={
            'description': '', 'lines': [{'debit_account_id': cash_id, 'credit_account_id': equity_id, 'amount': 100}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_rejects_entry_with_no_lines(self, client, admin_token):
        resp = client.post('/accounting/api/journal', json={'description': 'Nothing here', 'lines': []},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_rejects_zero_amount_line(self, client, admin_token):
        cash_id = _account_id(client, admin_token, '1000')
        equity_id = _account_id(client, admin_token, '3000')
        resp = client.post('/accounting/api/journal', json={
            'description': 'Bad line', 'lines': [{'debit_account_id': cash_id, 'credit_account_id': equity_id, 'amount': 0}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_rejects_same_account_on_both_sides(self, client, admin_token):
        cash_id = _account_id(client, admin_token, '1000')
        resp = client.post('/accounting/api/journal', json={
            'description': 'Self-referencing line',
            'lines': [{'debit_account_id': cash_id, 'credit_account_id': cash_id, 'amount': 50}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_rejects_unknown_account_id(self, client, admin_token):
        cash_id = _account_id(client, admin_token, '1000')
        resp = client.post('/accounting/api/journal', json={
            'description': 'Bad account', 'lines': [{'debit_account_id': cash_id, 'credit_account_id': 999999, 'amount': 50}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400


class TestWriteOffPostsToLedger:
    def test_write_off_reduces_receivable_and_books_bad_debt(self, client, admin_token, approved_loan):
        before_receivable = _trial_balance_row(client, admin_token, '1100')['debit']
        before_writeoff_exp = _trial_balance_row(client, admin_token, '5100')['debit']
        principal_owed = before_receivable  # only loan on the books in this test

        resp = client.post(f"/loans/api/{approved_loan['id']}/write-off",
                            json={'reason': 'Borrower deceased'}, headers=auth_header(admin_token))
        assert resp.status_code == 200, resp.get_data(as_text=True)

        assert _trial_balance_row(client, admin_token, '1100')['debit'] == round(before_receivable - principal_owed, 2)
        assert _trial_balance_row(client, admin_token, '5100')['debit'] == round(before_writeoff_exp + principal_owed, 2)

        loan = client.get(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token)).get_json()
        assert loan['outstanding_balance'] == 0

    def test_cannot_delete_written_off_loan(self, client, admin_token, approved_loan):
        client.post(f"/loans/api/{approved_loan['id']}/write-off", json={'reason': 'test'},
                    headers=auth_header(admin_token))
        resp = client.delete(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token))
        assert resp.status_code == 400
