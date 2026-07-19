"""
M-Pesa flow tests. Two things are exercised here rather than assumed:

1. `POST /mpesa/api/stkpush` -- validates target + writes a 'pending'
   mpesa_transactions row. The actual Safaricom HTTP call
   (core.mpesa.initiate_stk_push) is mocked; testing our own request/
   validation logic doesn't require a live Daraja sandbox call.
2. `POST /mpesa/callback` -- the important half of this flow, since this is
   where real money actually gets applied to a loan or savings account, and
   it runs with no login (Safaricom calls it directly). Covers: successful
   payment applies via the same _record_repayment path a manual entry
   uses, a failed STK result doesn't touch the loan, an unknown
   CheckoutRequestID is ignored (not 500s), and a callback replayed twice
   only applies once.

Run with: pytest tests/test_mpesa.py -v
"""
from unittest.mock import patch

from conftest import auth_header


def _stk_success_body(checkout_request_id, amount=1000, receipt='NLJ7RT61SV'):
    return {
        'Body': {
            'stkCallback': {
                'CheckoutRequestID': checkout_request_id,
                'MerchantRequestID': 'mr-1',
                'ResultCode': 0,
                'ResultDesc': 'The service request is processed successfully.',
                'CallbackMetadata': {
                    'Item': [
                        {'Name': 'Amount', 'Value': amount},
                        {'Name': 'MpesaReceiptNumber', 'Value': receipt},
                        {'Name': 'PhoneNumber', 'Value': 254712345678},
                    ]
                }
            }
        }
    }


def _stk_failure_body(checkout_request_id, result_code=1032, desc='Request cancelled by user'):
    return {
        'Body': {
            'stkCallback': {
                'CheckoutRequestID': checkout_request_id,
                'MerchantRequestID': 'mr-1',
                'ResultCode': result_code,
                'ResultDesc': desc,
            }
        }
    }


class TestStkPushInitiation:
    def test_initiate_stk_push_for_active_loan(self, client, admin_token, approved_loan):
        with patch('core.routes.mpesa.initiate_stk_push') as mock_push:
            mock_push.return_value = {
                'CheckoutRequestID': 'ws_CO_test123', 'MerchantRequestID': 'mr-1',
            }
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment',
                'target_id': approved_loan['id'],
                'phone': '0712345678',
                'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()['checkout_request_id'] == 'ws_CO_test123'
        mock_push.assert_called_once()

    def test_stk_push_against_pending_loan_rejected(self, client, admin_token, member, loan_product):
        created = client.post('/loans/api', json={
            'member_id': member['id'], 'borrower_type': 'member',
            'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
        }, headers=auth_header(admin_token)).get_json()['loan']

        with patch('core.routes.mpesa.initiate_stk_push') as mock_push:
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': created['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 400
        mock_push.assert_not_called()

    def test_stk_push_invalid_purpose_rejected(self, client, admin_token, approved_loan):
        resp = client.post('/mpesa/api/stkpush', json={
            'purpose': 'something_else', 'target_id': approved_loan['id'],
            'phone': '0712345678', 'amount': 1000,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_stk_push_missing_fields_rejected(self, client, admin_token, approved_loan):
        resp = client.post('/mpesa/api/stkpush', json={
            'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400


class TestStkCallback:
    def _seed_pending_transaction(self, db_conn, loan_id, checkout_request_id='ws_CO_abc', amount=1000):
        admin_id = db_conn.execute("SELECT id FROM users WHERE username = %s", ('admin',)).fetchone()['id']
        db_conn.execute(
            """INSERT INTO mpesa_transactions (checkout_request_id, merchant_request_id, purpose,
                   target_id, phone, amount, status, initiated_by, created_at, updated_at)
               VALUES (%s, %s, 'loan_repayment', %s, %s, %s, 'pending', %s, now()::text, now()::text)""",
            (checkout_request_id, 'mr-1', loan_id, '254712345678', amount, admin_id)
        )
        db_conn.commit()

    def test_successful_callback_applies_repayment_to_loan(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'ws_CO_ok', amount=1000)

        resp = client.post('/mpesa/callback', json=_stk_success_body('ws_CO_ok', amount=1000))
        assert resp.status_code == 200
        assert resp.get_json()['ResultCode'] == 0

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == round(starting_balance - 1000, 2)

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('ws_CO_ok',)
        ).fetchone()
        assert txn['status'] == 'success'
        assert txn['mpesa_receipt_number'] == 'NLJ7RT61SV'

    def test_failed_callback_does_not_touch_loan_balance(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'ws_CO_fail')

        resp = client.post('/mpesa/callback', json=_stk_failure_body('ws_CO_fail'))
        assert resp.status_code == 200  # always 200 so Safaricom doesn't retry forever

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == starting_balance

        txn = db_conn.execute(
            "SELECT status FROM mpesa_transactions WHERE checkout_request_id = %s", ('ws_CO_fail',)
        ).fetchone()
        assert txn['status'] == 'failed'

    def test_unknown_checkout_request_id_ignored_gracefully(self, client):
        resp = client.post('/mpesa/callback', json=_stk_success_body('ws_CO_never_seen'))
        assert resp.status_code == 200
        assert resp.get_json()['ResultCode'] == 0

    def test_malformed_callback_body_does_not_500(self, client):
        resp = client.post('/mpesa/callback', json={'nonsense': True})
        assert resp.status_code == 200

    def test_replayed_callback_does_not_double_apply(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'ws_CO_replay', amount=1000)

        first = client.post('/mpesa/callback', json=_stk_success_body('ws_CO_replay', amount=1000))
        assert first.status_code == 200
        # Safaricom is documented to sometimes deliver the same callback
        # more than once -- a second delivery must be a no-op, not a
        # second deduction from the loan.
        second = client.post('/mpesa/callback', json=_stk_success_body('ws_CO_replay', amount=1000))
        assert second.status_code == 200

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == round(starting_balance - 1000, 2)
