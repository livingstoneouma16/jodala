"""
Scheduled and on-demand database backups.

Runs `pg_dump` against the same connection string the app already uses
(core.database.resolve_db_url), writes a gzip-compressed SQL dump, prunes
old copies past a retention count, and logs the attempt to the `backups`
table (migration 24) so there's a visible history in Settings.

IMPORTANT CAVEAT -- read this before relying on this in production:
Render (and most PaaS hosts: Railway, Fly.io, Heroku) give the app an
EPHEMERAL filesystem. Anything written to local disk -- including
BACKUP_DIR below -- is wiped on every deploy and on every dyno/instance
restart. A local-only backup is therefore NOT durable storage; it's
useful for an immediate "let me grab a copy before I do something risky"
button, and as a local staging area before upload, but it must not be
your only copy.

To make backups actually durable, set S3_BACKUP_BUCKET (plus the usual
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION env vars,
or an attached IAM role) and install boto3. If boto3 isn't installed or
the bucket isn't configured, backups still run and are logged, just
flagged storage='local' instead of storage='s3' so this is visible at a
glance in Settings rather than silently assumed safe.

If you're instead relying on your Postgres provider's own managed backups
(Render's paid Postgres plans do include daily backups + point-in-time
recovery), this feature is a convenient supplement -- an easy manual
export and a visible log -- not a replacement for that.
"""
import gzip
import os
import shutil
import subprocess
from datetime import datetime, timezone

from core.database import execute, resolve_db_url, utcnow

BACKUP_DIR = os.getenv('BACKUP_DIR', os.path.join(os.getcwd(), 'backups'))
RETENTION = int(os.getenv('BACKUP_RETENTION', '7'))


def _s3_config():
    bucket = os.getenv('S3_BACKUP_BUCKET')
    if not bucket:
        return None
    try:
        import boto3  # noqa: F401 -- optional dependency, see module docstring
    except ImportError:
        return None
    return bucket


def _upload_to_s3(bucket, local_path, filename):
    import boto3
    client = boto3.client('s3')
    client.upload_file(local_path, bucket, f'jodala-backups/{filename}')


def _prune_old_backups():
    if not os.path.isdir(BACKUP_DIR):
        return
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith('jodala_backup_') and f.endswith('.sql.gz')),
        reverse=True,
    )
    for stale in files[RETENTION:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, stale))
        except OSError:
            pass


def run_backup(triggered_by='scheduled'):
    """Runs pg_dump, gzips the result, optionally uploads to S3, prunes old
    local copies, and logs the outcome. Always returns a dict describing
    what happened -- never raises, so a failed backup doesn't take down
    whatever called it (a scheduled job, or a request handler)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f'jodala_backup_{timestamp}.sql.gz'
    path = os.path.join(BACKUP_DIR, filename)

    if shutil.which('pg_dump') is None:
        error = 'pg_dump is not installed in this environment'
        execute(
            "INSERT INTO backups (filename, size_bytes, status, storage, error, triggered_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (filename, 0, 'failed', 'local', error, triggered_by, utcnow())
        )
        return {'status': 'failed', 'error': error}

    try:
        with gzip.open(path, 'wb') as gz_out:
            proc = subprocess.run(
                ['pg_dump', '--no-owner', '--no-privileges', resolve_db_url()],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            )
            gz_out.write(proc.stdout)
    except subprocess.CalledProcessError as e:
        error = (e.stderr or b'').decode(errors='replace')[:2000] or 'pg_dump exited with an error'
        if os.path.exists(path):
            os.remove(path)
        execute(
            "INSERT INTO backups (filename, size_bytes, status, storage, error, triggered_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (filename, 0, 'failed', 'local', error, triggered_by, utcnow())
        )
        return {'status': 'failed', 'error': error}

    size_bytes = os.path.getsize(path)
    storage = 'local'
    error = None

    bucket = _s3_config()
    if bucket:
        try:
            _upload_to_s3(bucket, path, filename)
            storage = 's3'
        except Exception as e:  # noqa: BLE001 -- any S3 failure just falls back to local-only, doesn't fail the backup
            error = f'Backup saved locally, but S3 upload failed: {e}'

    _prune_old_backups()

    execute(
        "INSERT INTO backups (filename, size_bytes, status, storage, error, triggered_by, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (filename, size_bytes, 'success', storage, error, triggered_by, utcnow())
    )
    return {'status': 'success', 'filename': filename, 'size_bytes': size_bytes, 'storage': storage, 'error': error}


def local_backup_path(filename):
    """Returns the local path for a backup filename if it still exists on
    this instance's disk (won't survive a restart/redeploy -- see module
    docstring), else None."""
    safe_name = os.path.basename(filename)
    path = os.path.join(BACKUP_DIR, safe_name)
    return path if os.path.isfile(path) else None
