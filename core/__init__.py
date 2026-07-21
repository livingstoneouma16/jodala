import os
import logging
from datetime import datetime
from flask import Flask
from flask_cors import CORS
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

from core.database import init_db, resolve_db_url

load_dotenv()

# Make sure INFO/WARNING/ERROR logs (including from core/mailer.py) actually
# print somewhere -- without this, Python's logging defaults to only
# showing WARNING+ with no timestamps, which makes diagnosing "why didn't
# this email send" much harder than it needs to be.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

limiter = Limiter(key_func=get_remote_address)


def fmtdate(value, fmt='%d %b %Y', default='—'):
    """Jinja filter: format a date/datetime, or an ISO date/datetime string
    coming straight out of SQLite (where all date columns are stored as
    TEXT), into a human-readable string. Falsy values render as `default`."""
    if not value:
        return default
    if hasattr(value, 'strftime'):
        return value.strftime(fmt)
    try:
        return datetime.fromisoformat(str(value)).strftime(fmt)
    except (ValueError, TypeError):
        return str(value)


def create_app():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__,
                template_folder=os.path.join(base_dir, 'templates'),
                static_folder=os.path.join(base_dir, 'static'))

    # Render, Railway, and Fly.io all sit their own reverse proxy in front of
    # this app -- without ProxyFix, every request's `remote_addr` is the
    # platform's proxy IP, not the real client's. That silently breaks two
    # things: (1) flask-limiter's get_remote_address() buckets every visitor
    # into the *same* rate-limit counter instead of one per client, so a
    # handful of unrelated users logging in around the same time could trip
    # each other's login rate limit; (2) url_for(..., _external=True) (used
    # for e.g. M-Pesa callback URLs) can resolve to http:// instead of
    # https:// without knowing the original scheme. x_for/x_proto/x_host=1
    # trusts exactly one hop of X-Forwarded-* headers, matching each of
    # these platforms' single edge proxy -- do not increase this unless you
    # add another proxy of your own in front of them.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Configuration
    app.config['ENV_NAME'] = os.getenv('APP_ENV', 'development').strip().lower()
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
    app.config['DATABASE_URL'] = resolve_db_url()
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'jwt-secret-key')

    # Refuse to boot with the insecure default secrets once this is a real
    # deployment (APP_ENV=production) -- these defaults exist purely so
    # `python app.py` works out of the box for local development; shipping
    # them to production would mean anyone can forge a login session or
    # auth token. Generate real ones with:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    if app.config['ENV_NAME'] == 'production':
        insecure = []
        if app.config['SECRET_KEY'] == 'dev-secret-key':
            insecure.append('SECRET_KEY')
        if app.config['JWT_SECRET_KEY'] == 'jwt-secret-key':
            insecure.append('JWT_SECRET_KEY')
        if insecure:
            raise RuntimeError(
                f"Refusing to start with APP_ENV=production while using the default "
                f"development value for: {', '.join(insecure)}. Set real random secrets "
                f"in the environment (see .env.example)."
            )
    # Explicitly set the rate-limiter storage backend. Without this,
    # flask-limiter falls back to an in-memory store and prints a
    # UserWarning on every startup. Set RATELIMIT_STORAGE_URI in the
    # environment (e.g. to a Redis URL like "redis://localhost:6379")
    # for production/multi-process deployments; defaults to the same
    # in-memory backend for local/dev use, just declared explicitly so
    # the warning no longer fires.
    app.config['RATELIMIT_STORAGE_URI'] = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')
    if app.config['ENV_NAME'] == 'production' and app.config['RATELIMIT_STORAGE_URI'] == 'memory://':
        logging.getLogger('jodala').warning(
            'RATELIMIT_STORAGE_URI is not set -- rate limits (e.g. on /auth/login) are tracked '
            'per-process. With more than one gunicorn worker this makes the effective limit '
            'N times more permissive, and counters reset on every restart/deploy. Set '
            'RATELIMIT_STORAGE_URI to a shared Redis URL for correct multi-worker rate limiting.'
        )
    # Whether the auth cookie should be marked Secure (only ever sent over
    # HTTPS). Defaults to on automatically once APP_ENV=production (a
    # production deployment not behind HTTPS is a bigger problem than this
    # flag alone); set COOKIE_SECURE=false explicitly to opt back out (e.g.
    # briefly, while debugging behind a non-TLS load balancer -- not
    # recommended). Off by default outside production so plain-HTTP local
    # dev still works (browsers silently drop Secure cookies over HTTP).
    _cookie_secure_default = 'true' if app.config['ENV_NAME'] == 'production' else 'false'
    app.config['COOKIE_SECURE'] = os.getenv('COOKIE_SECURE', _cookie_secure_default).strip().lower() in ('1', 'true', 'yes')

    # Jinja filters
    app.jinja_env.filters['fmtdate'] = fmtdate

    # Make the SACCO/chama branding (name + uploaded logo) available in
    # every template -- this is what the sidebar and login page display,
    # so uploading a logo in Settings actually shows up across the app.
    @app.context_processor
    def inject_company_branding():
        # Cached (core/database.py:get_company_branding, TTL + explicit
        # invalidation on Settings > Company save) instead of querying
        # company_settings on every single template render -- this runs on
        # nearly every request.
        from core.database import get_company_branding
        return get_company_branding()

    # Static assets are served straight from this app with no reverse proxy
    # in front on any of the supported deploy targets (docker-compose.yml
    # says so explicitly; Render/Railway/Fly point straight at this
    # container) -- so without the two settings below, every page load ships
    # ~600KB of vendor CSS/JS (bootstrap, bootstrap-icons, chart.js) fully
    # uncompressed, and the browser re-validates every static file with the
    # server (a full round trip, even if it ends in a 304) on every visit
    # instead of using its disk cache.
    #
    # SEND_FILE_MAX_AGE_DEFAULT lets the browser skip that round trip for
    # STATIC_CACHE_MAX_AGE seconds (default 1 week) instead of re-checking
    # every time. This is safe for the vendor libraries (versioned by
    # directory name, e.g. vendor/bootstrap/, so a version bump changes the
    # path) but does mean a same-path edit to static/css/main.css or
    # static/js/app.js can take up to that long to reach a browser that
    # already cached it -- bump the version query string on those
    # `url_for('static', ...)` calls (or lower STATIC_CACHE_MAX_AGE) if a
    # hotfix to either file needs to land immediately.
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = int(os.getenv('STATIC_CACHE_MAX_AGE', str(7 * 24 * 60 * 60)))
    Compress(app)

    # Initialize extensions
    # CORS: only allow-list origins explicitly configured via CORS_ALLOWED_ORIGINS
    # (comma-separated) in the environment. Previously this was `CORS(app)`
    # with no origins argument, which flask-cors treats as "*" -- i.e. any
    # website on the internet could make credentialed requests against this
    # API from a victim's browser. The server-rendered pages in this app
    # only ever call the API same-origin, so the safe default with nothing
    # configured is to allow NO cross-origin access at all; set
    # CORS_ALLOWED_ORIGINS if a separate frontend (e.g. a mobile app dev
    # server) needs it.
    _cors_origins = [o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()]
    CORS(app, origins=_cors_origins, supports_credentials=True)
    limiter.init_app(app)

    # Schema + migrations + first-run bootstrap
    init_db(app)

    # Register blueprints
    from core.routes.auth import auth_bp
    from core.routes.dashboard import dashboard_bp
    from core.routes.members import members_bp
    from core.routes.clients import clients_bp
    from core.routes.loans import loans_bp
    from core.routes.repayments import repayments_bp
    from core.routes.savings import savings_bp
    from core.routes.accounting import accounting_bp
    from core.routes.reports import reports_bp
    from core.routes.settings import settings_bp
    from core.routes.notifications import notifications_bp
    from core.routes.documents import documents_bp
    from core.routes.users import users_bp

    from core.routes.mpesa import mpesa_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(members_bp, url_prefix='/members')
    app.register_blueprint(clients_bp, url_prefix='/clients')
    app.register_blueprint(loans_bp, url_prefix='/loans')
    app.register_blueprint(repayments_bp, url_prefix='/repayments')
    app.register_blueprint(savings_bp, url_prefix='/savings')
    app.register_blueprint(accounting_bp, url_prefix='/accounting')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    app.register_blueprint(notifications_bp, url_prefix='/notifications')
    app.register_blueprint(documents_bp, url_prefix='/documents')
    app.register_blueprint(users_bp, url_prefix='/users')
    app.register_blueprint(mpesa_bp, url_prefix='/mpesa')

    # Root redirect
    from flask import redirect, url_for
    @app.route('/')
    def index():
        return redirect(url_for('auth.login'))

    # Service workers can only control pages *under* the URL path they're
    # served from (unless the server adds a Service-Worker-Allowed header) --
    # serving it from /static/sw.js would cap it to controlling /static/*,
    # which is useless for a PWA that needs to intercept page navigations
    # app-wide. Serving it from the root path instead gives it the whole
    # origin as its scope with no extra headers needed.
    @app.route('/sw.js')
    def service_worker():
        from flask import send_from_directory
        return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')

    # Security headers on every response. This app doesn't use a cookie-only
    # auth pattern for its mutating routes (see core/routes/auth.py + app.js:
    # the JWT is sent as an explicit Authorization header, not read ambiently
    # from the cookie), so CSRF tokens aren't the gap here -- but clickjacking
    # (X-Frame-Options), MIME-sniffing (X-Content-Type-Options), and protocol
    # downgrade (HSTS) are still open without these. CSP is scoped to what the
    # app actually loads (Bootstrap/Bootstrap Icons/Google Fonts from
    # cdnjs.cloudflare.com and fonts.googleapis.com/fonts.gstatic.com, see
    # base.html) rather than a blanket allow-all.
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers.setdefault('Content-Security-Policy', (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        ))
        # HSTS only makes sense once we're actually being served over HTTPS --
        # sending it over plain HTTP (e.g. local dev) does nothing useful and
        # risks locking a dev browser into https:// for localhost.
        if app.config['COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    # Health check for load balancers / uptime monitors / container
    # orchestrators (e.g. Docker HEALTHCHECK, k8s liveness probe). Does a
    # real (tiny) DB query rather than just returning 200 unconditionally,
    # so a broken DB file/connection actually shows up as unhealthy instead
    # of the app looking "up" while every real request 500s.
    @app.route('/health')
    def health():
        from flask import jsonify
        from core.database import get_db
        try:
            get_db().execute("SELECT 1").fetchone()
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'detail': str(e)}), 503

    return app
