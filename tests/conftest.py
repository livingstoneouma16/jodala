"""
Shared fixtures for the route-level test suite (auth, loans, repayments,
savings, m-pesa).

Unlike test_calculator.py (pure functions, no DB needed), these tests exercise
real route handlers against a real Postgres database -- the money-critical
bugs in this app live in SQL and transaction boundaries that an in-memory
fake can't faithfully reproduce, so we don't try to fake the DB layer.

DB strategy: `pytest-postgresql` spins up (or reuses, per its own caching) a
throwaway PostgreSQL instance for the test session -- no manual `createdb`,
no Docker, no dev DB reuse. `core.database.init_db()` runs the app's own
real migrations against it, so the schema tested is exactly the schema
production runs, not a hand-maintained copy of it.

Isolation strategy: each test runs inside one outer transaction that's
always rolled back at teardown (`db_conn` fixture), and the app is
monkeypatched to reuse that same connection for the duration of the test
(rather than opening its own per-request connection), so route code and
test-assertion code see the same uncommitted state and nothing a test does
ever persists to the next test.

Run with:
    pip install -r requirements-dev.txt
    pytest
"""
import os
import sys
import uuid
from datetime import date

import pytest
from pytest_postgresql import factories
from pytest_postgresql.janitor import DatabaseJanitor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')

os.environ.setdefault('JWT_SECRET_KEY', 'test-secret-do-not-use-in-prod')
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')
os.environ.setdefault('ENABLE_OVERDUE_SCHEDULER', 'false')
# Must be set before create_app() runs (see core/__init__.py) so
# flask-limiter's RATELIMIT_ENABLED is picked up as False at init_app time --
# setting app.config['TESTING'] afterwards is too late for flask-limiter to
# see it.
os.environ.setdefault('TESTING', '1')

postgresql_proc = factories.postgresql_proc()
postgresql = factories.postgresql('postgresql_proc')


@pytest.fixture(scope='session')
def database_url(postgresql_proc):
    """A DATABASE_URL pointing at the throwaway instance pytest-postgresql
    manages for the whole test session. Schema is created once here
    (session scope) since migrations are the slow part; per-test isolation
    is handled separately by db_conn's transaction rollback."""
    janitor = DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        dbname='jodala_test',
        version=postgresql_proc.version,
        password=postgresql_proc.password,
    )
    janitor.init()
    url = (f"postgresql://{postgresql_proc.user}@{postgresql_proc.host}:"
           f"{postgresql_proc.port}/jodala_test")
    yield url
    janitor.drop()


@pytest.fixture(scope='session')
def app(database_url):
    os.environ['DATABASE_URL'] = database_url

    from core import create_app
    from core.database import init_db

    application = create_app()
    application.config.update(TESTING=True, DATABASE_URL=database_url, COOKIE_SECURE=False)
    init_db(application)  # runs real migrations -> real schema, seeds default admin
    return application


from core.database import ConnectionWrapper


class _SavepointConnectionWrapper(ConnectionWrapper):
    """Same as ConnectionWrapper, but commit()/rollback() operate on a
    SAVEPOINT nested inside the outer per-test transaction instead of
    actually ending it. Plain ConnectionWrapper.commit() calls the real
    psycopg2 connection.commit() -- fine in production where every request
    gets its own connection, but fatal to this suite's isolation model:
    with one connection reused for a whole test (so route code and
    assertions see the same uncommitted state), a real commit() from
    execute()/executemany() ends the outer transaction there and then, so
    db_conn's teardown `raw.rollback()` only undoes whatever ran *after*
    that commit -- everything before it (e.g. a route changing the seeded
    admin's password) is permanently written to the database and leaks
    into every later test in the session. Routing commit()/rollback()
    through a savepoint instead keeps all of that nested inside the outer
    transaction, so the single real rollback at teardown is the only thing
    that ever decides what persists.
    """

    def __init__(self, raw_conn):
        super().__init__(raw_conn)
        self._conn.cursor().execute("SAVEPOINT test_txn")

    def commit(self):
        cur = self._conn.cursor()
        cur.execute("RELEASE SAVEPOINT test_txn")
        cur.execute("SAVEPOINT test_txn")

    def rollback(self):
        cur = self._conn.cursor()
        cur.execute("ROLLBACK TO SAVEPOINT test_txn")


@pytest.fixture
def db_conn(app):
    """One raw connection per test, wrapped in a transaction that's rolled
    back at the end -- so test data never leaks between tests and tests can
    run in any order. Routes are made to reuse this same connection (see
    `client` fixture) instead of opening their own, so route-side commits
    are actually nested savepoints that vanish on rollback."""
    import psycopg2

    raw = psycopg2.connect(app.config['DATABASE_URL'])
    raw.autocommit = False
    conn = _SavepointConnectionWrapper(raw)
    yield conn
    raw.rollback()
    raw.close()


@pytest.fixture
def client(app, db_conn, monkeypatch):
    """Flask test client wired so every route module's `get_db()` returns
    the test's single rolled-back-at-teardown connection instead of opening
    a fresh one per request, and with outbound email replaced by a
    recorder so tests never hit real SMTP.

    Every module below did `from core.database import get_db` (a plain
    Python import copies the reference into that module's own namespace),
    so patching core.database.get_db alone would NOT reach them --
    core.database.execute()/query_one()/query_all() call get_db() as a
    module-global lookup within core.database itself, so patching it there
    covers those three helpers, but every module that imported get_db
    directly needs its own patch too.
    """
    import core.database as db_module
    import core.auth as auth_module
    import core.utils as utils_module
    import core.sms as sms_module
    import core.mailer as mailer_module
    import core.mpesa as mpesa_module
    import core.routes.auth as auth_routes
    import core.routes.members as members_routes
    import core.routes.loans as loans_routes
    import core.routes.repayments as repayments_routes
    import core.routes.savings as savings_routes
    import core.routes.clients as clients_routes
    import core.routes.accounting as accounting_routes
    import core.routes.other_routes as other_routes
    import core.routes.dashboard as dashboard_routes
    import core.routes.reports as reports_routes
    import core.routes.mpesa as mpesa_routes

    def _fake_get_db():
        return db_conn

    modules_with_get_db = (
        db_module, auth_module, utils_module,
        sms_module, mailer_module, mpesa_module,
        auth_routes, members_routes, loans_routes, repayments_routes,
        savings_routes, clients_routes, accounting_routes, other_routes,
        dashboard_routes, reports_routes, mpesa_routes,
    )
    for mod in modules_with_get_db:
        if hasattr(mod, 'get_db'):
            monkeypatch.setattr(mod, 'get_db', _fake_get_db, raising=False)

    sent_emails = []

    def _fake_send_email_async(to_email, subject, body_text, body_html=None):
        sent_emails.append({'to': to_email, 'subject': subject, 'body': body_text})

    monkeypatch.setattr('core.mailer.send_email_async', _fake_send_email_async)

    # core.sms.send_sms_async fires a real background thread in production
    # (core/sms.py) so each async send gets its own connection from the
    # pool -- safe there. In this suite, get_db() is monkeypatched to always
    # hand back the single per-test db_conn (see modules_with_get_db above),
    # so a real background thread racing the main test thread over that one
    # non-thread-safe connection corrupts the outer transaction (savepoint
    # errors surfacing in unrelated, later tests). Faked the same way
    # send_email_async already is above, rather than actually spawning a
    # thread, for the same reason: no test asserts on SMS delivery, only on
    # the DB/route behavior that happens to trigger it.
    sent_sms = []

    def _fake_send_sms_async(to_phone, message):
        sent_sms.append({'to': to_phone, 'message': message})

    monkeypatch.setattr('core.sms.send_sms_async', _fake_send_sms_async)

    test_client = app.test_client()
    test_client.sent_emails = sent_emails
    test_client.sent_sms = sent_sms
    return test_client


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_token(client):
    """Logs in as the seeded default admin, changes the (forced) default
    password so subsequent requests aren't blocked by must_change_password,
    and returns a bearer token usable as
    client.get(url, headers=auth_header(token))."""
    resp = client.post('/auth/login', json={'username': 'admin', 'password': 'ChangeMe123!'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    token = resp.get_json()['access_token']

    resp = client.post('/auth/change-password-required', json={
        'current_password': 'ChangeMe123!',
        'new_password': 'TestAdminPass123!',
        'confirm_password': 'TestAdminPass123!',
    }, headers=auth_header(token))
    assert resp.status_code == 200, resp.get_data(as_text=True)

    resp = client.post('/auth/login', json={'username': 'admin', 'password': 'TestAdminPass123!'})
    assert resp.status_code == 200
    return resp.get_json()['access_token']


def auth_header(token):
    return {'Authorization': f'Bearer {token}'}


# ---------------------------------------------------------------------------
# Domain factories -- minimal valid rows for the entities tests build on
# ---------------------------------------------------------------------------
@pytest.fixture
def loan_product(db_conn):
    cur = db_conn.execute(
        """INSERT INTO loan_products (name, code, min_amount, max_amount, min_term, max_term,
               interest_rate, interest_type, repayment_frequency, insurance_fee, is_active, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()::text)""",
        ('Standard Loan', f'STD-{uuid.uuid4().hex[:8]}', 1000, 500000, 1, 24, 12.0, 'flat', 'monthly', 0, 1)
    )
    db_conn.commit()
    return db_conn.execute(
        "SELECT * FROM loan_products WHERE id = %s", (cur.lastrowid,)
    ).fetchone()


@pytest.fixture
def member(client, admin_token):
    resp = client.post('/members/api', json={
        'first_name': 'Jane',
        'last_name': 'Wanjiru',
        'phone': '0712345678',
        'email': 'jane@example.com',
        'status': 'active',
    }, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()['member']


@pytest.fixture
def approved_loan(client, admin_token, member, loan_product):
    """A loan that's past application + approval + disbursement -- the
    state repayment/write-off/topup tests actually need."""
    resp = client.post('/loans/api', json={
        'member_id': member['id'],
        'borrower_type': 'member',
        'product_id': loan_product['id'],
        'principal_amount': 10000,
        'term': 6,
    }, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.get_data(as_text=True)
    loan = resp.get_json()['loan']

    resp = client.post(f"/loans/api/{loan['id']}/approve", json={},
                        headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)

    resp = client.post(f"/loans/api/{loan['id']}/disburse", json={
        'disbursement_method': 'cash',
        'disbursement_date': date.today().isoformat(),
    }, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)

    resp = client.get(f"/loans/api/{loan['id']}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    return resp.get_json()
