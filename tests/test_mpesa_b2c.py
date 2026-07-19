"""
M-Pesa B2C (disbursement) flow tests -- money moving OUT of the SACCO to a
member's phone, the mirror image of the STK push flow in test_mpesa.py.
Same reasoning applies: the important half is the result/timeout callback,
since that's what actually flips an approved loan to disbursed and builds
its repayment schedule. The outbound Safaricom HTTP call
(core.mpesa.initiate_b2c_payment) is mocked throughout.

Run with: pytest tests/test_mpesa_b2c.py -v
"""
from unittest.mock import patch

from conftest import auth_header


def _b2c_result_body(originator_conversation_id, result_code=0, result_desc='The service request is processed successfully.',
                      transaction_id='NLJ7RT61SV'):
    body = {
        'Result': {
            'ResultType': 0,
            'ResultCode': result_code,
            'ResultDesc': result_desc,
            'OriginatorConversationID': originator_conversation_id,
            'ConversationID': 'AG_20240101_00001',
            'TransactionID': transaction_id,
        }
    }
    if result_code == 0:
        body['Result']['ResultParameters'] = {
            'ResultParameter': [
                {'Key': 'TransactionAmount', 'Value': 10000},
                {'Key': 'TransactionReceipt', 'Value': transaction_id},
                {'Key': 'ReceiverPartyPublicName', 'Value': '254712345678 - Jane Wanjiru'},
            ]
        }
    return body


class TestB2CInitiation:
    def test_initiate_b2c_for_approved_loan(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']
        client.post(f"/loans/api/{created['id']}/approve", json={}, headers=auth_header(admin_token))

        with patch('core.routes.mpesa.initiate_b2c_payment') as mock_b2c:
            mock_b2c.return_value = {
                'OriginatorConversationID': 'oc-test-1', 'ConversationID': 'AG_test_1',
            }
            resp = client.post('/mpesa/api/b2c', json={
                'loan_id': created['id'], 'phone': '0712345678',
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()['originator_conversation_id'] == 'oc-test-1'
        mock_b2c.assert_called_once()

    def test_b2c_against_pending_loan_rejected(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        # Never approved -- must not be disbursable via M-Pesa either.
        with patch('core.routes.mpesa.initiate_b2c_payment') as mock_b2c:
            resp = client.post('/mpesa/api/b2c', json={
                'loan_id': created['id'], 'phone': '0712345678',
            }, headers=auth_header(admin_token))
        assert resp.status_code == 400
        mock_b2c.assert_not_called()

    def test_b2c_against_already_disbursed_loan_rejected(self, client, admin_token, approved_loan):
        # approved_loan fixture already disbursed it via cash -- a second
        # disbursement (by any method) must be blocked.
        with patch('core.routes.mpesa.initiate_b2c_payment') as mock_b2c:
            resp = client.post('/mpesa/api/b2c', json={
                'loan_id': approved_loan['id'], 'phone': '0712345678',
            }, headers=auth_header(admin_token))
        assert resp.status_code == 400
        mock_b2c.assert_not_called()

    def test_b2c_missing_fields_rejected(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']
        client.post(f"/loans/api/{created['id']}/approve", json={}, headers=auth_header(admin_token))

        resp = client.post('/mpesa/api/b2c', json={'loan_id': created['id']},
                            headers=auth_header(admin_token))
        assert resp.status_code == 400


class TestB2CResultCallback:
    def _seed_pending_b2c(self, db_conn, loan_id, originator_conversation_id='oc-abc'):
        admin_id = db_conn.execute("SELECT id FROM users WHERE username = %s", ('admin',)).fetchone()['id']
        db_conn.execute(
            """INSERT INTO mpesa_transactions (originator_conversation_id, conversation_id, purpose,
                   target_id, phone, amount, status, initiated_by, created_at, updated_at)
               VALUES (%s, %s, 'loan_disbursement', %s, %s, %s, 'pending', %s, now()::text, now()::text)""",
            (originator_conversation_id, 'AG_test', loan_id, '254712345678', 10000, admin_id)
        )
        db_conn.commit()

    def _approved_unfinanced_loan(self, client, admin_token, member, loan_product):
        """An approved-but-not-yet-disbursed loan -- what a pending B2C
        transaction is actually waiting on."""
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']
        client.post(f"/loans/api/{created['id']}/approve", json={}, headers=auth_header(admin_token))
        return created

    def test_successful_result_disburses_loan(self, client, db_conn, admin_token, member, loan_product):
        loan = self._approved_unfinanced_loan(client, admin_token, member, loan_product)
        self._seed_pending_b2c(db_conn, loan['id'], 'oc-ok')

        resp = client.post('/mpesa/b2c/result', json=_b2c_result_body('oc-ok'))
        assert resp.status_code == 200
        assert resp.get_json()['ResultCode'] == 0

        follow_up = db_conn.execute("SELECT status, outstanding_balance FROM loans WHERE id = %s",
                                     (loan['id'],)).fetchone()
        assert follow_up['status'] == 'active'
        assert follow_up['outstanding_balance'] > 0

        schedule = db_conn.execute("SELECT * FROM loan_schedules WHERE loan_id = %s", (loan['id'],)).fetchall()
        assert len(schedule) == 6

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE originator_conversation_id = %s", ('oc-ok',)
        ).fetchone()
        assert txn['status'] == 'success'
        assert txn['transaction_id'] == 'NLJ7RT61SV'

    def test_failed_result_leaves_loan_approved_not_disbursed(self, client, db_conn, admin_token, member, loan_product):
        loan = self._approved_unfinanced_loan(client, admin_token, member, loan_product)
        self._seed_pending_b2c(db_conn, loan['id'], 'oc-fail')

        resp = client.post('/mpesa/b2c/result', json=_b2c_result_body(
            'oc-fail', result_code=2001, result_desc='The initiator information is invalid.'
        ))
        assert resp.status_code == 200

        follow_up = db_conn.execute("SELECT status FROM loans WHERE id = %s", (loan['id'],)).fetchone()
        # Must still be 'approved', not disbursed and not stuck/errored --
        # staff can retry B2C or fall back to cash/bank.
        assert follow_up['status'] == 'approved'

        txn = db_conn.execute(
            "SELECT status FROM mpesa_transactions WHERE originator_conversation_id = %s", ('oc-fail',)
        ).fetchone()
        assert txn['status'] == 'failed'

    def test_timeout_callback_treated_same_as_failure(self, client, db_conn, admin_token, member, loan_product):
        loan = self._approved_unfinanced_loan(client, admin_token, member, loan_product)
        self._seed_pending_b2c(db_conn, loan['id'], 'oc-timeout')

        resp = client.post('/mpesa/b2c/timeout', json=_b2c_result_body(
            'oc-timeout', result_code=1, result_desc='Timeout waiting for response.'
        ))
        assert resp.status_code == 200

        follow_up = db_conn.execute("SELECT status FROM loans WHERE id = %s", (loan['id'],)).fetchone()
        assert follow_up['status'] == 'approved'

    def test_replayed_result_does_not_disburse_twice(self, client, db_conn, admin_token, member, loan_product):
        loan = self._approved_unfinanced_loan(client, admin_token, member, loan_product)
        self._seed_pending_b2c(db_conn, loan['id'], 'oc-replay')

        first = client.post('/mpesa/b2c/result', json=_b2c_result_body('oc-replay'))
        assert first.status_code == 200
        second = client.post('/mpesa/b2c/result', json=_b2c_result_body('oc-replay'))
        assert second.status_code == 200

        schedule = db_conn.execute("SELECT * FROM loan_schedules WHERE loan_id = %s", (loan['id'],)).fetchall()
        # A second disbursement would either error or double the schedule
        # rows -- neither happened, so exactly one schedule's worth exists.
        assert len(schedule) == 6

    def test_unknown_originator_conversation_id_ignored_gracefully(self, client):
        resp = client.post('/mpesa/b2c/result', json=_b2c_result_body('oc-never-seen'))
        assert resp.status_code == 200
        assert resp.get_json()['ResultCode'] == 0

    def test_malformed_result_body_does_not_500(self, client):
        resp = client.post('/mpesa/b2c/result', json={'nonsense': True})
        assert resp.status_code == 200
