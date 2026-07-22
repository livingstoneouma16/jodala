"""
permissions.py
--------------
Registry of fine-grained, role-configurable permissions.

Each entry in PERMISSIONS is a (module, label) pair keyed by a stable
permission_key. These keys are what's stored in the `role_permissions`
table (see database.py migration 0016) and checked by
core.auth.permission_required.

DEFAULT_ROLE_PERMISSIONS captures exactly what the hardcoded
@role_required(...) decorators used to allow, before this system existed --
migration 0016 seeds the table from this dict so upgrading an existing
install doesn't silently change anyone's access. From then on, an admin can
change these freely from Settings > Permissions.

The 'admin' role is always treated as fully permitted (see
core.auth.permission_required) regardless of what's stored, so admins can
never accidentally lock themselves out.
"""

# Roles a permission can be granted to (admin is intentionally excluded --
# it's always-allowed, see note above, and isn't shown as a toggle in the UI).
CONFIGURABLE_ROLES = ('loan_officer', 'accountant', 'cashier')

ALL_ROLES = ('admin', 'loan_officer', 'accountant', 'cashier')

# permission_key -> (module, label)
PERMISSIONS = {
    # Members
    'members.delete':                    ('Members', 'Delete members'),

    # Clients
    'clients.delete':                    ('Clients', 'Delete clients'),

    # Loans
    'loans.approve':                     ('Loans', 'Approve loan applications'),
    'loans.restructure':                 ('Loans', 'Restructure loans (re-negotiate term/rate)'),
    'loans.write_off':                   ('Loans', 'Write off loans'),
    'loans.delete':                      ('Loans', 'Delete loans'),
    'loans.send_overdue_reminders':      ('Loans', 'Manually trigger overdue-reminder emails'),

    # Company settings
    'settings.company.update':           ('Company Settings', 'Edit company profile'),
    'settings.company.main_account':     ('Company Settings', 'Add funds to main account'),

    # Notification settings
    'settings.notifications.view':       ('Notification Settings', 'View email settings'),
    'settings.notifications.update':     ('Notification Settings', 'Edit email settings'),
    'settings.notifications.log':        ('Notification Settings', 'View email activity log'),
    'settings.notifications.test':       ('Notification Settings', 'Send test emails'),

    # M-Pesa settings
    'settings.mpesa.view':               ('M-Pesa Settings', 'View M-Pesa settings'),
    'settings.mpesa.update':             ('M-Pesa Settings', 'Edit M-Pesa settings'),
    'settings.mpesa.log':                ('M-Pesa Settings', 'View M-Pesa activity log'),
    'settings.mpesa.test':               ('M-Pesa Settings', 'Send test STK push'),
    'settings.mpesa.test_b2c':           ('M-Pesa Settings', 'Send test B2C payout'),

    # Loan products
    'settings.loan_products.create':     ('Loan Products', 'Create loan products'),
    'settings.loan_products.update':     ('Loan Products', 'Edit loan products'),
    'settings.loan_products.delete':     ('Loan Products', 'Delete loan products'),

    # Savings products
    'settings.savings_products.create':  ('Savings Products', 'Create savings products'),
    'settings.savings_products.update':  ('Savings Products', 'Edit savings products'),
    'settings.savings_products.delete':  ('Savings Products', 'Delete savings products'),

    # Regions
    'settings.regions.create':           ('Regions', 'Create regions'),
    'settings.regions.update':           ('Regions', 'Edit regions'),
    'settings.regions.delete':           ('Regions', 'Delete regions'),

    # User management
    'users.view':                        ('User Management', 'View user list'),
    'users.create':                      ('User Management', 'Create users'),
    'users.delete':                      ('User Management', 'Delete users'),
    'users.audit_logs':                  ('User Management', 'View audit logs'),
}

# What each non-admin role could do before this system existed -- mirrors
# the old @role_required(...) allow-lists exactly. Any key not listed here
# for a role defaults to False.
DEFAULT_ROLE_PERMISSIONS = {
    'loan_officer': {
        'loans.approve',
        'loans.restructure',
    },
    'accountant': set(),
    'cashier': set(),
}


def module_groups():
    """Returns an ordered dict-like list of (module_name, [(key, label), ...])
    in registration order, for rendering the permissions matrix."""
    groups = []
    seen = {}
    for key, (module, label) in PERMISSIONS.items():
        if module not in seen:
            seen[module] = []
            groups.append((module, seen[module]))
        seen[module].append((key, label))
    return groups
