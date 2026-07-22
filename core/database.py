"""
database.py
-----------
PostgreSQL data layer for Jodala Microfinance (via psycopg2).

Responsibilities:
  * Resolve the DB connection string (configurable via DATABASE_URL / DB_* env vars)
  * Provide per-request connections with row access by column name (and by
    position, to match the sqlite3.Row calling convention the rest of the
    codebase was written against)
  * Own the schema (CREATE TABLE statements)
  * Run versioned migrations against a `schema_migrations` table
  * Bootstrap first-run data (default admin user, default settings, chart of accounts)

Nothing in here talks HTTP. Routes call get_db() and run SQL directly (using
conn.execute(...), a convenience method sqlite3 connections have built in but
plain psycopg2 connections don't -- ConnectionWrapper below adds it), or use
the small helper functions below (query_one/query_all/execute).
"""
import os
import psycopg2
import psycopg2.extensions
import psycopg2.pool
from datetime import datetime, timezone
from flask import g, current_app


# ---------------------------------------------------------------------------
# Connection string resolution
# ---------------------------------------------------------------------------
def resolve_db_url():
    """
    DATABASE_URL wins if set (this is what Render, Railway, and Fly.io all
    inject automatically when a Postgres addon/service is attached).
    Falls back to assembling one from individual PG* env vars, and finally to
    a sane local-dev default (a `jodala` database on localhost with the
    `postgres` role -- create it with `createdb jodala` before first run).
    """
    url = os.getenv('DATABASE_URL')
    if url:
        # Render/Heroku-style URLs sometimes use the legacy 'postgres://'
        # scheme, which psycopg2 accepts but SQLAlchemy-style tooling
        # elsewhere may not -- normalize it to 'postgresql://' either way.
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        return url

    host = os.getenv('PGHOST', 'localhost')
    port = os.getenv('PGPORT', '5432')
    user = os.getenv('PGUSER', 'postgres')
    password = os.getenv('PGPASSWORD', '')
    dbname = os.getenv('PGDATABASE', 'jodala')
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{dbname}"


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _table_columns(conn, table_name):
    """Postgres equivalent of sqlite's `PRAGMA table_info(table)` -- returns
    the set of column names currently on `table_name`."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table_name,)
    ).fetchall()
    return {row[0] for row in rows}


def _resync_serial_sequence(conn, table_name, id_column='id'):
    """After inserting rows with explicit ids into a SERIAL column (as the
    clients-table rebuild migration below does), the table's sequence is
    left behind at its default starting value -- the next plain INSERT would
    collide with an id that's already in use. This moves the sequence past
    the highest id actually in the table."""
    conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{id_column}'), "
        f"COALESCE((SELECT MAX({id_column}) FROM {table_name}), 1), "
        f"(SELECT MAX({id_column}) FROM {table_name}) IS NOT NULL)"
    )


# ---------------------------------------------------------------------------
# sqlite3.Row-alike: supports row['col'], row[0], dict(row), and iteration,
# so the rest of the codebase (written against sqlite3.Row) doesn't need to
# change how it reads query results.
# ---------------------------------------------------------------------------
class Row:
    __slots__ = ('_keys', '_values')

    def __init__(self, keys, values):
        self._keys = keys
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._keys.index(key)]
        return self._values[key]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, key):
        return key in self._keys

    def __repr__(self):
        return f"Row({dict(zip(self._keys, self._values))})"


class CursorWrapper:
    """Wraps a psycopg2 cursor so fetchone()/fetchall()/fetchmany() return
    Row objects instead of plain tuples, and exposes `.lastrowid` (populated
    by ConnectionWrapper.execute() for INSERTs) the way sqlite3 cursors do."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def _wrap(self, raw):
        if raw is None:
            return None
        keys = [d[0] for d in self._cursor.description]
        return Row(keys, list(raw))

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        raws = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._wrap(r) for r in raws]

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class ConnectionWrapper:
    """Adds sqlite3-style conn.execute(...)/conn.executemany(...) convenience
    methods on top of a plain psycopg2 connection, and auto-populates
    cursor.lastrowid for INSERTs (psycopg2 has no native lastrowid support --
    it relies on `RETURNING`, which this transparently appends)."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    # The only table in this schema whose primary key isn't `id` (it's
    # `version`) -- every other INSERT can safely ask for `RETURNING id`.
    _NO_ID_PK_TABLES = {'schema_migrations'}

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        stripped = sql.lstrip()
        is_insert = stripped[:11].upper() == 'INSERT INTO'
        run_sql = sql
        if is_insert and 'RETURNING' not in sql.upper():
            target_table = stripped[11:].strip().split()[0].split('(')[0].strip('"').lower()
            if target_table not in self._NO_ID_PK_TABLES:
                run_sql = sql.rstrip().rstrip(';') + ' RETURNING id'
            else:
                is_insert = False
        cur.execute(run_sql, params)
        wrapped = CursorWrapper(cur)
        if is_insert:
            try:
                row = cur.fetchone()
                wrapped.lastrowid = row[0] if row else None
            except psycopg2.ProgrammingError:
                wrapped.lastrowid = None
        return wrapped

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql, seq_of_params)
        return CursorWrapper(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)


# ---------------------------------------------------------------------------
# Connection management (pooled -- one *borrowed* connection per request,
# stashed on flask.g, returned to the pool at teardown instead of closed)
# ---------------------------------------------------------------------------
#
# Previously get_db() called psycopg2.connect() fresh on every single
# request and close_db() fully closed it. Against a remote-hosted Postgres
# (Render/Railway/Fly/etc, not a local socket) that means a full TCP
# handshake + SSL negotiation + auth round trip -- often 50-150ms -- paid
# on *every* request before any query even runs. The dashboard alone fires
# ~8 concurrent requests on load, so that was ~8 fresh connections opened
# and torn down at once, on top of the query time itself.
#
# _pool is intentionally a lazily-created module global, NOT created at
# import time. gunicorn.conf.py sets preload_app=True, which loads this
# module once in the master process *before* forking workers -- a pool
# opened at import time would have its live sockets duplicated across
# every forked worker (all sharing the same underlying file descriptors),
# which silently corrupts connections under real concurrency. Creating it
# lazily on first get_db() call means each worker process builds its own
# pool the first time it actually handles a request, safely after the fork.
_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        # minconn=1 so an idle worker doesn't hold connections it isn't
        # using; maxconn covers gunicorn's threads-per-worker (default 4,
        # see gunicorn.conf.py) with headroom for the background scheduler.
        maxconn = int(os.getenv('DB_POOL_MAX_CONN', '10'))
        _pool = psycopg2.pool.ThreadedConnectionPool(1, maxconn, current_app.config['DATABASE_URL'])
    return _pool


def get_db():
    if 'db_conn' not in g:
        raw_conn = _get_pool().getconn()
        raw_conn.autocommit = False
        g.db_conn = ConnectionWrapper(raw_conn)
    return g.db_conn


# ---------------------------------------------------------------------------
# Company branding cache
# ---------------------------------------------------------------------------
#
# inject_company_branding() (core/__init__.py) runs on every template render
# -- i.e. almost every request -- so a naive implementation means a
# `SELECT ... FROM company_settings` on every single page load, for two
# values (company_name, logo_image) that change maybe a few times a year.
#
# Cached per worker process with a short TTL rather than cached forever:
# invalidate_branding_cache() (called right after Settings > Company saves
# company_name/logo_image, see core/routes/other_routes.py:update_company)
# clears it instantly in whichever worker handles that request, so the
# admin who just changed it sees the update immediately. The TTL below is
# just a safety net for every *other* worker process -- each holds its own
# copy of this module global, not a shared cache, so they wouldn't otherwise
# see that invalidation -- so they fall back to picking up the change within
# BRANDING_CACHE_TTL seconds regardless.
_branding_cache = {'data': None, 'expires_at': 0.0}
BRANDING_CACHE_TTL = int(os.getenv('BRANDING_CACHE_TTL', '60'))


def get_company_branding():
    import time
    now = time.monotonic()
    if _branding_cache['data'] is not None and now < _branding_cache['expires_at']:
        return _branding_cache['data']

    try:
        rows = get_db().execute(
            "SELECT key, value FROM company_settings WHERE key IN ('company_name', 'logo_image')"
        ).fetchall()
        branding = {r['key']: r['value'] for r in rows}
    except Exception:
        branding = {}

    data = {
        'company_name': branding.get('company_name') or 'Jodala Microfinance',
        'company_logo': branding.get('logo_image') or ''
    }
    _branding_cache['data'] = data
    _branding_cache['expires_at'] = now + BRANDING_CACHE_TTL
    return data


def invalidate_branding_cache():
    _branding_cache['data'] = None
    _branding_cache['expires_at'] = 0.0


def close_db(e=None):
    conn = g.pop('db_conn', None)
    if conn is not None:
        raw_conn = conn._conn
        is_broken = raw_conn.closed != 0
        if not is_broken:
            try:
                # Reads never call commit(), so a read-only request can
                # leave a transaction open (autocommit=False) -- rolling
                # back before the connection goes back in the pool closes
                # that transaction out cleanly instead of leaving it
                # idle-in-transaction on Postgres until some later,
                # unrelated request reuses this connection and happens to
                # commit.
                conn.rollback()
            except Exception:
                is_broken = True
        _get_pool().putconn(raw_conn, close=is_broken)


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    """Run an INSERT/UPDATE/DELETE, commit, and return the cursor
    (cursor.lastrowid is populated for INSERTs)."""
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def executemany(sql, seq_of_params):
    db = get_db()
    cur = db.executemany(sql, seq_of_params)
    db.commit()
    return cur


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'loan_officer',
        phone TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0,
        last_login TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS members (
        id SERIAL PRIMARY KEY,
        member_number TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        middle_name TEXT,
        gender TEXT,
        date_of_birth TEXT,
        national_id TEXT UNIQUE,
        phone TEXT NOT NULL,
        email TEXT,
        region TEXT,
        district TEXT,
        village TEXT,
        address TEXT,
        occupation TEXT,
        employer TEXT,
        monthly_income REAL DEFAULT 0,
        next_of_kin_name TEXT,
        next_of_kin_phone TEXT,
        next_of_kin_relation TEXT,
        member_type TEXT DEFAULT 'member',
        group_name TEXT,
        status TEXT DEFAULT 'active',
        photo TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS clients (
        id SERIAL PRIMARY KEY,
        client_number TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        middle_name TEXT,
        gender TEXT,
        date_of_birth TEXT,
        national_id TEXT UNIQUE,
        phone TEXT NOT NULL,
        email TEXT,
        region TEXT,
        district TEXT,
        village TEXT,
        address TEXT,
        occupation TEXT,
        employer TEXT,
        monthly_income REAL DEFAULT 0,
        next_of_kin_name TEXT,
        next_of_kin_phone TEXT,
        next_of_kin_relation TEXT,
        status TEXT DEFAULT 'active',
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS loan_products (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        description TEXT,
        min_amount REAL NOT NULL,
        max_amount REAL NOT NULL,
        interest_rate REAL NOT NULL,
        interest_type TEXT DEFAULT 'flat',
        repayment_frequency TEXT DEFAULT 'monthly',
        min_term INTEGER NOT NULL,
        max_term INTEGER NOT NULL,
        penalty_rate REAL DEFAULT 0,
        insurance_fee REAL DEFAULT 0,
        require_guarantor INTEGER DEFAULT 0,
        require_collateral INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS loans (
        id SERIAL PRIMARY KEY,
        loan_number TEXT UNIQUE NOT NULL,
        member_id INTEGER REFERENCES members(id),
        client_id INTEGER REFERENCES clients(id),
        product_id INTEGER NOT NULL REFERENCES loan_products(id),
        borrower_type TEXT DEFAULT 'member',
        principal_amount REAL NOT NULL,
        interest_rate REAL NOT NULL,
        interest_type TEXT DEFAULT 'flat',
        term INTEGER NOT NULL,
        repayment_frequency TEXT DEFAULT 'monthly',
        total_interest REAL DEFAULT 0,
        total_repayable REAL DEFAULT 0,
        insurance_fee REAL DEFAULT 0,
        amount_disbursed REAL DEFAULT 0,
        outstanding_balance REAL DEFAULT 0,
        total_paid REAL DEFAULT 0,
        total_penalties REAL DEFAULT 0,
        purpose TEXT,
        collateral TEXT,
        guarantor_name TEXT,
        guarantor_phone TEXT,
        status TEXT DEFAULT 'pending',
        application_date TEXT,
        approval_date TEXT,
        disbursement_date TEXT,
        first_repayment_date TEXT,
        expected_end_date TEXT,
        actual_end_date TEXT,
        loan_officer_id INTEGER REFERENCES users(id),
        approved_by INTEGER REFERENCES users(id),
        disbursed_by INTEGER REFERENCES users(id),
        rejection_reason TEXT,
        notes TEXT,
        is_topup INTEGER DEFAULT 0,
        rollover_amount REAL DEFAULT 0,
        parent_loan_id INTEGER REFERENCES loans(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS loan_schedules (
        id SERIAL PRIMARY KEY,
        loan_id INTEGER NOT NULL REFERENCES loans(id),
        installment_number INTEGER NOT NULL,
        due_date TEXT NOT NULL,
        principal_due REAL DEFAULT 0,
        interest_due REAL DEFAULT 0,
        total_due REAL DEFAULT 0,
        principal_paid REAL DEFAULT 0,
        interest_paid REAL DEFAULT 0,
        penalty_paid REAL DEFAULT 0,
        total_paid REAL DEFAULT 0,
        balance_after REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        paid_date TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS repayments (
        id SERIAL PRIMARY KEY,
        receipt_number TEXT UNIQUE NOT NULL,
        loan_id INTEGER NOT NULL REFERENCES loans(id),
        amount REAL NOT NULL,
        principal_portion REAL DEFAULT 0,
        interest_portion REAL DEFAULT 0,
        penalty_portion REAL DEFAULT 0,
        payment_method TEXT DEFAULT 'cash',
        reference_number TEXT,
        payment_date TEXT,
        notes TEXT,
        collected_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS savings_products (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        description TEXT,
        interest_rate REAL DEFAULT 0,
        min_balance REAL DEFAULT 0,
        withdrawal_limit REAL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS regions (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS savings_accounts (
        id SERIAL PRIMARY KEY,
        account_number TEXT UNIQUE NOT NULL,
        member_id INTEGER NOT NULL REFERENCES members(id),
        product_id INTEGER NOT NULL REFERENCES savings_products(id),
        balance REAL DEFAULT 0,
        status TEXT DEFAULT 'active',
        opened_at TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS savings_transactions (
        id SERIAL PRIMARY KEY,
        transaction_number TEXT UNIQUE NOT NULL,
        account_id INTEGER NOT NULL REFERENCES savings_accounts(id),
        transaction_type TEXT NOT NULL,
        amount REAL NOT NULL,
        balance_before REAL DEFAULT 0,
        balance_after REAL DEFAULT 0,
        payment_method TEXT DEFAULT 'cash',
        reference TEXT,
        notes TEXT,
        transaction_date TEXT,
        processed_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS accounts (
        id SERIAL PRIMARY KEY,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        account_type TEXT NOT NULL,
        parent_id INTEGER REFERENCES accounts(id),
        balance REAL DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        description TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS journal_entries (
        id SERIAL PRIMARY KEY,
        entry_number TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        reference TEXT,
        entry_type TEXT DEFAULT 'manual',
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS journal_entry_lines (
        id SERIAL PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
        debit_account_id INTEGER REFERENCES accounts(id),
        credit_account_id INTEGER REFERENCES accounts(id),
        amount REAL NOT NULL,
        description TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS income (
        id SERIAL PRIMARY KEY,
        reference TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        category TEXT,
        amount REAL NOT NULL,
        income_date TEXT NOT NULL,
        payment_method TEXT DEFAULT 'cash',
        account_id INTEGER REFERENCES accounts(id),
        recorded_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS expenses (
        id SERIAL PRIMARY KEY,
        reference TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        category TEXT,
        amount REAL NOT NULL,
        expense_date TEXT NOT NULL,
        payment_method TEXT DEFAULT 'cash',
        vendor TEXT,
        account_id INTEGER REFERENCES accounts(id),
        recorded_by INTEGER REFERENCES users(id),
        approved_by INTEGER REFERENCES users(id),
        receipt_ref TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        notification_type TEXT DEFAULT 'info',
        is_read INTEGER DEFAULT 0,
        related_type TEXT,
        related_id INTEGER,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS email_log (
        id SERIAL PRIMARY KEY,
        recipient TEXT NOT NULL,
        subject TEXT,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        resource_type TEXT,
        resource_id INTEGER,
        old_values TEXT,
        new_values TEXT,
        ip_address TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS company_settings (
        id SERIAL PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,
        value TEXT,
        description TEXT,
        updated_at TEXT NOT NULL
    )""",
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_loans_member ON loans(member_id)",
    "CREATE INDEX IF NOT EXISTS idx_loans_client ON loans(client_id)",
    "CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)",
    "CREATE INDEX IF NOT EXISTS idx_loans_disbursement_date ON loans(disbursement_date)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_loan ON loan_schedules(loan_id)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_due ON loan_schedules(due_date, status)",
    "CREATE INDEX IF NOT EXISTS idx_repayments_loan ON repayments(loan_id)",
    "CREATE INDEX IF NOT EXISTS idx_repayments_payment_date ON repayments(payment_date)",
    "CREATE INDEX IF NOT EXISTS idx_savings_member ON savings_accounts(member_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_income_date ON income(income_date)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date)",
    "CREATE INDEX IF NOT EXISTS idx_journal_entries_date ON journal_entries(entry_date)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read)",
]


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Each migration is (version, description, fn(conn)). Applied in order, once,
# tracked in schema_migrations. Add new ones to the END of this list only --
# never edit or reorder past entries once shipped.
def _migration_0001_initial_schema(conn):
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    for stmt in INDEX_STATEMENTS:
        conn.execute(stmt)


def _migration_0002_seed_defaults(conn):
    now = utcnow()

    existing_admin = conn.execute(
        "SELECT id FROM users WHERE username = %s", ('admin',)
    ).fetchone()
    if not existing_admin:
        # Local import to avoid a circular import at module load time.
        from core.auth import hash_password
        conn.execute(
            """INSERT INTO users (username, email, password_hash, full_name, role,
                                   is_active, must_change_password, totp_enabled, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, 1, 1, 0, %s, %s)""",
            ('admin', 'admin@jodala.local', hash_password('ChangeMe123!'),
             'System Administrator', 'admin', now, now)
        )

    default_settings = {
        'company_name': 'Jodala Microfinance',
        'company_email': 'jodalamicrofinance@gmail.com',
        'company_phone': '',
        'company_address': '',
        'company_website': '',
        'currency': 'KES',
        'fiscal_year_start': '01-01',
        'loan_prefix': 'LN',
        'logo_image': '',
        'main_account_opening_balance': '0',
        'gmail_address': 'jodalamicrofinance@gmail.com',
        'gmail_app_password': '',
        'gmail_sender_name': 'Jodala Microfinance',
        'email_notifications_enabled': '1',
    }
    for key, value in default_settings.items():
        row = conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (key, value, now)
            )

    default_accounts = [
        ('1000', 'Cash and Bank', 'asset'),
        ('1100', 'Loans Receivable', 'asset'),
        ('2000', 'Member Savings', 'liability'),
        ('3000', 'Equity', 'equity'),
        ('4000', 'Interest Income', 'income'),
        ('4100', 'Fee Income', 'income'),
        ('5000', 'Operating Expenses', 'expense'),
        ('5100', 'Loan Write-offs', 'expense'),
    ]
    for code, name, acc_type in default_accounts:
        row = conn.execute("SELECT id FROM accounts WHERE code = %s", (code,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO accounts (code, name, account_type, balance, is_active) VALUES (%s, %s, %s, 0, 1)",
                (code, name, acc_type)
            )


def _migration_0003_clients_are_borrowers_not_businesses(conn):
    """The `clients` table originally modeled clients as businesses
    (business_name, contact_person, business_type, annual_revenue,
    registration_number). Clients are actually just non-member borrowers --
    individuals, same shape as members -- so this rebuilds the table with
    first_name/last_name/national_id/occupation instead. Safe to run whether
    the table is still in the old shape (fresh installs created after this
    migration was added already get the new shape from SCHEMA_STATEMENTS, so
    this is a no-op for them)."""
    columns = _table_columns(conn, 'clients')
    if 'business_name' not in columns:
        return  # already the new shape

    conn.execute("ALTER TABLE clients RENAME TO clients_old")
    conn.execute("""CREATE TABLE clients (
        id SERIAL PRIMARY KEY,
        client_number TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        national_id TEXT UNIQUE,
        phone TEXT NOT NULL,
        email TEXT,
        region TEXT,
        district TEXT,
        address TEXT,
        occupation TEXT,
        status TEXT DEFAULT 'active',
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")

    old_cursor = conn.execute("SELECT * FROM clients_old")
    old_columns = [d[0] for d in old_cursor.description]
    old_rows = old_cursor.fetchall()
    for row in old_rows:
        old = dict(zip(old_columns, row))
        # Best-effort split of contact_person into first/last name; fall back
        # to the business name if contact_person wasn't captured.
        name_source = old.get('contact_person') or old.get('business_name') or ''
        parts = name_source.strip().split(' ', 1)
        first_name = parts[0] if parts and parts[0] else (old.get('business_name') or 'Unknown')
        last_name = parts[1] if len(parts) > 1 else ''
        conn.execute(
            """INSERT INTO clients (id, client_number, first_name, last_name, phone, email,
                   region, district, address, status, created_by, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (old['id'], old['client_number'], first_name, last_name, old['phone'], old['email'],
             old['region'], old['district'], old['address'], old['status'], old['created_by'],
             old['created_at'], old['updated_at'])
        )

    conn.execute("DROP TABLE clients_old")
    _resync_serial_sequence(conn, 'clients')


def _migration_0004_client_gender_dob(conn):
    """Clients were missing gender/date_of_birth, which members already have
    as part of their personal-info section -- this brings clients up to the
    same shape. Guarded so it's a no-op on fresh installs that already get
    these columns straight from SCHEMA_STATEMENTS."""
    columns = _table_columns(conn, 'clients')
    if 'gender' not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN gender TEXT")
    if 'date_of_birth' not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN date_of_birth TEXT")


def _migration_0005_loan_rollover_amount(conn):
    """Top-up loans roll the parent loan's unpaid balance into the new loan's
    principal without any new cash changing hands -- only the extra amount
    the borrower actually receives is real disbursed cash. Without tracking
    how much of a top-up's principal is rollover vs. new cash, disbursement
    was paying out the *entire* new principal (including the rolled-over
    debt), which double-counts money that already left the main account
    when the original loan was disbursed."""
    columns = _table_columns(conn, 'loans')
    if 'rollover_amount' not in columns:
        conn.execute("ALTER TABLE loans ADD COLUMN rollover_amount REAL DEFAULT 0")


def _migration_0006_regions_table(conn):
    """Region was previously a hardcoded list of options baked into every
    member/client form template (Thika, Chuka, Nyeri, Meru, Maua, Naivasha,
    Mirema). This moves it into a proper lookup table that's manageable from
    Settings, and seeds it with the old hardcoded list so existing member and
    client records still match a known region."""
    conn.execute("""CREATE TABLE IF NOT EXISTS regions (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    now = utcnow()
    for name in ['Thika', 'Chuka', 'Nyeri', 'Meru', 'Maua', 'Naivasha', 'Mirema']:
        existing = conn.execute("SELECT id FROM regions WHERE name = %s", (name,)).fetchone()
        if not existing:
            conn.execute("INSERT INTO regions (name, is_active, created_at) VALUES (%s, 1, %s)", (name, now))


def _migration_0007_email_log_and_gmail_settings(conn):
    """Adds the email_log table (tracks every notification email attempt --
    sent/failed/skipped -- for the Settings > Notifications activity panel)
    and the gmail_* / email_notifications_enabled company_settings keys.
    Both were added to SCHEMA_STATEMENTS/defaults after migration 0001/0002
    already ran for existing installs, so those installs never picked them
    up -- this backfills them idempotently."""
    conn.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id SERIAL PRIMARY KEY,
        recipient TEXT NOT NULL,
        subject TEXT,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    )""")
    now = utcnow()
    gmail_defaults = {
        'gmail_address': '',
        'gmail_app_password': '',
        'gmail_sender_name': 'Jodala Microfinance',
        'email_notifications_enabled': '1',
    }
    for key, value in gmail_defaults.items():
        row = conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (key, value, now)
            )


def _migration_0008_mpesa_transactions(conn):
    """Adds M-Pesa STK Push support: the mpesa_transactions table (tracks
    every push request from initiation through the Safaricom callback -- the
    Settings > M-Pesa activity panel and the frontend polling endpoint both
    read from this) and the mpesa_* company_settings keys used to configure
    Daraja credentials in-app."""
    conn.execute("""CREATE TABLE IF NOT EXISTS mpesa_transactions (
        id SERIAL PRIMARY KEY,
        checkout_request_id TEXT UNIQUE,
        merchant_request_id TEXT,
        purpose TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        phone TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result_code INTEGER,
        result_desc TEXT,
        mpesa_receipt_number TEXT,
        repayment_id INTEGER REFERENCES repayments(id),
        savings_transaction_id INTEGER REFERENCES savings_transactions(id),
        initiated_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mpesa_checkout ON mpesa_transactions(checkout_request_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mpesa_status ON mpesa_transactions(status)")

    now = utcnow()
    mpesa_defaults = {
        'mpesa_environment': 'sandbox',
        'mpesa_consumer_key': '',
        'mpesa_consumer_secret': '',
        'mpesa_shortcode': '',
        'mpesa_passkey': '',
        'mpesa_enabled': '1',
    }
    for key, value in mpesa_defaults.items():
        row = conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (key, value, now)
            )


def _migration_0009_mpesa_b2c(conn):
    """Adds M-Pesa B2C (Business to Customer) support for loan disbursement:
    a disbursement_method column on loans, the extra correlation-ID columns
    B2C needs on mpesa_transactions (B2C uses OriginatorConversationID /
    ConversationID / TransactionID instead of STK's CheckoutRequestID), and
    the mpesa_initiator_*/mpesa_b2c_* company_settings keys."""
    cols = list(_table_columns(conn, 'loans'))
    if 'disbursement_method' not in cols:
        conn.execute("ALTER TABLE loans ADD COLUMN disbursement_method TEXT DEFAULT 'cash'")

    mpesa_cols = list(_table_columns(conn, 'mpesa_transactions'))
    if 'originator_conversation_id' not in mpesa_cols:
        conn.execute("ALTER TABLE mpesa_transactions ADD COLUMN originator_conversation_id TEXT")
    if 'conversation_id' not in mpesa_cols:
        conn.execute("ALTER TABLE mpesa_transactions ADD COLUMN conversation_id TEXT")
    if 'transaction_id' not in mpesa_cols:
        conn.execute("ALTER TABLE mpesa_transactions ADD COLUMN transaction_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mpesa_originator_conv "
        "ON mpesa_transactions(originator_conversation_id)"
    )

    now = utcnow()
    b2c_defaults = {
        'mpesa_initiator_name': '',
        'mpesa_initiator_password': '',
        'mpesa_b2c_shortcode': '',
        'mpesa_b2c_certificate': '',
        'mpesa_b2c_command_id': 'BusinessPayment',
    }
    for key, value in b2c_defaults.items():
        row = conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (key, value, now)
            )


def _migration_0010_client_full_kyc_fields(conn):
    """Clients only had a subset of the KYC fields members have (missing
    middle_name, village, employer, monthly_income, next_of_kin_*) even
    though the registration/edit UI is meant to mirror members'. Brings the
    clients table up to the same shape. Guarded so it's a no-op on fresh
    installs that already get these columns straight from SCHEMA_STATEMENTS."""
    columns = _table_columns(conn, 'clients')
    for col, coltype in [
        ('middle_name', 'TEXT'),
        ('village', 'TEXT'),
        ('employer', 'TEXT'),
        ('monthly_income', 'REAL DEFAULT 0'),
        ('next_of_kin_name', 'TEXT'),
        ('next_of_kin_phone', 'TEXT'),
        ('next_of_kin_relation', 'TEXT'),
    ]:
        if col not in columns:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {coltype}")


def _migration_0011_force_default_admin_password_change(conn):
    """Adds `must_change_password`, and flags it for any existing 'admin'
    account whose password is still the seeded default -- a deployment that
    already changed it is left alone, but one that never got around to it
    (the common case people forget) now gets forced to on next login rather
    than silently running production on a publicly-documented password."""
    columns = _table_columns(conn, 'users')
    if 'must_change_password' not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

    from core.auth import verify_password
    for row in conn.execute("SELECT id, password_hash FROM users WHERE username = 'admin'").fetchall():
        if verify_password('ChangeMe123!', row[1]):
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = %s", (row[0],))


def _migration_0012_remove_processing_fee(conn):
    """Processing fee has been removed from the product -- drop the column
    from both loan_products and loans on installs that still have it (fresh
    installs never create it in the first place, see the schema above)."""
    for table in ('loan_products', 'loans'):
        columns = _table_columns(conn, table)
        if 'processing_fee' in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN processing_fee")


def _migration_0013_resend_email_settings(conn):
    """Switches email delivery from Gmail SMTP to the Resend HTTP API --
    Gmail SMTP doesn't work on Render/most PaaS hosts because they block
    outbound SMTP ports at the network level. Seeds the new resend_*
    company_settings keys; the old gmail_* rows are left in place (unused,
    harmless) rather than deleted, in case anyone wants to roll back."""
    now = utcnow()
    resend_defaults = {
        'resend_api_key': '',
        'resend_from_email': '',
        'resend_sender_name': 'Jodala Microfinance',
    }
    for key, value in resend_defaults.items():
        row = conn.execute("SELECT id FROM company_settings WHERE key = %s", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (key, value, now)
            )


def _migration_0014_loan_restructuring(conn):
    """Formal loan restructuring for members/clients in genuine distress --
    distinct from top-up (more cash out) and extend (term only, rate/history
    untouched). A restructure can change term, interest rate, interest type
    and repayment frequency together and re-amortizes the *remaining
    principal* over the new terms, while keeping a full before/after audit
    trail in loan_restructures (including a snapshot of the schedule rows
    that get replaced) -- unlike top-up/extend, which only log a one-line
    audit_logs entry with no durable record of what the loan looked like
    before the change.

    is_restructured/restructure_count on loans let reports flag distressed
    loans without joining loan_restructures for the common case."""
    columns = _table_columns(conn, 'loans')
    if 'is_restructured' not in columns:
        conn.execute("ALTER TABLE loans ADD COLUMN is_restructured INTEGER DEFAULT 0")
    if 'restructure_count' not in columns:
        conn.execute("ALTER TABLE loans ADD COLUMN restructure_count INTEGER DEFAULT 0")

    conn.execute("""CREATE TABLE IF NOT EXISTS loan_restructures (
        id SERIAL PRIMARY KEY,
        loan_id INTEGER NOT NULL REFERENCES loans(id),
        reason TEXT NOT NULL,
        old_principal_outstanding REAL NOT NULL,
        old_term INTEGER NOT NULL,
        old_interest_rate REAL NOT NULL,
        old_interest_type TEXT NOT NULL,
        old_repayment_frequency TEXT NOT NULL,
        old_expected_end_date TEXT,
        old_schedule_snapshot TEXT,
        new_term INTEGER NOT NULL,
        new_interest_rate REAL NOT NULL,
        new_interest_type TEXT NOT NULL,
        new_repayment_frequency TEXT NOT NULL,
        new_expected_end_date TEXT,
        restructured_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""")


def _migration_0015_loan_writeoff_account(conn):
    """Adds a 'Loan Write-offs' (5100) expense account to the chart of
    accounts. Writing off a loan was previously a status-only change on the
    loans table -- it never touched the ledger, so Loans Receivable (1100)
    stayed permanently overstated by every written-off balance and no bad
    debt expense was ever recognized. core/routes/loans.py:write_off_loan
    now posts Debit 5100 / Credit 1100 for the loan's remaining booked
    principal; this migration makes sure 5100 exists on every install,
    including ones that ran migration 2 (seed defaults) long before this
    account was added."""
    row = conn.execute("SELECT id FROM accounts WHERE code = '5100'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO accounts (code, name, account_type, balance, is_active) VALUES (%s, %s, %s, 0, 1)",
            ('5100', 'Loan Write-offs', 'expense')
        )


def _migration_0016_role_permissions(conn):
    """Adds a role_permissions table backing the new Settings > Permissions
    page, letting an admin toggle which actions each non-admin role can
    perform instead of it being hardcoded in @role_required(...) decorators.
    Seeded from core.permissions.DEFAULT_ROLE_PERMISSIONS so existing
    installs keep exactly the same access they had before this migration --
    nothing changes until an admin explicitly edits it."""
    from core.permissions import PERMISSIONS, CONFIGURABLE_ROLES, DEFAULT_ROLE_PERMISSIONS

    conn.execute("""CREATE TABLE IF NOT EXISTS role_permissions (
        id SERIAL PRIMARY KEY,
        role TEXT NOT NULL,
        permission_key TEXT NOT NULL,
        granted BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TEXT,
        UNIQUE(role, permission_key)
    )""")

    now = utcnow()
    for role in CONFIGURABLE_ROLES:
        granted_keys = DEFAULT_ROLE_PERMISSIONS.get(role, set())
        for key in PERMISSIONS:
            conn.execute(
                "INSERT INTO role_permissions (role, permission_key, granted, updated_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (role, permission_key) DO NOTHING",
                (role, key, key in granted_keys, now)
            )


MIGRATIONS = [
    (1, 'initial schema', _migration_0001_initial_schema),
    (2, 'seed default admin/settings/accounts', _migration_0002_seed_defaults),
    (3, 'clients are individual borrowers, not businesses', _migration_0003_clients_are_borrowers_not_businesses),
    (4, 'add gender/date_of_birth to clients', _migration_0004_client_gender_dob),
    (5, 'add rollover_amount to loans for correct top-up disbursement', _migration_0005_loan_rollover_amount),
    (6, 'move region from hardcoded list to a manageable regions table', _migration_0006_regions_table),
    (7, 'add email_log table and gmail settings for existing installs', _migration_0007_email_log_and_gmail_settings),
    (8, 'add mpesa_transactions table and mpesa settings for STK Push', _migration_0008_mpesa_transactions),
    (9, 'add mpesa B2C support for loan disbursement', _migration_0009_mpesa_b2c),
    (10, 'bring clients KYC fields up to parity with members', _migration_0010_client_full_kyc_fields),
    (11, 'force password change for admin accounts still on the seeded default', _migration_0011_force_default_admin_password_change),
    (12, 'permanently remove processing fee from loan products and loans', _migration_0012_remove_processing_fee),
    (13, 'switch email delivery from Gmail SMTP to Resend HTTP API', _migration_0013_resend_email_settings),
    (14, 'add formal loan restructuring (term/rate re-negotiation with history)', _migration_0014_loan_restructuring),
    (15, "add 'Loan Write-offs' (5100) expense account so write-offs post to the ledger", _migration_0015_loan_writeoff_account),
    (16, "add role_permissions table for configurable per-role action permissions", _migration_0016_role_permissions),
]


def run_migrations(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        description TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""")
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, description, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (%s, %s, %s)",
            (version, description, utcnow())
        )
        conn.commit()


def init_db(app):
    """Call once at app startup: resolves DATABASE_URL, opens a bootstrap
    connection, and runs any pending migrations."""
    raw_conn = psycopg2.connect(app.config['DATABASE_URL'])
    conn = ConnectionWrapper(raw_conn)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
