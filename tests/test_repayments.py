"""
Repayment tests: recording a payment against a disbursed loan, verifying it
actually reduces outstanding_balance and updates the schedule (not just
that the HTTP call returns 200 -- that's the part most likely to hide a
money bug), plus the validation guards around it.

Run with: pytest tests/test_repayments.py -v
"""
from conftest import auth_header


class TestRecordRepayment:
    def test_valid_repayment_reduces_outstanding_balance(self, client, admin_token, approved_loan):
        starting_balance = approved_loan['outstanding_balance']

        resp = client.post('/repayments/api', json={
            'loan_id': approved_loan['id'],
            'amount': 1000,
            'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['loan_balance'] == round(starting_balance - 1000, 2)

        follow_up = client.get(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token)).get_json()
        assert follow_up['outstanding_balance'] == round(starting_balance - 1000, 2)
        # At least one schedule installment should have absorbed some of
        # this payment (partial or fully paid).
        assert any(s['status'] in ('partial', 'paid') for s in follow_up['schedule'])

    def test_repayment_appears_in_loan_repayment_history(self, client, admin_token, approved_loan):
        client.post('/repayments/api', json={
            'loan_id': approved_loan['id'], 'amount': 500, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))

        follow_up = client.get(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token)).get_json()
        assert len(follow_up['repayments']) == 1
        assert follow_up['repayments'][0]['amount'] == 500

    def test_zero_amount_rejected(self, client, admin_token, approved_loan):
        resp = client.post('/repayments/api', json={
            'loan_id': approved_loan['id'], 'amount': 0, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_negative_amount_rejected(self, client, admin_token, approved_loan):
        resp = client.post('/repayments/api', json={
            'loan_id': approved_loan['id'], 'amount': -500, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_repayment_against_nonexistent_loan_404s(self, client, admin_token):
        resp = client.post('/repayments/api', json={
            'loan_id': 999999, 'amount': 500, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 404

    def test_repayment_against_pending_loan_rejected(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        # Never approved/disbursed -- there's no schedule to allocate
        # against yet, so accepting a payment here would be silently lost
        # money.
        resp = client.post('/repayments/api', json={
            'loan_id': created['id'], 'amount': 500, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_multiple_partial_repayments_accumulate_correctly(self, client, admin_token, approved_loan):
        starting_balance = approved_loan['outstanding_balance']

        client.post('/repayments/api', json={
            'loan_id': approved_loan['id'], 'amount': 300, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        second = client.post('/repayments/api', json={
            'loan_id': approved_loan['id'], 'amount': 400, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))

        assert second.get_json()['loan_balance'] == round(starting_balance - 700, 2)
