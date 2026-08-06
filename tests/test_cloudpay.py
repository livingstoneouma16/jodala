"""
CloudPay gateway tests -- mirrors test_mpesa.py's structure since CloudPay
is a switchable drop-in alternative to M-Pesa Daraja for STK Push (see
core/routes/mpesa.py:get_active_gateway()).

Covers:
1. `POST /mpesa/api/stkpush` routes to CloudPay instead of Daraja once
   `payment_gateway` is set to 'cloudpay', and the stored mpesa_transactions
   row is tagged with gateway='cloudpay'. The actual CloudPay HTTP call
   (core.cloudpay.initiate_stk_push) is mocked, same as the Daraja test does
   for core.mpesa.initiate_stk_push.
2. `POST /mpesa/cloudpay/callback` -- applies a confirmed payment via the
   same shared _apply_successful_stk_payment() helper the Daraja callback
   uses, so it's covered for: successful payment applies to the loan, a
   failed result doesn't touch the loan, an unknown checkout_request_id is
   ignored (not 500s), and a replayed callback only applies once.
3. The default gateway (no `payment_gateway` setting) is still 'mpesa', so
   existing M-Pesa-only installs are unaffected by this feature existing.

Run with: pytest tests/test_cloudpay.py -v
"""
from unittest.mock import patch

from conftest import auth_header


def _set_active_gateway(db_conn, gateway):
    existing = db_conn.execute(
        "SELECT id FROM company_settings WHERE key = %s", ('payment_gateway',)
    ).fetchone()
    if existing:
        db_conn.execute(
            "UPDATE company_settings SET value = %s WHERE key = %s", (gateway, 'payment_gateway')
        )
    else:
        db_conn.execute(
            "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, now()::text)",
            ('payment_gateway', gateway)
        )
    db_conn.commit()


def _cloudpay_success_body(checkout_request_id, amount=1000, receipt='CP-RECEIPT-1'):
    return {
        'checkout_request_id': checkout_request_id,
        'result_code': 0,
        'result_desc': 'Success',
        'receipt_number': receipt,
        'amount': amount,
    }


def _cloudpay_failure_body(checkout_request_id, result_code=1, desc='Request cancelled by user'):
    return {
        'checkout_request_id': checkout_request_id,
        'result_code': result_code,
        'result_desc': desc,
    }


class TestGatewaySelection:
    def test_default_gateway_is_mpesa(self, client, admin_token, approved_loan):
        # No payment_gateway setting has been written -- stkpush must still
        # go to Daraja, not CloudPay, so existing installs are unaffected.
        with patch('core.routes.mpesa.initiate_stk_push') as mock_mpesa, \
             patch('core.routes.mpesa.cloudpay_initiate_stk_push') as mock_cloudpay:
            mock_mpesa.return_value = {'CheckoutRequestID': 'ws_CO_default', 'MerchantRequestID': 'mr-1'}
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()['gateway'] == 'mpesa'
        mock_mpesa.assert_called_once()
        mock_cloudpay.assert_not_called()

    def test_stkpush_routes_to_cloudpay_when_active(self, client, db_conn, admin_token, approved_loan):
        _set_active_gateway(db_conn, 'cloudpay')
        with patch('core.routes.mpesa.cloudpay_initiate_stk_push') as mock_cloudpay, \
             patch('core.routes.mpesa.initiate_stk_push') as mock_mpesa:
            mock_cloudpay.return_value = {
                'checkout_request_id': 'cp_CO_test123', 'merchant_request_id': 'cp-mr-1',
            }
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['gateway'] == 'cloudpay'
        assert body['checkout_request_id'] == 'cp_CO_test123'
        mock_cloudpay.assert_called_once()
        mock_mpesa.assert_not_called()

        txn = db_conn.execute(
            "SELECT gateway FROM mpesa_transactions WHERE checkout_request_id = %s", ('cp_CO_test123',)
        ).fetchone()
        assert txn['gateway'] == 'cloudpay'

    def test_unrecognized_gateway_value_falls_back_to_mpesa(self, client, db_conn, admin_token, approved_loan):
        _set_active_gateway(db_conn, 'some_future_gateway')
        with patch('core.routes.mpesa.initiate_stk_push') as mock_mpesa:
            mock_mpesa.return_value = {'CheckoutRequestID': 'ws_CO_fallback', 'MerchantRequestID': 'mr-1'}
            resp = client.post('/mpesa/api/stkpush', json={
                'purpose': 'loan_repayment', 'target_id': approved_loan['id'],
                'phone': '0712345678', 'amount': 1000,
            }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.get_json()['gateway'] == 'mpesa'


class TestCloudPayCallback:
    def _seed_pending_transaction(self, db_conn, loan_id, checkout_request_id='cp_CO_abc', amount=1000):
        admin_id = db_conn.execute("SELECT id FROM users WHERE username = %s", ('admin',)).fetchone()['id']
        db_conn.execute(
            """INSERT INTO mpesa_transactions (checkout_request_id, merchant_request_id, purpose,
                   target_id, phone, amount, status, initiated_by, gateway, created_at, updated_at)
               VALUES (%s, %s, 'loan_repayment', %s, %s, %s, 'pending', %s, 'cloudpay', now()::text, now()::text)""",
            (checkout_request_id, 'cp-mr-1', loan_id, '254712345678', amount, admin_id)
        )
        db_conn.commit()

    def test_successful_callback_applies_repayment_to_loan(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'cp_CO_ok', amount=1000)

        resp = client.post('/mpesa/cloudpay/callback', json=_cloudpay_success_body('cp_CO_ok', amount=1000))
        assert resp.status_code == 200
        assert resp.get_json()['result_code'] == 0

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == round(starting_balance - 1000, 2)

        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('cp_CO_ok',)
        ).fetchone()
        assert txn['status'] == 'success'
        assert txn['mpesa_receipt_number'] == 'CP-RECEIPT-1'
        assert txn['gateway'] == 'cloudpay'

    def test_failed_callback_does_not_touch_loan_balance(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'cp_CO_fail')

        resp = client.post('/mpesa/cloudpay/callback', json=_cloudpay_failure_body('cp_CO_fail'))
        assert resp.status_code == 200  # always 200 so CloudPay doesn't retry forever

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == starting_balance

        txn = db_conn.execute(
            "SELECT status FROM mpesa_transactions WHERE checkout_request_id = %s", ('cp_CO_fail',)
        ).fetchone()
        assert txn['status'] == 'failed'

    def test_unknown_checkout_request_id_ignored_gracefully(self, client):
        resp = client.post('/mpesa/cloudpay/callback', json=_cloudpay_success_body('cp_CO_never_seen'))
        assert resp.status_code == 200
        assert resp.get_json()['result_code'] == 0

    def test_malformed_callback_body_does_not_500(self, client):
        resp = client.post('/mpesa/cloudpay/callback', json={'nonsense': True})
        assert resp.status_code == 200

    def test_replayed_callback_does_not_double_apply(self, client, db_conn, approved_loan):
        starting_balance = approved_loan['outstanding_balance']
        self._seed_pending_transaction(db_conn, approved_loan['id'], 'cp_CO_replay', amount=1000)

        first = client.post('/mpesa/cloudpay/callback', json=_cloudpay_success_body('cp_CO_replay', amount=1000))
        assert first.status_code == 200
        # Same reasoning as the Daraja test -- a callback delivered twice
        # must be a no-op the second time, not a double deduction.
        second = client.post('/mpesa/cloudpay/callback', json=_cloudpay_success_body('cp_CO_replay', amount=1000))
        assert second.status_code == 200

        loan = db_conn.execute(
            "SELECT outstanding_balance FROM loans WHERE id = %s", (approved_loan['id'],)
        ).fetchone()
        assert loan['outstanding_balance'] == round(starting_balance - 1000, 2)

    def test_savings_deposit_purpose_applies_to_savings_account(self, client, db_conn, admin_token, member):
        # Sanity check that the shared _apply_successful_stk_payment()
        # helper still branches correctly on purpose for the CloudPay path,
        # same as it does for M-Pesa. Seed the product/account directly via
        # db_conn (mirroring how loan_product/member fixtures build rows)
        # rather than depending on the exact shape of the savings-product
        # creation route, which isn't exercised elsewhere in this file.
        db_conn.execute(
            """INSERT INTO savings_products (name, code, interest_rate, min_balance, is_active, created_at)
               VALUES (%s, %s, %s, %s, %s, now()::text)""",
            ('Regular Savings CP Test', f'SAV-{member["id"]}-cp', 0, 0, 1)
        )
        db_conn.commit()
        product = db_conn.execute(
            "SELECT id FROM savings_products WHERE code = %s", (f'SAV-{member["id"]}-cp',)
        ).fetchone()

        db_conn.execute(
            """INSERT INTO savings_accounts (account_number, member_id, product_id, balance, status, opened_at)
               VALUES (%s, %s, %s, 0, 'active', now()::text)""",
            (f'SA-{member["id"]}-cp', member['id'], product['id'])
        )
        db_conn.commit()
        account = db_conn.execute(
            "SELECT id FROM savings_accounts WHERE account_number = %s", (f'SA-{member["id"]}-cp',)
        ).fetchone()

        admin_id = db_conn.execute("SELECT id FROM users WHERE username = %s", ('admin',)).fetchone()['id']
        db_conn.execute(
            """INSERT INTO mpesa_transactions (checkout_request_id, merchant_request_id, purpose,
                   target_id, phone, amount, status, initiated_by, gateway, created_at, updated_at)
               VALUES (%s, %s, 'savings_deposit', %s, %s, %s, 'pending', %s, 'cloudpay', now()::text, now()::text)""",
            ('cp_CO_savings', 'cp-mr-2', account['id'], '254712345678', 500, admin_id)
        )
        db_conn.commit()

        resp = client.post('/mpesa/cloudpay/callback', json=_cloudpay_success_body('cp_CO_savings', amount=500))
        assert resp.status_code == 200
        txn = db_conn.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", ('cp_CO_savings',)
        ).fetchone()
        assert txn['status'] == 'success'
        assert txn['savings_transaction_id'] is not None

        acct = db_conn.execute("SELECT balance FROM savings_accounts WHERE id = %s", (account['id'],)).fetchone()
        assert acct['balance'] == 500
