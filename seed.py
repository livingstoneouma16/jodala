"""
seed.py
-------
Sets up reference/catalog data needed for the app to function: loan and
savings products, and the extended chart of accounts. Safe to re-run --
each block only inserts rows that don't already exist.

No fake/demo members, clients, loans, or savings accounts are created —
those should come from real registrations. The very first admin user +
default settings + default chart of accounts are created automatically
on app startup by app/database.py's migrations.
"""
from app import create_app
from app.database import get_db, execute, utcnow
from app.auth import hash_password


def seed_database():
    app = create_app()

    with app.app_context():
        db = get_db()

        now = utcnow()

        print("Creating loan products...")
        loan_products = [
            ('Personal Emergency Loan', 'PEL', 'Quick personal loans for emergencies',
             100, 5000, 5.0, 'flat', 'monthly', 1, 12, 0.5, 2.0, 1.0, 0, 0),
            ('Business Development Loan', 'BDL', 'Loans for business expansion and development',
             1000, 50000, 4.0, 'reducing', 'monthly', 3, 36, 0.3, 1.5, 0.5, 1, 0),
            ('Agriculture Loan', 'AGL', 'Seasonal agricultural financing',
             500, 20000, 3.5, 'flat', 'monthly', 3, 18, 0.2, 1.0, 0.5, 0, 0),
            ('Salary Advance', 'SAL', 'Short-term salary-backed loans',
             100, 3000, 2.5, 'flat', 'monthly', 1, 6, 1.0, 0, 0, 0, 0),
            ('Group Loan', 'GRP', 'Loans for registered groups',
             500, 30000, 3.0, 'flat', 'monthly', 3, 24, 0.3, 1.0, 0.5, 0, 0),
        ]
        for (name, code, desc, min_amt, max_amt, rate, itype, freq, min_t, max_t,
             penalty, proc_fee, ins_fee, req_guarantor, req_collateral) in loan_products:
            existing = db.execute("SELECT id FROM loan_products WHERE code = ?", (code,)).fetchone()
            if not existing:
                execute(
                    """INSERT INTO loan_products (name, code, description, min_amount, max_amount,
                           interest_rate, interest_type, repayment_frequency, min_term, max_term,
                           penalty_rate, processing_fee, insurance_fee, require_guarantor,
                           require_collateral, is_active, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (name, code, desc, min_amt, max_amt, rate, itype, freq, min_t, max_t,
                     penalty, proc_fee, ins_fee, req_guarantor, req_collateral, now)
                )

        print("Creating savings products...")
        savings_products = [
            ('Regular Savings', 'RS001', 'Regular savings account', 3.0, 50),
            ('Fixed Deposit', 'FD001', 'Fixed deposit with higher returns', 8.0, 1000),
            ('Junior Savings', 'JS001', 'Savings account for children', 4.0, 10),
        ]
        for name, code, desc, rate, min_bal in savings_products:
            existing = db.execute("SELECT id FROM savings_products WHERE code = ?", (code,)).fetchone()
            if not existing:
                execute(
                    """INSERT INTO savings_products (name, code, description, interest_rate, min_balance, is_active, created_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (name, code, desc, rate, min_bal, now)
                )

        print("Extending chart of accounts...")
        extra_accounts = [
            ('1101', 'Cash in Hand', 'asset'), ('1102', 'Cash at Bank', 'asset'),
            ('1201', 'Active Loans - Principal', 'asset'), ('1300', 'Interest Receivable', 'asset'),
            ('2100', 'Member Savings Deposits', 'liability'), ('2200', 'Borrowings', 'liability'),
            ('3100', 'Share Capital', 'equity'), ('3200', 'Retained Earnings', 'equity'),
            ('4200', 'Processing Fees', 'income'), ('4300', 'Penalty Income', 'income'),
            ('4400', 'Other Income', 'income'), ('5100', 'Staff Salaries', 'expense'),
            ('5200', 'Rent and Utilities', 'expense'), ('5300', 'Office Supplies', 'expense'),
            ('5600', 'Loan Loss Provision', 'expense'), ('5700', 'Other Expenses', 'expense'),
        ]
        for code, name, acc_type in extra_accounts:
            existing = db.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()
            if not existing:
                execute("INSERT INTO accounts (code, name, account_type, balance, is_active) VALUES (?, ?, ?, 0, 1)",
                        (code, name, acc_type))

        print("Creating seed user...")
        existing_user = db.execute("SELECT id FROM users WHERE username = ?", ('Livow',)).fetchone()
        if not existing_user:
            execute(
                """INSERT INTO users (username, email, password_hash, full_name, role,
                       is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                ('Livow', 'livow@jodala.local', hash_password('Lee 1234'),
                 'Livow', 'admin', now, now)
            )
            print("  Created user: Livow (admin)")
        else:
            print("  User 'Livow' already exists, skipping.")

        print("\n" + "=" * 50)
        print("Setup complete!")
        print("=" * 50)
        print("\nSeed user credentials:")
        print("  Username : Livow")
        print("  Password : Lee 1234")
        print("=" * 50)


if __name__ == '__main__':
    seed_database()
