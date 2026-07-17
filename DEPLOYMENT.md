# Deploying Jodala Microfinance to production

This app ships as a normal Flask app with PostgreSQL storage. It's genuinely
deployable as-is for small-to-medium SACCO/chama usage, but the Flask dev
server it runs under locally (`python app.py`) is **not** suitable for
production -- this guide covers what changes and why.

## What's already handled for you

- `gunicorn.conf.py` -- production WSGI server config.
- `Dockerfile` + `entrypoint.sh` + `docker-compose.yml` -- containerized
  deploy with PostgreSQL and Redis, running as a non-root container.
- `render.yaml`, `railway.json`, `fly.toml` -- ready-to-use configs for
  each of those three platforms specifically (see step 7 below).
- `core/__init__.py` refuses to boot with `APP_ENV=production` set while
  `SECRET_KEY`/`JWT_SECRET_KEY` are still the insecure dev defaults.
- The auth cookie automatically becomes `Secure` (HTTPS-only) once
  `APP_ENV=production`.
- The seeded default admin account (and any user created with a temporary
  password) is forced to set a new password on first login -- it's not
  just a note in this doc that's easy to forget.
- `ProxyFix` middleware, so the app sees each visitor's real IP/scheme
  behind Render/Railway/Fly's reverse proxy instead of the proxy's own IP
  (this matters for per-client login rate limiting and for HTTPS URLs
  generated for M-Pesa callbacks).
- `GET /health` -- a real DB-backed health check for load balancers /
  container orchestrators.

## 1. Generate real secrets

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"   # JWT_SECRET_KEY (different value!)
```

Put these in `.env.production` (copy from `.env.production.example`). Never
commit the filled-in file -- `.gitignore`/`.dockerignore` already exclude it.

## 2. HTTPS is not optional

Three separate things in this app require it once you're in production:

- The auth cookie is marked `Secure` -- browsers silently drop it over
  plain HTTP, so login will appear broken.
- **M-Pesa STK Push and B2C callbacks** -- Safaricom must reach your
  `/mpesa/callback`, `/mpesa/b2c/result`, and `/mpesa/b2c/timeout` URLs over
  public HTTPS with a valid certificate, or payments/disbursements will sit
  on "pending" forever.
- Basic hygiene for anything handling financial data and login credentials.

Put a reverse proxy in front of gunicorn that terminates TLS -- nginx +
certbot (Let's Encrypt), Caddy (automatic HTTPS with zero config), or your
hosting platform's built-in TLS (Render, Railway, Fly.io, DigitalOcean App
Platform all handle this for you automatically).

## 3. Database: PostgreSQL

This app uses PostgreSQL, connected via a standard `DATABASE_URL` connection
string (`postgresql://user:password@host:5432/dbname`). Managed Postgres from
your host (Render's PostgreSQL, Railway's Postgres plugin, Fly Postgres,
Supabase, RDS, etc.) is the easiest way to run it in production -- you get
backups, connection pooling, and failover handled for you rather than
operating a database server yourself.

`docker-compose.yml` runs Postgres 16 in its own container with a named
volume (`jodala-postgres`) so data survives restarts. `render.yaml` and
`fly.toml` provision a managed Postgres instance and wire `DATABASE_URL` in
automatically -- see step 7 below. For local development without Docker,
create a `jodala` database (`createdb jodala`) and either set `DATABASE_URL`
or rely on the `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` defaults in
`core/database.py`.

Unlike the SQLite version this replaced, PostgreSQL handles concurrent
writers natively, so `gunicorn.conf.py`'s worker count isn't constrained by
the database and this app can run more than one instance/replica if needed.

## 4. Rate limiting needs Redis once you run >1 worker

`flask-limiter` (used on `/auth/login` and a few other sensitive endpoints)
defaults to in-memory storage, which is per-process. With more than one
gunicorn worker, each worker tracks login attempts independently, so the
real-world limit becomes N times weaker than configured, and every
restart/deploy resets all counters to zero.

`docker-compose.yml` already wires up a Redis container and points
`RATELIMIT_STORAGE_URI` at it. If you're not using that compose file, run
Redis yourself (a $5-10/mo managed Redis instance from your host is
plenty) and set `RATELIMIT_STORAGE_URI=redis://<host>:6379/0`.

## 5. Backups

The entire application state lives in the PostgreSQL database
(`DATABASE_URL`). There is **no automated backup built into the app** --
managed Postgres from Render/Railway/Fly/RDS/etc. typically includes
automated daily backups/point-in-time-recovery out of the box (check your
provider's plan), but if you're running Postgres yourself, set one up:

```bash
# Simple daily cron example (adjust connection details/destination):
0 2 * * * pg_dump "$DATABASE_URL" | gzip > /backups/jodala-$(date +\%F).sql.gz
```

Then ship `/backups` somewhere off the host (S3-compatible object storage,
rsync to another machine, etc.) and periodically actually test restoring
one -- an untested backup isn't a backup.

## 6. M-Pesa production checklist

Sandbox works out of the box for STK Push (public test shortcode/passkey
are built in) but B2C never has a shared default -- see Settings > M-Pesa
in-app for what B2C specifically needs. For **production** money movement:

1. On developer.safaricom.co.ke, create a production app and get it
   approved/"Go Live" (separate approval for B2C if you need disbursement).
2. Set `mpesa_environment = production` in Settings > M-Pesa (or
   `MPESA_ENVIRONMENT=production` env var).
3. Enter your **production** Consumer Key/Secret, Shortcode, Passkey.
4. For B2C: production Initiator Name/Password and the **production**
   certificate (different file from the sandbox one -- see the info box in
   Settings > M-Pesa for the exact download location).
5. Confirm your callback URLs (Settings > M-Pesa: Callback URL, Result URL,
   Queue Timeout URL) resolve to your real public HTTPS domain.
6. Send a real KSh 1 test push/payout before trusting it with real member
   money.

## 7. Deploy it

### Option A: Docker (recommended -- most portable)

```bash
cp .env.production.example .env.production   # fill in real values
docker compose --env-file .env.production up -d --build
```

Then put nginx/Caddy/your platform's load balancer in front of port 8000
for TLS termination.

### Option B: Render

`render.yaml` in this repo is a Blueprint that provisions the web service,
a managed PostgreSQL database, *and* a Redis instance together, wired up
correctly (`DATABASE_URL` pointed at Postgres and `RATELIMIT_STORAGE_URI`
pointed at Redis automatically, `SECRET_KEY`/`JWT_SECRET_KEY`
auto-generated).

1. Push this repo to GitHub/GitLab.
2. In the Render dashboard: **New > Blueprint**, select the repo. Render
   reads `render.yaml` and provisions both services.
3. When prompted (or afterwards, under the web service's
   **Environment** tab), fill in `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`,
   `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` -- or skip these and
   configure them from inside the app under Settings instead, either
   works.
4. Once deployed, note the `https://your-app.onrender.com` URL Render
   gives you and set it as the M-Pesa Callback/Result/Timeout URLs under
   Settings > M-Pesa in-app.

This web service has no attached disk, so it can be scaled to multiple
instances on Render if needed -- PostgreSQL handles concurrent writers
natively, unlike the SQLite setup this replaced.

### Option C: Railway

Railway's config-as-code (`railway.json`, included) covers build/health
check/restart settings, but **the Postgres and Redis add-ons are
dashboard/CLI steps, not something you can declare in the config file** --
do these once after the first deploy:

1. `railway init` (or link this repo via the dashboard: **New Project >
   Deploy from GitHub repo**). Railway detects the `Dockerfile`
   automatically.
2. Add Postgres: **+ New > Database > PostgreSQL** in the project canvas.
   This creates a `Postgres` service with its own `DATABASE_URL` variable.
3. Add Redis: **+ New > Database > Redis** in the project canvas. This
   creates a `Redis` service with its own `REDIS_URL` variable.
4. On the web service's **Variables** tab, set:
   ```
   APP_ENV=production
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   COOKIE_SECURE=true
   SECRET_KEY=<paste output of: python -c "import secrets; print(secrets.token_hex(32))">
   JWT_SECRET_KEY=<same command again, different value>
   RATELIMIT_STORAGE_URI=${{Redis.REDIS_URL}}
   ```
   (Railway doesn't auto-generate secrets the way Render does -- generate
   the two above yourself and paste them in.)
5. Under **Settings > Networking**, generate a public domain -- you'll
   need its `https://` URL for the M-Pesa callback settings, and Railway
   also exposes it to your app automatically as `RAILWAY_PUBLIC_DOMAIN`.

### Option D: Fly.io

`fly.toml` (included) has one placeholder you must change: `app = "..."`
needs to be a globally-unique name.

```bash
fly launch --no-deploy          # detects fly.toml, don't let it overwrite yours
fly postgres create --name jodala-db --region iad   # match fly.toml's primary_region
fly postgres attach jodala-db    # wires DATABASE_URL into this app's secrets
fly redis create                 # managed Upstash Redis -- note the redis:// URL printed
fly secrets set \
  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  RATELIMIT_STORAGE_URI="<the redis:// URL from `fly redis create`>"
fly deploy
```

`fly.toml` pins `min_machines_running = 1` and `auto_stop_machines = false`
deliberately -- Fly's usual scale-to-zero behavior would mean an M-Pesa
callback arriving while the Machine is stopped just fails. The database
itself runs as its own separate Fly Postgres app, so this web app has no
local volume and can scale past one Machine if you outgrow the default.
Once deployed, set the `https://your-app.fly.dev` URL as your M-Pesa
Callback/Result/Timeout URLs under Settings > M-Pesa in-app.

### Option E: A plain VPS with systemd + nginx (no Docker)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `/etc/systemd/system/jodala.service`:

```ini
[Unit]
Description=Jodala Microfinance
After=network.target

[Service]
Type=notify
User=jodala
WorkingDirectory=/opt/jodala
EnvironmentFile=/opt/jodala/.env.production
ExecStart=/opt/jodala/venv/bin/gunicorn -c gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`chmod 600 /opt/jodala/.env.production` (it holds secrets -- don't let
other users on the box read it), then `systemctl enable --now jodala`, and
put nginx in front of it as a reverse proxy to `127.0.0.1:8000` with
certbot for TLS. Run PostgreSQL and Redis on the same box (`apt install
postgresql redis-server`) or use managed instances and point
`DATABASE_URL`/`RATELIMIT_STORAGE_URI` at those instead.

## 8. After it's live

- Log in as the default admin (`admin` / whatever was seeded) -- you'll be
  **required** to set a new password before you can do anything else, and
  should turn on 2FA for the account afterwards under Settings > Profile.
- Set up the daily backup cron from step 5.
- Set `send_overdue_reminders.py` to actually run on a schedule (cron, or a
  platform's scheduled-job feature) -- it exists but doesn't run itself.
- Watch `GET /health` from an uptime monitor (UptimeRobot, Better Uptime,
  your platform's built-in one, etc.).

## 9. Installable as an app (PWA)

The app ships with a web app manifest and a service worker, so once it's
served over HTTPS (required for both) it's installable straight from the
browser -- no app-store build or review needed:

- **Android/desktop Chrome/Edge**: an install icon appears in the address
  bar, and the app's own top navbar shows a download button once the
  browser signals it's installable.
- **iOS Safari**: Share -> "Add to Home Screen" (Safari doesn't support
  the automatic install prompt, so there's no in-app button there).

What's cached for offline use is deliberately narrow -- only static
assets (CSS/JS/icons), never pages or `/api/` data, since this is a live
financial ledger and must never show stale account/loan/savings figures.
Losing connectivity mid-navigation shows a plain "you're offline, retry"
screen (`static/offline.html`) instead of a broken page.
