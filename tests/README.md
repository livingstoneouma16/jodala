# Running the test suite

```bash
pip install -r requirements-dev.txt
pytest
```

## Two kinds of tests here

- **`test_calculator.py`** -- pure functions (loan schedule/penalty/payment
  math), no database needed at all.
- **`test_auth.py` / `test_loans.py` / `test_repayments.py` /
  `test_savings.py` / `test_mpesa.py`** -- exercise the real Flask routes
  against a real (throwaway) PostgreSQL database via `core.database`'s own
  migrations, not a hand-maintained schema copy or an in-memory fake. This
  is deliberate: the bugs most worth catching in loan/repayment/savings
  code live in SQL and transaction boundaries, which a fake DB layer can't
  faithfully reproduce.

## One-time setup: a local Postgres binary

`conftest.py` uses `pytest-postgresql`'s `postgresql_proc` fixture, which
starts and stops a **throwaway** PostgreSQL server for the test session --
you don't need to `createdb` anything or point it at your dev database. It
does need an actual `postgres`/`pg_ctl`/`initdb` binary installed and on
`PATH` to do that, though (it doesn't ship one). If you don't already have
Postgres installed locally:

```bash
# Debian/Ubuntu
sudo apt-get install postgresql

# macOS (Homebrew)
brew install postgresql@16
```

If your Postgres binaries live somewhere not on `PATH` (common on macOS
Homebrew installs), point pytest-postgresql at them, e.g. in `pytest.ini`:

```ini
[pytest]
postgresql_exec = /opt/homebrew/opt/postgresql@16/bin/pg_ctl
```

If you'd rather run tests against a Postgres server you already have
running (e.g. inside `docker-compose up db`) instead of spinning up a new
one per test session, swap `conftest.py`'s `postgresql_proc`/`postgresql`
factories for `pytest_postgresql.factories.postgresql_noproc(...)` pointed
at that server -- see the
[pytest-postgresql docs](https://pytest-postgresql.readthedocs.io/) for the
exact fixture signature.

## What's still not covered

This adds coverage for the highest-risk money paths (auth gating, loan
application/approval/disbursement state machine, repayment allocation,
savings deposit/withdrawal + minimum-balance enforcement, and the M-Pesa
STK push + callback flow, including the "callback delivered twice" and
"malformed callback" edge cases Safaricom is documented to actually do).
Not yet covered: accounting/ledger routes, reports, clients, documents,
notifications, settings, and the B2C (disbursement-via-M-Pesa) flow in
`core/mpesa.py`/`core/routes/mpesa.py` (`initiate_b2c_payment`,
`b2c_result`, `b2c_timeout`) -- worth a follow-up pass, since B2C moves
money out of the SACCO the same way STK push moves it in.
