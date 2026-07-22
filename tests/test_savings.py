"""
Savings tests: opening an account, deposit/withdrawal balance math, and the
two guards that stop an account going where it shouldn't (overdraft, and
withdrawing below a product's minimum balance).

Run with: pytest tests/test_savings.py -v
"""
from conftest import auth_header


def _open_savings_product(db_conn, min_balance=0):
    cur = db_conn.execute(
        """INSERT INTO savings_products (name, code, interest_rate, min_balance, is_active, created_at)
           VALUES (%s, %s, %s, %s, %s, now()::text)""",
        ('Ordinary Savings', f'ORD-{min_balance}', 5.0, min_balance, 1)
    )
    db_conn.commit()
    return db_conn.execute(
        "SELECT * FROM savings_products WHERE id = %s", (cur.lastrowid,)
    ).fetchone()


class TestOpenAccount:
    def test_open_account_with_initial_deposit(self, client, admin_token, member, db_conn):
        product = _open_savings_product(db_conn)
        resp = client.post('/savings/api/accounts', json={
            'member_id': member['id'],
            'product_id': product['id'],
            'initial_deposit': 500,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        account = resp.get_json()['account']
        assert account['balance'] == 500
        assert account['status'] == 'active'

    def test_open_account_without_initial_deposit_starts_at_zero(self, client, admin_token, member, db_conn):
        product = _open_savings_product(db_conn)
        resp = client.post('/savings/api/accounts', json={
            'member_id': member['id'], 'product_id': product['id'],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.get_json()['account']['balance'] == 0

    def test_duplicate_account_for_same_member_rejected(self, client, admin_token, member, db_conn):
        product = _open_savings_product(db_conn)
        client.post('/savings/api/accounts', json={
            'member_id': member['id'], 'product_id': product['id'],
        }, headers=auth_header(admin_token))

        resp = client.post('/savings/api/accounts', json={
            'member_id': member['id'], 'product_id': product['id'],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_invalid_product_rejected(self, client, admin_token, member):
        resp = client.post('/savings/api/accounts', json={
            'member_id': member['id'], 'product_id': 999999,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

    def test_invalid_member_rejected(self, client, admin_token, db_conn):
        product = _open_savings_product(db_conn)
        resp = client.post('/savings/api/accounts', json={
            'member_id': 999999, 'product_id': product['id'],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 404


class TestDepositWithdraw:
    def _open(self, client, admin_token, member, db_conn, min_balance=0, initial=1000):
        product = _open_savings_product(db_conn, min_balance=min_balance)
        resp = client.post('/savings/api/accounts', json={
            'member_id': member['id'], 'product_id': product['id'], 'initial_deposit': initial,
        }, headers=auth_header(admin_token))
        return resp.get_json()['account']

    def test_deposit_increases_balance(self, client, admin_token, member, db_conn):
        account = self._open(client, admin_token, member, db_conn)
        resp = client.post('/savings/api/deposit', json={
            'account_id': account['id'], 'amount': 250, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.get_json()['balance'] == 1250

    def test_withdrawal_decreases_balance(self, client, admin_token, member, db_conn):
        account = self._open(client, admin_token, member, db_conn)
        resp = client.post('/savings/api/withdraw', json={
            'account_id': account['id'], 'amount': 300, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.get_json()['balance'] == 700

    def test_withdrawal_exceeding_balance_rejected(self, client, admin_token, member, db_conn):
        account = self._open(client, admin_token, member, db_conn, initial=100)
        resp = client.post('/savings/api/withdraw', json={
            'account_id': account['id'], 'amount': 500, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400

        # Balance must be unchanged after a rejected withdrawal.
        follow_up = client.get(f"/savings/api/accounts/{account['id']}", headers=auth_header(admin_token))
        assert follow_up.get_json()['balance'] == 100

    def test_withdrawal_below_product_minimum_balance_rejected(self, client, admin_token, member, db_conn):
        account = self._open(client, admin_token, member, db_conn, min_balance=200, initial=1000)
        # Leaves 150 behind, below the 200 minimum -- must be blocked.
        resp = client.post('/savings/api/withdraw', json={
            'account_id': account['id'], 'amount': 850, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 400
        assert 'minimum balance' in resp.get_json()['error'].lower()

    def test_withdrawal_exactly_at_minimum_balance_allowed(self, client, admin_token, member, db_conn):
        account = self._open(client, admin_token, member, db_conn, min_balance=200, initial=1000)
        resp = client.post('/savings/api/withdraw', json={
            'account_id': account['id'], 'amount': 800, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.get_json()['balance'] == 200

    def test_deposit_to_nonexistent_account_404s(self, client, admin_token):
        resp = client.post('/savings/api/deposit', json={
            'account_id': 999999, 'amount': 100, 'payment_method': 'cash',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 404
