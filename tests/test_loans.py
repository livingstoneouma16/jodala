"""
Loan lifecycle tests: application -> approval/rejection -> disbursement ->
write-off, plus the state-machine guards that stop money moving out of
order (e.g. disbursing an unapproved loan, approving twice).

Run with: pytest tests/test_loans.py -v
"""
from conftest import auth_header


class TestLoanApplication:
    def test_create_loan_within_product_limits(self, client, admin_token, member, loan_product):
        resp = client.post('/loans/api', json={
            'member_id': member['id'],
            'borrower_type': 'member',
            'product_id': loan_product['id'],
            'principal_amount': 10000,
            'term': 6,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        loan = resp.get_json()['loan']
        assert loan['status'] == 'pending'
        assert loan['principal_amount'] == 10000

    def test_principal_below_product_minimum_rejected(self, client, admin_token, member, loan_product):
        resp = client.post('/loans/api', json={
            'member_id': member['id'],
            'borrower_type': 'member',
            'product_id': loan_product['id'],
            'principal_amount': 1,  # below min_amount=1000
            'term': 6,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_principal_above_product_maximum_rejected(self, client, admin_token, member, loan_product):
        resp = client.post('/loans/api', json={
            'member_id': member['id'],
            'borrower_type': 'member',
            'product_id': loan_product['id'],
            'principal_amount': 10_000_000,  # above max_amount=500000
            'term': 6,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_term_outside_product_range_rejected(self, client, admin_token, member, loan_product):
        resp = client.post('/loans/api', json={
            'member_id': member['id'],
            'borrower_type': 'member',
            'product_id': loan_product['id'],
            'principal_amount': 10000,
            'term': 999,  # above max_term=24
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_invalid_product_id_rejected(self, client, admin_token, member):
        resp = client.post('/loans/api', json={
            'member_id': member['id'],
            'borrower_type': 'member',
            'product_id': 999999,
            'principal_amount': 10000,
            'term': 6,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400


class TestApprovalAndRejection:
    def test_approve_pending_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        resp = client.post(f"/loans/api/{created['id']}/approve", json={},
                            headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.get_json()['loan']['status'] == 'approved'

    def test_cannot_approve_already_approved_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']
        client.post(f"/loans/api/{created['id']}/approve", json={}, headers=auth_header(admin_token))

        resp = client.post(f"/loans/api/{created['id']}/approve", json={},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_approve_nonexistent_loan_404s(self, client, admin_token):
        resp = client.post('/loans/api/999999/approve', json={}, headers=auth_header(admin_token))
        assert resp.status_code == 404


class TestDisbursement:
    def test_cannot_disburse_pending_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        # Never approved -- disbursing straight from 'pending' must be
        # blocked, or a rejected/unapproved loan could pay out.
        resp = client.post(f"/loans/api/{created['id']}/disburse", json={},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_disburse_approved_loan_builds_schedule_and_activates(self, client, admin_token, approved_loan):
        assert approved_loan['status'] == 'active'
        assert approved_loan['outstanding_balance'] > 0
        assert len(approved_loan['schedule']) == 6  # term=6, monthly

    def test_cannot_disburse_twice(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']
        client.post(f"/loans/api/{created['id']}/approve", json={}, headers=auth_header(admin_token))
        first = client.post(f"/loans/api/{created['id']}/disburse", json={}, headers=auth_header(admin_token))
        assert first.status_code == 200

        second = client.post(f"/loans/api/{created['id']}/disburse", json={}, headers=auth_header(admin_token))
        assert second.status_code == 400


class TestWriteOff:
    def test_write_off_active_loan(self, client, admin_token, approved_loan):
        resp = client.post(f"/loans/api/{approved_loan['id']}/write-off",
                            json={'reason': 'Borrower deceased'}, headers=auth_header(admin_token))
        assert resp.status_code == 200

        follow_up = client.get(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token))
        assert follow_up.get_json()['status'] == 'written_off'

    def test_cannot_write_off_pending_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        resp = client.post(f"/loans/api/{created['id']}/write-off", json={'reason': 'test'},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400


class TestRestructure:
    def test_restructure_requires_reason(self, client, admin_token, approved_loan):
        resp = client.post(f"/loans/api/{approved_loan['id']}/restructure",
                            json={'new_term': 12}, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_cannot_restructure_pending_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        resp = client.post(f"/loans/api/{created['id']}/restructure",
                            json={'reason': 'Lost job', 'new_term': 12},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_restructure_changes_term_rate_and_frequency(self, client, admin_token, approved_loan):
        original_term = approved_loan['term']
        original_rate = approved_loan['interest_rate']

        resp = client.post(f"/loans/api/{approved_loan['id']}/restructure", json={
            'reason': 'Borrower lost their job, requested lower repayments over a longer term',
            'new_term': 12,
            'new_interest_rate': 8,
            'new_repayment_frequency': 'monthly',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200, resp.get_data(as_text=True)

        loan = resp.get_json()['loan']
        assert loan['term'] == 12
        assert loan['term'] != original_term
        assert loan['interest_rate'] == 8
        assert loan['interest_rate'] != original_rate
        assert loan['status'] == 'active'  # restructuring doesn't change status

        # New schedule matches the new term
        schedule_resp = client.get(f"/loans/api/{approved_loan['id']}", headers=auth_header(admin_token))
        assert len(schedule_resp.get_json()['schedule']) == 12

    def test_restructure_preserves_history(self, client, admin_token, approved_loan):
        resp = client.post(f"/loans/api/{approved_loan['id']}/restructure", json={
            'reason': 'Seasonal income shortfall, needs smaller installments',
            'new_term': 10,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200

        history = client.get(f"/loans/api/{approved_loan['id']}/restructures",
                              headers=auth_header(admin_token))
        assert history.status_code == 200
        records = history.get_json()['restructures']
        assert len(records) == 1
        record = records[0]
        assert record['reason'] == 'Seasonal income shortfall, needs smaller installments'
        assert record['old_term'] == approved_loan['term']
        assert record['new_term'] == 10
        # The old schedule rows that got replaced are preserved verbatim
        assert len(record['old_schedule_snapshot']) == approved_loan['term']

    def test_restructure_rejects_invalid_interest_type(self, client, admin_token, approved_loan):
        resp = client.post(f"/loans/api/{approved_loan['id']}/restructure", json={
            'reason': 'Distress restructure',
            'new_interest_type': 'compound',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_restructure_nonexistent_loan_404s(self, client, admin_token):
        resp = client.post('/loans/api/999999/restructure', json={'reason': 'test'},
                            headers=auth_header(admin_token))
        assert resp.status_code == 404

