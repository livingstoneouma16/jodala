import os
import logging
from datetime import datetime
from flask import Flask
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from app.database import init_db, resolve_db_path

load_dotenv()

# Make sure INFO/WARNING/ERROR logs (including from app/mailer.py) actually
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

    # Configuration
    app.config['ENV_NAME'] = os.getenv('APP_ENV', 'development').strip().lower()
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
    app.config['DB_PATH'] = os.getenv('DB_PATH', resolve_db_path())
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'jwt-secret-key')

    # Refuse to boot with the insecure default secrets once this is a real
    # deployment (APP_ENV=production) -- these defaults exist purely so
    # `python run.py` works out of the box for local development; shipping
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
        from app.database import get_db
        try:
            rows = get_db().execute(
                "SELECT key, value FROM company_settings WHERE key IN ('company_name', 'logo_image')"
            ).fetchall()
            branding = {r['key']: r['value'] for r in rows}
        except Exception:
            branding = {}
        return {
            'company_name': branding.get('company_name') or 'Jodala Microfinance',
            'company_logo': branding.get('logo_image') or ''
        }

    # Initialize extensions
    # CORS: only allow-list origins explicitly configured via CORS_ALLOWED_ORIGINS
    # (comma-separated) in the environment. Previously this was `CORS(app)`
    # with no origins argument, which flask-cors treats as "*" -- i.e. any
    # website on the internet could make credentialed requests against this
    # API from a victim's browser. The server-rendered pages in this app
    # only ever call the API same-origin, so the safe default with nothing
    # configured is to allow NO cross-origin access at all; set
    # CORS_ALLOWED_ORIGINS if a separate frontend (e.g. a mobile app dev
    # server or the /v3 React build served from another host) needs it.
    _cors_origins = [o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()]
    CORS(app, origins=_cors_origins, supports_credentials=True)
    limiter.init_app(app)

    # Schema + migrations + first-run bootstrap
    init_db(app)

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.members import members_bp
    from app.routes.clients import clients_bp
    from app.routes.loans import loans_bp
    from app.routes.repayments import repayments_bp
    from app.routes.savings import savings_bp
    from app.routes.accounting import accounting_bp
    from app.routes.reports import reports_bp
    from app.routes.settings import settings_bp
    from app.routes.notifications import notifications_bp
    from app.routes.documents import documents_bp
    from app.routes.users import users_bp
    from app.routes.v3 import v3_bp
    from app.routes.mpesa import mpesa_bp

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
    app.register_blueprint(v3_bp, url_prefix='/v3')
    app.register_blueprint(mpesa_bp, url_prefix='/mpesa')

    # Root redirect
    from flask import redirect, url_for
    @app.route('/')
    def index():
        return redirect(url_for('auth.login'))

    # Health check for load balancers / uptime monitors / container
    # orchestrators (e.g. Docker HEALTHCHECK, k8s liveness probe). Does a
    # real (tiny) DB query rather than just returning 200 unconditionally,
    # so a broken DB file/connection actually shows up as unhealthy instead
    # of the app looking "up" while every real request 500s.
    @app.route('/health')
    def health():
        from flask import jsonify
        from app.database import get_db
        try:
            get_db().execute("SELECT 1").fetchone()
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'detail': str(e)}), 503

    return app
