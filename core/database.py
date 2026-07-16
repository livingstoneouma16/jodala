"""
database.py
-----------
Raw sqlite3 data layer for Jodala Microfinance.

Responsibilities:
  * Resolve the DB file location (configurable via DB_PATH env var)
  * Provide per-request connections with row access by column name
  * Own the schema (CREATE TABLE statements)
  * Run versioned migrations against a `schema_migrations` table
  * Bootstrap first-run data (default admin user, default settings, chart of accounts)

Nothing in here talks HTTP. Routes call get_db() and run SQL directly, or use
the small helper functions below (query_one/query_all/execute).
"""
import os
import sqlite3
from datetime import datetime, timezone
from flask import g, current_app


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def resolve_db_path():
    """
    DB_PATH env var wins if set (e.g. production: /var/data/sacco.db).
    Otherwise fall back to a local file next to the project for dev/test.
    """
    env_path = os.getenv('DB_PATH')
    if env_path:
        return env_path
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'instance', 'sacco.db')


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Connection management (one connection per request, stashed on flask.g)
# ---------------------------------------------------------------------------
def get_db():
    if 'db_conn' not in g:
        path = current_app.config['DB_PATH']
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        g.db_conn = conn
    return g.db_conn


def close_db(e=None):
    conn = g.pop('db_conn', None)
    if conn is not None:
        conn.close()


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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        processing_fee REAL DEFAULT 0,
        insurance_fee REAL DEFAULT 0,
        require_guarantor INTEGER DEFAULT 0,
        require_collateral INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        processing_fee REAL DEFAULT 0,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS savings_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_number TEXT UNIQUE NOT NULL,
        member_id INTEGER NOT NULL REFERENCES members(id),
        product_id INTEGER NOT NULL REFERENCES savings_products(id),
        balance REAL DEFAULT 0,
        status TEXT DEFAULT 'active',
        opened_at TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS savings_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        account_type TEXT NOT NULL,
        parent_id INTEGER REFERENCES accounts(id),
        balance REAL DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        description TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_number TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        reference TEXT,
        entry_type TEXT DEFAULT 'manual',
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS journal_entry_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
        debit_account_id INTEGER REFERENCES accounts(id),
        credit_account_id INTEGER REFERENCES accounts(id),
        amount REAL NOT NULL,
        description TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS income (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient TEXT NOT NULL,
        subject TEXT,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    "CREATE INDEX IF NOT EXISTS idx_schedule_loan ON loan_schedules(loan_id)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_due ON loan_schedules(due_date, status)",
    "CREATE INDEX IF NOT EXISTS idx_repayments_loan ON repayments(loan_id)",
    "CREATE INDEX IF NOT EXISTS idx_savings_member ON savings_accounts(member_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)",
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
        "SELECT id FROM users WHERE username = ?", ('admin',)
    ).fetchone()
    if not existing_admin:
        # Local import to avoid a circular import at module load time.
        from core.auth import hash_password
        conn.execute(
            """INSERT INTO users (username, email, password_hash, full_name, role,
                                   is_active, must_change_password, totp_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, 0, ?, ?)""",
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
        row = conn.execute("SELECT id FROM company_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (?, ?, ?)",
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
    ]
    for code, name, acc_type in default_accounts:
        row = conn.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO accounts (code, name, account_type, balance, is_active) VALUES (?, ?, ?, 0, 1)",
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)")}
    if 'business_name' not in columns:
        return  # already the new shape

    conn.execute("ALTER TABLE clients RENAME TO clients_old")
    conn.execute("""CREATE TABLE clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (old['id'], old['client_number'], first_name, last_name, old['phone'], old['email'],
             old['region'], old['district'], old['address'], old['status'], old['created_by'],
             old['created_at'], old['updated_at'])
        )

    conn.execute("DROP TABLE clients_old")


def _migration_0004_client_gender_dob(conn):
    """Clients were missing gender/date_of_birth, which members already have
    as part of their personal-info section -- this brings clients up to the
    same shape. Guarded so it's a no-op on fresh installs that already get
    these columns straight from SCHEMA_STATEMENTS."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)")}
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(loans)")}
    if 'rollover_amount' not in columns:
        conn.execute("ALTER TABLE loans ADD COLUMN rollover_amount REAL DEFAULT 0")


def _migration_0006_regions_table(conn):
    """Region was previously a hardcoded list of options baked into every
    member/client form template (Thika, Chuka, Nyeri, Meru, Maua, Naivasha,
    Mirema). This moves it into a proper lookup table that's manageable from
    Settings, and seeds it with the old hardcoded list so existing member and
    client records still match a known region."""
    conn.execute("""CREATE TABLE IF NOT EXISTS regions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    now = utcnow()
    for name in ['Thika', 'Chuka', 'Nyeri', 'Meru', 'Maua', 'Naivasha', 'Mirema']:
        existing = conn.execute("SELECT id FROM regions WHERE name = ?", (name,)).fetchone()
        if not existing:
            conn.execute("INSERT INTO regions (name, is_active, created_at) VALUES (?, 1, ?)", (name, now))


def _migration_0007_email_log_and_gmail_settings(conn):
    """Adds the email_log table (tracks every notification email attempt --
    sent/failed/skipped -- for the Settings > Notifications activity panel)
    and the gmail_* / email_notifications_enabled company_settings keys.
    Both were added to SCHEMA_STATEMENTS/defaults after migration 0001/0002
    already ran for existing installs, so those installs never picked them
    up -- this backfills them idempotently."""
    conn.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        row = conn.execute("SELECT id FROM company_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now)
            )


def _migration_0008_mpesa_transactions(conn):
    """Adds M-Pesa STK Push support: the mpesa_transactions table (tracks
    every push request from initiation through the Safaricom callback -- the
    Settings > M-Pesa activity panel and the frontend polling endpoint both
    read from this) and the mpesa_* company_settings keys used to configure
    Daraja credentials in-app."""
    conn.execute("""CREATE TABLE IF NOT EXISTS mpesa_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        row = conn.execute("SELECT id FROM company_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now)
            )


def _migration_0009_mpesa_b2c(conn):
    """Adds M-Pesa B2C (Business to Customer) support for loan disbursement:
    a disbursement_method column on loans, the extra correlation-ID columns
    B2C needs on mpesa_transactions (B2C uses OriginatorConversationID /
    ConversationID / TransactionID instead of STK's CheckoutRequestID), and
    the mpesa_initiator_*/mpesa_b2c_* company_settings keys."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(loans)").fetchall()]
    if 'disbursement_method' not in cols:
        conn.execute("ALTER TABLE loans ADD COLUMN disbursement_method TEXT DEFAULT 'cash'")

    mpesa_cols = [r[1] for r in conn.execute("PRAGMA table_info(mpesa_transactions)").fetchall()]
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
        row = conn.execute("SELECT id FROM company_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO company_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now)
            )


def _migration_0010_client_full_kyc_fields(conn):
    """Clients only had a subset of the KYC fields members have (missing
    middle_name, village, employer, monthly_income, next_of_kin_*) even
    though the registration/edit UI is meant to mirror members'. Brings the
    clients table up to the same shape. Guarded so it's a no-op on fresh
    installs that already get these columns straight from SCHEMA_STATEMENTS."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)")}
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if 'must_change_password' not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

    from core.auth import verify_password
    for row in conn.execute("SELECT id, password_hash FROM users WHERE username = 'admin'").fetchall():
        if verify_password('ChangeMe123!', row[1]):
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (row[0],))


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
            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
            (version, description, utcnow())
        )
        conn.commit()


def init_db(app):
    """Call once at app startup: resolves DB_PATH, opens a bootstrap
    connection, and runs any pending migrations."""
    path = app.config['DB_PATH']
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        run_migrations(conn)
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
