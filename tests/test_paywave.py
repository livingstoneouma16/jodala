"""
Paywave Express STK Push gateway tests -- mirrors tests/test_mpesa.py's
approach: the outbound HTTP call to Paywave Express is mocked (no live
account needed), while our own request validation, gateway selection, and
webhook handling are exercised for real.

Run with: pytest tests/test_paywave.py -v
"""
from unittest.mock import patch

from conftest import auth_header


def _set_setting(db_conn, key, value):
    existing = db_conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
    if existing:
        db_conn.execute("UPDATE company_settings SET value = %s WHERE key = %s", (value, key))
    else:
        db_conn.execute(
            "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, now()::text)",
            (key, value)
        )
    db_conn.commit()


class TestPaywaveStkPushInitiation:
    def test_stkpush_uses_paywave_when_selected_as_active_gateway(self, client, db_conn, admin_token, approved_loan):
        _set_setting(db_conn, 'payment_gateway', 'paywave')
        with patch('core.routes.mpesa.paywave_initiate_stk_push') as mock_push, \
                patch('core.routes.mpesa.initiate_stk_push') as mock_mpesa_push:
            mock_push.return_value = {
                'ResponseCode': '0',
                'transaction_request_id': 'FCID20260220153045123456',
                'MerchantRequestID': 'mr-1',
                'CheckoutRequestID': 'ws_CO_pw1',
            }
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['gateway'] == 'paywave'
        assert body['checkout_request_id'] == 'FCID20260220153045123456'
        mock_push.assert_called_once()
        mock_mpesa_push.assert_not_called()

    def test_stkpush_uses_mpesa_by_default(self, client, db_conn, admin_token, approved_loan):
        _set_setting(db_conn, 'payment_gateway', 'mpesa')
        with patch('core.routes.mpesa.paywave_initiate_stk_push') as mock_paywave_push, \
                patch('core.routes.mpesa.initiate_stk_push') as mock_push:
            mock_push.return_value = {'CheckoutRequestID': 'ws_CO_m1', 'MerchantRequestID': 'mr-1'}
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()['gateway'] == 'mpesa'
        mock_paywave_push.assert_not_called()


class TestPaywaveWebhook:
    def _seed_pending_transaction(self, db_conn, loan_id, transaction_id='FCID123', amount=1000):
        admin_id = db_conn.execute("SELECT id FROM users WHERE username = %s", ('admin',)).fetchone()['id']
        db_conn.execute(
            """INSERT INTO mpesa_transactions (checkout_request_id, merchant_request_id, purpose,
                   target_id, phone, amount, status, initiated_by, gateway, created_at, updated_at)
               VALUES (%s, %s, 'loan_repayment', %s, %s, %s, 'pending', %s, 'paywave', now()::text, now()::text)""",
            (transaction_id, 'mr-1', loan_id, '254712345678', amount, admin_id)
        )
        db_conn.commit()

    def test_successful_webhook_applies_repayment_to_loan(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'FCID_ok', amount=1000)

        resp = client.post('/mpesa/paywave-webhook', json={
            'ResponseCode': 0, 'ResponseDescription': 'Success',
            'MerchantRequestID': 'mr-1', 'CheckoutRequestID': 'ws_CO_pw1',
            'TransactionID': 'FCID_ok', 'TransactionAmount': 1000,
            'TransactionReceipt': 'SHJ7ABCDEF', 'TransactionDate': '20260220153045',
            'TransactionReference': approved_loan['loan_number'], 'Msisdn': '254712345678',
        })
        assert resp.status_code == 200

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('FCID_ok',)
        ).fetchone()
        assert txn['status'] == 'success'
        assert txn['mpesa_receipt_number'] == 'SHJ7ABCDEF'

        loan = db_conn.execute("SELECT * FROM loans WHERE id = %s", (approved_loan['id'],)).fetchone()
        assert loan['outstanding_balance'] < starting_balance

    def test_failed_webhook_marks_failed_without_touching_loan(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'FCID_cancel', amount=1000)

        resp = client.post('/mpesa/paywave-webhook', json={
            'ResponseCode': 1032, 'ResponseDescription': 'Request cancelled by user',
            'TransactionID': 'FCID_cancel',
        })
        assert resp.status_code == 200

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('FCID_cancel',)
        ).fetchone()
        assert txn['status'] == 'failed'

        loan = db_conn.execute("SELECT * FROM loans WHERE id = %s", (approved_loan['id'],)).fetchone()
        assert loan['outstanding_balance'] == starting_balance

    def test_unknown_transaction_id_is_ignored_not_500(self, client):
        resp = client.post('/mpesa/paywave-webhook', json={
            'ResponseCode': 0, 'TransactionID': 'does-not-exist', 'TransactionAmount': 1000,
        })
        assert resp.status_code == 200

    def test_webhook_replayed_twice_only_applies_once(self, client, db_conn, approved_loan):
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'FCID_twice', amount=1000)
        payload = {
            'ResponseCode': 0, 'ResponseDescription': 'Success', 'TransactionID': 'FCID_twice',
            'TransactionAmount': 1000, 'TransactionReceipt': 'SHJ7XYZ', 'Msisdn': '254712345678',
        }
        r1 = client.post('/mpesa/paywave-webhook', json=payload)
        r2 = client.post('/mpesa/paywave-webhook', json=payload)
        assert r1.status_code == 200 and r2.status_code == 200

        repayments = db_conn.execute(
            "SELECT COUNT(*) AS c FROM repayments WHERE reference_number = %s", ('SHJ7XYZ',)
        ).fetchone()
        assert repayments['c'] == 1

    def test_amount_mismatch_is_not_applied(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'FCID_mismatch', amount=1000)

        resp = client.post('/mpesa/paywave-webhook', json={
            'ResponseCode': 0, 'TransactionID': 'FCID_mismatch',
            'TransactionAmount': 1, 'TransactionReceipt': 'BADAMT', 'Msisdn': '254712345678',
        })
        assert resp.status_code == 200

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('FCID_mismatch',)
        ).fetchone()
        assert txn['status'] == 'failed'
        loan = db_conn.execute("SELECT * FROM loans WHERE id = %s", (approved_loan['id'],)).fetchone()
        assert loan['outstanding_balance'] == starting_balance
