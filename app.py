import logging
import os
from datetime import timedelta
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config
from utils.log_redaction import install_log_redaction

# Set up logging - use INFO in production, DEBUG only when DEV_MODE is set
log_level = (
    logging.DEBUG if os.environ.get("DEV_MODE", "").lower() == "true" else logging.INFO
)
logging.basicConfig(level=log_level)

# The Google clients pass their API key as a query parameter, so it is part of
# every request URL - and urllib3 logs the full URL at DEBUG. Setting DEV_MODE,
# or any library that raises the level itself, is then enough to write the key
# into a log in plain text. Redact at the handler instead of relying on the
# level staying low.
install_log_redaction()

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
    if app_config.get("TESTING"):
        return

    errors = []

    # DATABASE_URL must be set and look like a valid URI
    db_url = app_config.get("DATABASE_URL") or app_config.get("SQLALCHEMY_DATABASE_URI")
    if not db_url:
        errors.append(
            "DATABASE_URL is not configured (set DATABASE_URL or DB_USER/DB_PASSWORD/DB_NAME)"
        )
    elif not db_url.startswith(("postgresql://", "sqlite://", "postgres://")):
        errors.append(
            f"DATABASE_URL has unexpected scheme: {db_url.split('://')[0] if '://' in db_url else db_url[:20]}"
        )

    # Scheduler timezone validation
    tz = getattr(Config, "SCHEDULER_TIMEZONE", None)
    if tz:
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(tz)
        except (KeyError, Exception):
            errors.append(f"SCHEDULER_TIMEZONE '{tz}' is not a valid IANA timezone")

    # Ingestion times format (HH:MM)
    for t_str in getattr(Config, "INGESTION_TIMES", []):
        parts = t_str.split(":")
        if len(parts) != 2:
            errors.append(f"INGESTION_TIMES entry '{t_str}' is not HH:MM format")
            continue
        try:
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                errors.append(
                    f"INGESTION_TIMES entry '{t_str}' has out-of-range hour/minute"
                )
        except ValueError:
            errors.append(f"INGESTION_TIMES entry '{t_str}' is not numeric")

    # Scoring profile weights should sum to ~1.0
    for profile_name in ("investment", "lifestyle"):
        weights = getattr(Config, "SCORING_PROFILES", {}).get(profile_name, {})
        if weights:
            total = sum(weights.values())
            if abs(total - 1.0) > 0.01:
                errors.append(
                    f"SCORING_PROFILES['{profile_name}'] weights sum to {total:.3f}, expected ~1.0"
                )

    if errors:
        msg = "Configuration validation failed:\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)


def _is_in_memory_sqlite(database_uri):
    """True for `sqlite://` / `sqlite:///:memory:`, i.e. a DB with no file.

    Used to keep pool settings that only make sense for a networked database
    away from a StaticPool whose single connection *is* the database.
    """
    if not database_uri:
        return False
    try:
        url = make_url(database_uri)
    except ArgumentError:
        return False
    return url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:")


def create_app(testing: bool = False):
    """Application factory.

    The deployment entrypoint applies SQL migrations before importing this
    factory. Scheduler startup remains gated by config and disabled in tests.
    """
    app = Flask(__name__)
    app.config.from_object(Config)
    if testing:
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

    # Refresh env-dependent config at runtime.
    dev_mode = os.environ.get("DEV_MODE", "").lower() == "true"
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        db_user = os.environ.get("DB_USER")
        db_password = os.environ.get("DB_PASSWORD")
        db_name = os.environ.get("DB_NAME")
        db_host = os.environ.get("DB_HOST", "localhost")
        db_port = os.environ.get("DB_PORT", "5432")
        if db_user and db_password and db_name:
            database_url = (
                f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
            )

    app.config.update(
        {
            "DEV_MODE": dev_mode,
            "DATABASE_URL": database_url,
            "SECRET_KEY": os.environ.get("SECRET_KEY"),
            "SESSION_SECRET": os.environ.get("SESSION_SECRET"),
            "AUTO_START_SCHEDULER": os.environ.get(
                "AUTO_START_SCHEDULER", "true" if dev_mode else "false"
            ).lower()
            == "true",
            # Issue #16: the Flask session cookie (used for the Flask-WTF
            # CSRF token, flash messages and language preference -- the
            # admin login itself was removed in #62) was never marked
            # Secure/SameSite, so it rode along on any plain-HTTP request
            # and was sent cross-site under browsers' Lax-by-default
            # heuristics alone. SameSite=Lax blocks it being attached to
            # cross-site POSTs; Secure is skipped only under DEV_MODE,
            # where the app is served over plain HTTP with no
            # TLS-terminating proxy in front of it.
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": not dev_mode,
        }
    )

    # Dev QoL: allow template changes to appear without restarting the server.
    # This matters when running under gunicorn inside Docker with bind mounts.
    if dev_mode and not app.config.get("TESTING", False):
        app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Fail-fast: validate critical configuration before anything else.
    _validate_config(app.config)

    # Security: Validate all required secrets before continuing.
    # In tests we allow missing required secrets.
    from utils.security import SecurityValidator

    raise_on_missing = not app.config.get("TESTING", False)
    security_results = SecurityValidator.validate_all_secrets(
        raise_on_missing_required=raise_on_missing
    )
    logger.info(
        "Security check passed: %s/%s optional secrets available",
        security_results["optional_available_count"],
        security_results["total_optional"],
    )

    secret = app.config.get("SESSION_SECRET")
    if not secret and not app.config.get("TESTING"):
        raise ValueError("SESSION_SECRET must be configured for session security")
    app.secret_key = secret or "testing-secret-key"
    app.permanent_session_lifetime = timedelta(days=30)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Configure the database.
    #
    # Note for anyone changing this: Flask-SQLAlchemy builds the engine inside
    # init_app() below, so SQLALCHEMY_DATABASE_URI has to be right *here*.
    # Reassigning it on a returned app does nothing.
    database_uri = app.config.get("DATABASE_URL")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    # Recycling exists for the production Postgres pool. Against an in-memory
    # SQLite database Flask-SQLAlchemy uses a StaticPool holding one
    # connection, and recycling that connection discards the database with it:
    # every table would silently vanish once an app outlived pool_recycle.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = (
        {}
        if _is_in_memory_sqlite(database_uri)
        else {
            "pool_recycle": 300,
            "pool_pre_ping": True,
        }
    )
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
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(language_bp, url_prefix="/api")

    # Exempt JSON API blueprints from CSRF (they use token/session auth, not form submissions)
    csrf.exempt(api_bp)
    csrf.exempt(language_bp)

    # Issue #140: those two blueprints answer JSON, so their errors must too.
    # Each registers its own HTTPException handler for what its views raise;
    # this one covers the errors raised before dispatch -- a URL matching no
    # rule (404), a rule rejecting the method (405) -- where there is no
    # blueprint to ask. It answers JSON only under /api and hands every other
    # request back to werkzeug's HTML error page.
    from utils.api_errors import http_error_response

    app.register_error_handler(HTTPException, http_error_response)

    # Initialize caching
    from utils.cache import init_cache

    init_cache(app)

    # Add localization functions to template context
    from utils.i18n import t, get_current_language

    app.jinja_env.globals["t"] = t
    app.jinja_env.globals["get_current_language"] = get_current_language

    # Every list row needs the sea-view verdict, and it is four states plus its
    # provenance rather than a column, so the templates read it through here.
    from services.sea_view_service import read_verdict as sea_view_verdict_for
    from services.sea_view_service import state_label_key as sea_view_state_key

    app.jinja_env.globals["sea_view_verdict_for"] = sea_view_verdict_for
    # `likely` names two different claims and only one of them is the listing's
    # (Selorio report, 2026-08-15), so the badge text comes from here rather
    # than from `state` — in all three places that draw it.
    app.jinja_env.globals["sea_view_state_key"] = sea_view_state_key

    # One Maps URL builder for every surface: list travel cells, detail rows,
    # beach lines. Templates used to concatenate free-text place names into
    # /maps/dir/ paths — unencoded, and resolvable to the wrong town.
    from utils.maps_urls import maps_directions_url, maps_place_url

    app.jinja_env.globals["maps_directions_url"] = maps_directions_url
    app.jinja_env.globals["maps_place_url"] = maps_place_url

    # Display-side cleanup of the Gmail-alert boilerplate in descriptions —
    # the raw column is never modified, and the card keeps "show original".
    from utils.description_display import clean_description_for_display

    app.jinja_env.globals["clean_description_for_display"] = (
        clean_description_for_display
    )

    with app.app_context():
        # Import models to ensure metadata is registered
        import models  # noqa: F401

        # Issue #176: reap any background_jobs row whose lease has expired --
        # e.g. the deploy watcher recreated the container on every new main
        # while a job was mid-flight. Safe to call from *any* create_app(),
        # including a one-shot utility script's own instance while the web
        # process is still alive (#190 review round 2, finding 3): a row's
        # lease is renewed by its own worker on every write
        # (services/background_jobs.py), so a row still genuinely in flight
        # is never touched here, no matter how many processes call this or
        # how often. Also a no-op when the table does not exist yet, which
        # covers most test fixtures: they call create_app() before
        # db.create_all(), while production's migration entrypoint always
        # creates the table first.
        from services.background_jobs import reconcile_orphaned_jobs

        interrupted = reconcile_orphaned_jobs()
        if interrupted:
            logger.warning(
                "Marked %d background job(s) as interrupted from a previous process",
                interrupted,
            )

        # Start the scheduler only after the migration entrypoint has completed.
        if app.config.get("AUTO_START_SCHEDULER", False) and not app.config.get(
            "TESTING", False
        ):
            from services.scheduler_service import init_scheduler

            init_scheduler(app)

    logger.info("Application initialized successfully")

    return app


__all__ = ["create_app", "db", "csrf", "limiter"]
