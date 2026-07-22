"""
Gunicorn config for production. Usage:

    gunicorn -c gunicorn.conf.py app:app

All values can be overridden via environment variables so the same config
file works unchanged across dev/staging/prod containers.

This app's database is PostgreSQL (core/database.py, via DATABASE_URL) --
unlike the SQLite setup this project started from, Postgres has no
single-writer lock, so it's fine to raise WEB_CONCURRENCY as real traffic
demands it. It defaults to 2 here anyway, since that's plenty for
small-to-medium SACCO/chama traffic (a handful of staff using the app at
once) and keeps the connection-pool footprint against jodala-db small by
default -- see core/database.py's ThreadedConnectionPool maxconn, which
should be raised alongside WEB_CONCURRENCY if you do scale workers up.
"""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

# Deliberately NOT defaulting to (2 * cpu_count) + 1 the way gunicorn docs
# usually suggest -- that formula assumes CPU-bound work, and this app is
# mostly waiting on Postgres/network I/O per request. 2 workers x 4 threads
# comfortably covers small-to-medium SACCO/chama traffic; raise
# WEB_CONCURRENCY (and the connection pool's maxconn in core/database.py)
# if you outgrow it.
workers = int(os.getenv('WEB_CONCURRENCY', '2'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
worker_class = 'gthread'

timeout = int(os.getenv('GUNICORN_TIMEOUT', '30'))
graceful_timeout = 30
keepalive = 5

# NOTE: app.py's daily overdue-reminder scheduler (core/scheduler.py) relies
# on preload_app=True below -- it means app.py (and its top-level
# start_scheduler(app) call) runs exactly once in the master process before
# forking workers, rather than once per worker. Don't flip this to False
# without also moving the scheduler start into a gunicorn `post_fork`/
# `when_ready` hook, or every worker will run its own copy of the job and
# reminder emails will go out duplicated (once per worker).
accesslog = '-'   # stdout -- let the platform/container runtime collect it
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')

# Restart workers periodically to shed any slow memory growth; jitter avoids
# every worker recycling at the exact same moment.
max_requests = 2000
max_requests_jitter = 200

preload_app = True
