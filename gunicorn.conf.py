"""
Gunicorn config for production. Usage:

    gunicorn -c gunicorn.conf.py app:app

All values can be overridden via environment variables so the same config
file works unchanged across dev/staging/prod containers.

IMPORTANT (SQLite): this app's default database is SQLite, which handles
concurrent *writes* poorly -- multiple worker processes hammering the same
.db file under real write concurrency will occasionally hit "database is
locked" errors. WEB_CONCURRENCY defaults to 2, which is fine for small-to-
medium SACCO/chama traffic (a handful of staff using the app at once). If
you outgrow that, migrate to PostgreSQL rather than just adding more
workers -- more SQLite workers doesn't actually buy you more write
throughput, only more contention.
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

# Deliberately NOT defaulting to (2 * cpu_count) + 1 the way gunicorn docs
# usually suggest -- that formula assumes a CPU-bound app with no shared
# single-writer datastore. For this app, more workers just means more
# processes contending for the same SQLite file lock. Keep this low unless
# you've migrated to Postgres.
workers = int(os.getenv('WEB_CONCURRENCY', '2'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
worker_class = 'gthread'

timeout = int(os.getenv('GUNICORN_TIMEOUT', '30'))
graceful_timeout = 30
keepalive = 5

accesslog = '-'   # stdout -- let the platform/container runtime collect it
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')

# Restart workers periodically to shed any slow memory growth; jitter avoids
# every worker recycling at the exact same moment.
max_requests = 2000
max_requests_jitter = 200

preload_app = True
