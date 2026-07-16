# 🏦 Jodala Microfinance Management System

A complete, production-ready microfinance loan management system built with Flask (Python), Bootstrap 5, SQLite, and JWT authentication.

---

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed the database (creates all tables + demo data)
python seed.py

# 3. Run the application
python run.py
```

Visit: **http://localhost:5000**

---

## 🔐 Demo Login Credentials

| Role         | Username      | Password      |
|--------------|---------------|---------------|
| Admin        | `admin`       | `Admin@2024`  |
| Loan Officer | `officer1`    | `Officer@2024`|
| Accountant   | `accountant1` | `Account@2024`|
| Cashier      | `cashier1`    | `Cashier@2024`|

---

## 📁 Project Structure

```
jodala/
├── run.py                    # Application entry point
├── seed.py                   # Database seeder with demo data
├── requirements.txt          # Python dependencies
├── .env                      # Environment configuration
│
├── app/
│   ├── __init__.py           # Flask app factory, extension setup
│   ├── models/
│   │   └── __init__.py       # All SQLAlchemy database models
│   ├── routes/
│   │   ├── auth.py           # Login, logout, profile, 2FA
│   │   ├── dashboard.py      # Stats, charts, activity feeds
│   │   ├── members.py        # Member CRUD + status management
│   │   ├── clients.py        # Business client management
│   │   ├── loans.py          # Full loan lifecycle management
│   │   ├── repayments.py     # Payment recording and receipts
│   │   ├── savings.py        # Savings accounts and transactions
│   │   ├── accounting.py     # Income, expenses, journal, trial balance
│   │   ├── reports.py        # Report generation + Excel exports
│   │   ├── other_routes.py   # Settings, notifications, documents, users
│   │   ├── settings.py       # (imports from other_routes)
│   │   ├── notifications.py  # (imports from other_routes)
│   │   ├── documents.py      # (imports from other_routes)
│   │   └── users.py          # (imports from other_routes)
│   └── utils/
│       └── __init__.py       # Helpers: ID generators, loan calculator, audit logger
│
├── static/
│   ├── css/main.css          # Full design system (dark/light mode, components)
│   └── js/app.js             # API client, sidebar, theme, toasts, helpers
│
└── templates/
    ├── base.html             # Sidebar layout, navbar, notifications
    ├── auth/
    │   ├── login.html        # Login with demo credential buttons
    │   └── profile.html      # Profile edit + 2FA setup
    ├── dashboard/
    │   └── index.html        # Stats, charts, due-today, overdue, activity
    ├── members/
    │   ├── index.html        # List with search/filter/pagination
    │   ├── register.html     # Registration form
    │   ├── detail.html       # Profile with loans and savings
    │   └── edit.html         # Edit form
    ├── clients/
    │   ├── index.html        # Client list
    │   ├── register.html     # Client registration
    │   └── detail.html       # Client profile with loans
    ├── loans/
    │   ├── index.html        # Loan list with quick approve/reject
    │   ├── apply.html        # Application form with live calculator
    │   └── detail.html       # Full loan detail, schedule, repayments
    ├── repayments/
    │   ├── index.html        # Transaction history
    │   ├── record.html       # Record payment with loan lookup
    │   └── receipt.html      # Printable receipt
    ├── savings/
    │   └── index.html        # Accounts + deposit/withdraw modals
    ├── accounting/
    │   ├── index.html        # P&L, income, expenses, journal, trial balance
    │   └── cashbook.html     # Daily cash receipts and payments
    ├── reports/
    │   └── index.html        # Loan, arrears, collection, member reports
    ├── settings/
    │   ├── index.html        # Company, loan products, savings products
    │   └── users.html        # User management + audit log
    ├── documents/
    │   └── index.html        # Generate PDFs, download Excel
    └── notifications/
        └── index.html        # Notification feed
```

---

## 🗄️ Database Models

| Model             | Description                                      |
|-------------------|--------------------------------------------------|
| `User`            | System users with roles and 2FA support          |
| `Member`          | Individual borrowers/savers                      |
| `Client`          | Business clients                                 |
| `LoanProduct`     | Loan product templates with rates & limits       |
| `Loan`            | Full loan lifecycle (pending → completed)        |
| `LoanSchedule`    | Generated repayment schedules per loan           |
| `Repayment`       | Individual payment transactions                  |
| `SavingsProduct`  | Savings account types                            |
| `SavingsAccount`  | Individual member savings accounts               |
| `SavingsTransaction` | Deposit/withdrawal/interest transactions      |
| `Account`         | Chart of accounts for double-entry bookkeeping  |
| `JournalEntry`    | Accounting journal entries                       |
| `JournalEntryLine`| Journal entry debit/credit lines                |
| `Income`          | Non-loan income records                          |
| `Expense`         | Operational expense records                      |
| `Notification`    | In-system user notifications                     |
| `AuditLog`        | Full audit trail of all system actions           |
| `CompanySettings` | Key-value company configuration store            |

---

## 🔌 API Endpoints

### Authentication
| Method | Endpoint              | Description              |
|--------|-----------------------|--------------------------|
| POST   | `/auth/login`         | Login (form or JSON)     |
| GET    | `/auth/logout`        | Logout                   |
| GET    | `/auth/me`            | Current user info        |
| PUT    | `/auth/profile`       | Update profile/password  |
| GET    | `/auth/setup-2fa`     | Get 2FA QR code          |
| POST   | `/auth/setup-2fa`     | Enable 2FA               |
| POST   | `/auth/disable-2fa`   | Disable 2FA              |

### Dashboard
| Method | Endpoint                                | Description              |
|--------|-----------------------------------------|--------------------------|
| GET    | `/dashboard/stats`                      | Summary statistics       |
| GET    | `/dashboard/loan-trend`                 | 12-month loan chart data |
| GET    | `/dashboard/loan-status-distribution`   | Doughnut chart data      |
| GET    | `/dashboard/due-today`                  | Payments due today       |
| GET    | `/dashboard/overdue-loans`              | Overdue loan list        |
| GET    | `/dashboard/recent-activities`          | Audit log feed           |
| GET    | `/dashboard/notifications`              | Unread notifications     |

### Members & Clients
| Method | Endpoint                         | Description           |
|--------|----------------------------------|-----------------------|
| GET    | `/members/api`                   | List (search/filter)  |
| POST   | `/members/api`                   | Create member         |
| GET    | `/members/api/<id>`              | Get member detail     |
| PUT    | `/members/api/<id>`              | Update member         |
| PUT    | `/members/api/<id>/status`       | Change status         |
| GET    | `/clients/api`                   | List clients          |
| POST   | `/clients/api`                   | Create client         |
| GET    | `/clients/api/<id>`              | Get client detail     |
| PUT    | `/clients/api/<id>`              | Update client         |

### Loans
| Method | Endpoint                          | Description            |
|--------|-----------------------------------|------------------------|
| GET    | `/loans/api`                      | List (search/filter)   |
| POST   | `/loans/api`                      | Apply for loan         |
| GET    | `/loans/api/<id>`                 | Loan detail + schedule |
| POST   | `/loans/api/<id>/approve`         | Approve application    |
| POST   | `/loans/api/<id>/reject`          | Reject application     |
| POST   | `/loans/api/<id>/disburse`        | Disburse loan          |
| POST   | `/loans/api/<id>/topup`           | Create top-up loan     |
| POST   | `/loans/api/<id>/extend`          | Extend loan term       |
| POST   | `/loans/api/<id>/write-off`       | Write off loan         |

### Repayments & Savings
| Method | Endpoint                          | Description            |
|--------|-----------------------------------|------------------------|
| GET    | `/repayments/api`                 | List repayments        |
| POST   | `/repayments/api`                 | Record repayment       |
| GET    | `/repayments/api/<id>`            | Get repayment          |
| GET    | `/savings/api/accounts`           | List accounts          |
| POST   | `/savings/api/accounts`           | Open account           |
| GET    | `/savings/api/accounts/<id>`      | Account + transactions |
| POST   | `/savings/api/deposit`            | Deposit                |
| POST   | `/savings/api/withdraw`           | Withdraw               |
| GET    | `/savings/api/transactions`       | List transactions      |

### Accounting & Reports
| Method | Endpoint                              | Description          |
|--------|---------------------------------------|----------------------|
| GET    | `/accounting/api/income`              | Income records       |
| POST   | `/accounting/api/income`              | Record income        |
| GET    | `/accounting/api/expenses`            | Expense records      |
| POST   | `/accounting/api/expenses`            | Record expense       |
| GET    | `/accounting/api/trial-balance`       | Trial balance        |
| GET    | `/accounting/api/profit-loss`         | P&L statement        |
| GET    | `/accounting/api/cashbook-data`       | Cashbook entries     |
| GET    | `/reports/api/loan-report`            | Loan report          |
| GET    | `/reports/api/arrears-report`         | Arrears/PAR report   |
| GET    | `/reports/api/collection-report`      | Collection report    |
| GET    | `/reports/api/member-report`          | Member report        |
| GET    | `/reports/api/export/loans/excel`     | Export loans XLSX    |
| GET    | `/reports/api/export/members/excel`   | Export members XLSX  |

### Documents (PDF)
| Method | Endpoint                                 | Description          |
|--------|------------------------------------------|----------------------|
| GET    | `/documents/loan-agreement/<id>`         | Loan agreement PDF   |
| GET    | `/documents/repayment-receipt/<id>`      | Receipt PDF          |

---

## ⚙️ Configuration (.env)

```ini
SECRET_KEY=change-in-production
JWT_SECRET_KEY=change-in-production
DATABASE_URL=sqlite:///jodala.db
FLASK_ENV=production
COMPANY_NAME=Jodala Microfinance
```

To use PostgreSQL:
```ini
DATABASE_URL=postgresql://user:password@localhost/jodala_db
```

---

## 🔒 Security Features

- **JWT authentication** via HttpOnly cookies
- **Role-based access control** (Admin, Loan Officer, Accountant, Cashier)
- **Two-factor authentication** (TOTP/Google Authenticator)
- **Password hashing** with bcrypt
- **Rate limiting** on sensitive endpoints
- **Full audit logging** of all system actions
- **SQL injection protection** via SQLAlchemy ORM
- **CORS** configured

---

## 🎨 UI Features

- **Dark/Light mode** toggle (persisted in localStorage)
- **Collapsible sidebar** (persisted in localStorage)
- **Mobile responsive** Bootstrap 5 layout
- **Toast notifications** for all actions
- **Real-time charts** (Chart.js) on dashboard
- **Pagination** on all data tables
- **Debounced search** for instant filtering
- **Loading states** on all async actions

---

## 📊 Loan Calculation

The system supports two interest calculation methods:

**Flat Rate:** `Interest = Principal × Rate × Term`

**Reducing Balance:** `Payment = P × r(1+r)^n / ((1+r)^n - 1)`

Repayment schedules are auto-generated on disbursement with daily/weekly/monthly installments.

---

## 🚀 Production Deployment

```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 "app:create_app()"
```

For Nginx + SSL, point your reverse proxy to `http://localhost:5000`.

---

*Built with ❤️ — Jodala Microfinance Management System v1.0*
#   j o d a l a  
 #   j o d a l a  
 