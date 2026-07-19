"""
Auth flow tests: login (correct/incorrect credentials, deactivated
account), forced password change gating, and JWT-protected route access.

Run with: pytest tests/test_auth.py -v
"""
from conftest import auth_header


class TestLogin:
    def test_login_wrong_password_rejected(self, client):
        resp = client.post('/auth/login', json={'username': 'admin', 'password': 'wrong'})
        assert resp.status_code == 401
        assert 'error' in resp.get_json()

    def test_login_unknown_user_rejected(self, client):
        resp = client.post('/auth/login', json={'username': 'nobody', 'password': 'whatever'})
        assert resp.status_code == 401

    def test_login_correct_credentials_returns_token_and_forces_password_change(self, client):
        resp = client.post('/auth/login', json={'username': 'admin', 'password': 'ChangeMe123!'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['access_token']
        assert body['must_change_password'] is True
        assert body['user']['username'] == 'admin'
        # The password hash must never be echoed back to the client.
        assert 'password_hash' not in body['user']

    def test_deactivated_account_cannot_login(self, client, db_conn):
        db_conn.execute(
            """INSERT INTO users (username, email, password_hash, full_name, role,
                   is_active, must_change_password, totp_enabled, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, 0, 0, 0, now()::text, now()::text)""",
            ('deactivated_user', 'deact@example.com',
             __import__('core.auth', fromlist=['hash_password']).hash_password('Whatever123!'),
             'Deactivated User', 'cashier')
        )
        db_conn.commit()
        resp = client.post('/auth/login', json={'username': 'deactivated_user', 'password': 'Whatever123!'})
        assert resp.status_code == 403
        assert 'deactivated' in resp.get_json()['error'].lower()


class TestForcedPasswordChange:
    def test_must_change_password_blocks_other_routes(self, client):
        login = client.post('/auth/login', json={'username': 'admin', 'password': 'ChangeMe123!'})
        token = login.get_json()['access_token']

        # Any ordinary API endpoint should be refused while a forced
        # password change is outstanding -- otherwise the "must change"
        # flag is purely cosmetic.
        resp = client.get('/members/api', headers=auth_header(token))
        assert resp.status_code == 403

    def test_wrong_current_password_rejected(self, client):
        login = client.post('/auth/login', json={'username': 'admin', 'password': 'ChangeMe123!'})
        token = login.get_json()['access_token']

        resp = client.post('/auth/change-password-required', json={
            'current_password': 'not-the-real-one',
            'new_password': 'NewPassword123!',
            'confirm_password': 'NewPassword123!',
        }, headers=auth_header(token))
        assert resp.status_code == 400

    def test_successful_change_unblocks_account(self, admin_token, client):
        # admin_token fixture already drove the full change -> re-login
        # flow; if we got a token back at all, the account is unblocked.
        resp = client.get('/members/api', headers=auth_header(admin_token))
        assert resp.status_code == 200


class TestProtectedRoutes:
    def test_no_token_rejected(self, client):
        resp = client.get('/members/api')
        assert resp.status_code == 401

    def test_garbage_token_rejected(self, client):
        resp = client.get('/members/api', headers=auth_header('not-a-real-jwt'))
        assert resp.status_code == 401

    def test_valid_token_accepted(self, client, admin_token):
        resp = client.get('/members/api', headers=auth_header(admin_token))
        assert resp.status_code == 200
