import logging
import os
from datetime import timedelta
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config

# Set up logging - use INFO in production, DEBUG only when DEV_MODE is set
log_level = logging.DEBUG if os.environ.get('DEV_MODE', '').lower() == 'true' else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("REDIS_URL", "memory://"),
    default_limits=[],  # No global default; limits applied per-endpoint
)


def _validate_config(app_config):
    """Fail-fast validation of critical configuration at startup.

    Raises ValueError with a clear message listing every problem found.
    Skipped when TESTING is True.
    """
    if app_config.get('TESTING'):
        return

    errors = []

    # DATABASE_URL must be set and look like a valid URI
    db_url = app_config.get('DATABASE_URL') or app_config.get('SQLALCHEMY_DATABASE_URI')
    if not db_url:
        errors.append("DATABASE_URL is not configured (set DATABASE_URL or DB_USER/DB_PASSWORD/DB_NAME)")
    elif not db_url.startswith(('postgresql://', 'sqlite://', 'postgres://')):
        errors.append(f"DATABASE_URL has unexpected scheme: {db_url.split('://')[0] if '://' in db_url else db_url[:20]}")

    # Scheduler timezone validation
    tz = getattr(Config, 'SCHEDULER_TIMEZONE', None)
    if tz:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(tz)
        except (KeyError, Exception):
            errors.append(f"SCHEDULER_TIMEZONE '{tz}' is not a valid IANA timezone")

    # Ingestion times format (HH:MM)
    for t_str in getattr(Config, 'INGESTION_TIMES', []):
        parts = t_str.split(':')
        if len(parts) != 2:
            errors.append(f"INGESTION_TIMES entry '{t_str}' is not HH:MM format")
            continue
        try:
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                errors.append(f"INGESTION_TIMES entry '{t_str}' has out-of-range hour/minute")
        except ValueError:
            errors.append(f"INGESTION_TIMES entry '{t_str}' is not numeric")

    # Scoring profile weights should sum to ~1.0
    for profile_name in ('investment', 'lifestyle'):
        weights = getattr(Config, 'SCORING_PROFILES', {}).get(profile_name, {})
        if weights:
            total = sum(weights.values())
            if abs(total - 1.0) > 0.01:
                errors.append(f"SCORING_PROFILES['{profile_name}'] weights sum to {total:.3f}, expected ~1.0")

    if errors:
        msg = "Configuration validation failed:\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)


def create_app(testing: bool = False):
    """Application factory.

    Side effects (DB create_all, scheduler start) are gated by config flags.
    """
    app = Flask(__name__)
    app.config.from_object(Config)
    if testing:
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False

    # Refresh env-dependent config at runtime.
    dev_mode = os.environ.get('DEV_MODE', '').lower() == 'true'
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        db_user = os.environ.get('DB_USER')
        db_password = os.environ.get('DB_PASSWORD')
        db_name = os.environ.get('DB_NAME')
        db_host = os.environ.get('DB_HOST', 'localhost')
        db_port = os.environ.get('DB_PORT', '5432')
        if db_user and db_password and db_name:
            database_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    app.config.update({
        'DEV_MODE': dev_mode,
        'DATABASE_URL': database_url,
        'SECRET_KEY': os.environ.get('SECRET_KEY'),
        'SESSION_SECRET': os.environ.get('SESSION_SECRET'),
        'AUTO_CREATE_DB': os.environ.get("AUTO_CREATE_DB", "true" if dev_mode else "false").lower() == "true",
        'AUTO_START_SCHEDULER': os.environ.get("AUTO_START_SCHEDULER", "true" if dev_mode else "false").lower() == "true",
    })

    # Dev QoL: allow template changes to appear without restarting the server.
    # This matters when running under gunicorn inside Docker with bind mounts.
    if dev_mode and not app.config.get('TESTING', False):
        app.config['TEMPLATES_AUTO_RELOAD'] = True

    # Fail-fast: validate critical configuration before anything else.
    _validate_config(app.config)

    # Security: Validate all required secrets before continuing.
    # In tests we allow missing required secrets.
    from utils.security import SecurityValidator

    raise_on_missing = not app.config.get('TESTING', False)
    security_results = SecurityValidator.validate_all_secrets(raise_on_missing_required=raise_on_missing)
    logger.info(
        "Security check passed: %s/%s optional secrets available",
        security_results['optional_available_count'],
        security_results['total_optional'],
    )

    secret = app.config.get("SESSION_SECRET")
    if not secret and not app.config.get('TESTING'):
        raise ValueError("SESSION_SECRET must be configured for session security")
    app.secret_key = secret or 'testing-secret-key'
    app.permanent_session_lifetime = timedelta(days=30)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Configure the database
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config.get("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Initialize the app with the extension
    db.init_app(app)

    # CSRF protection for form-based POST requests
    csrf.init_app(app)

    # Rate limiting (Redis when REDIS_URL set, in-memory fallback)
    limiter.init_app(app)

    # Import and register routes
    from routes.main_routes import main_bp
    from routes.api_routes import api_bp
    from routes.language_routes import language_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(language_bp, url_prefix='/api')

    # Exempt JSON API blueprints from CSRF (they use token/session auth, not form submissions)
    csrf.exempt(api_bp)
    csrf.exempt(language_bp)

    # Initialize caching
    from utils.cache import init_cache
    init_cache(app)

    # Add localization functions to template context
    from utils.i18n import t, get_current_language
    app.jinja_env.globals['t'] = t
    app.jinja_env.globals['get_current_language'] = get_current_language

    with app.app_context():
        # Import models to ensure metadata is registered
        import models  # noqa: F401

        # Optional dev convenience: auto-create tables
        if app.config.get('AUTO_CREATE_DB', False):
            db.create_all()

        # Optional: start background scheduler
        if app.config.get('AUTO_START_SCHEDULER', False) and not app.config.get('TESTING', False):
            from services.scheduler_service import init_scheduler
            init_scheduler(app)

    logger.info("Application initialized successfully")

    return app

__all__ = ["create_app", "db", "csrf", "limiter"]
