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
    # The limiter rides the same REDIS_URL switch as the cache (#356), and
    # until that variable existed its storage could not fail: `memory://` is a
    # dict. Redis can refuse, and flask-limiter checks the limit *inside* the
    # request, where only HTTPException is handled - so a stopped Redis took
    # the 15 rate-limited routes in routes/api_routes.py out entirely.
    # Measured before this was added: with Redis up the route answered; with
    # `docker stop` on Redis the same call raised ConnectionError out of the
    # request. Those routes are the AI analysis, the bulk and manual
    # enrichment, the email ingest and the status check - the buttons someone
    # presses when something is already wrong.
    #
    # in_memory_fallback_enabled keeps enforcing the limit in this process
    # while the shared storage is unreachable, so an outage relaxes the limit
    # to per-process rather than removing it. swallow_errors makes a storage
    # failure cost the *limit*, never the request.
    #
    # This is the cache's rule one step out: utils/cache.py got the outage
    # guard for this switch and the limiter, flipped by the same variable, had
    # none. A guard correct inside the scope it was written for.
    in_memory_fallback_enabled=True,
    swallow_errors=True,
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
    factory. This factory does **not** start the scheduler: every `utils/*`
    backfill builds an app through it, and one built inside a
    `docker compose run` container would otherwise take the scheduler over.
    `main.py` starts it, via `should_start_scheduler` (issue #333).
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
    from utils.i18n import t, tn, get_current_language

    app.jinja_env.globals["t"] = t
    app.jinja_env.globals["tn"] = tn
    app.jinja_env.globals["get_current_language"] = get_current_language

    # Every list row needs the sea-view verdict, and it is four states plus its
    # provenance rather than a column, so the templates read it through here.
    from services.sea_view_service import read_verdict as sea_view_verdict_for
    from services.sea_view_service import state_label_key as sea_view_state_key

    app.jinja_env.globals["sea_view_verdict_for"] = sea_view_verdict_for
    # `likely` names two different claims and only one of them is the listing's
    # (#334), so the badge text comes from here rather than from `state` — in
    # all three places that draw it.
    app.jinja_env.globals["sea_view_state_key"] = sea_view_state_key

    # Whether a row's derived numbers are about the property at all. Both read
    # `location_accuracy`, so a template can show a locality centroid's travel
    # times and sea distance as what they are instead of as measurements of the
    # parcel. Passed as globals for the same reason the verdict above is: every
    # list row needs them, and the rule has one home in Python.
    from services.property_travel_service import (
        effective_travel_state as travel_state_for,
    )
    from services.sea_distance_service import parcel_measurement as sea_distance_for

    app.jinja_env.globals["travel_state_for"] = travel_state_for
    app.jinja_env.globals["sea_distance_for"] = sea_distance_for

    # `listing_status` defaults to 'active' at ingestion and nothing verified
    # that default, so no template may render the column directly: they read
    # the verdict, which has a fourth state for "never checked".
    from services.listing_verification import read_verdict as listing_verdict_for

    app.jinja_env.globals["listing_verdict_for"] = listing_verdict_for

    # Which site a listing is on, derived from its URL. The templates get the
    # same two functions the filter clause is built from, so a badge cannot
    # say one thing while the dropdown beside it counts another.
    from utils.listing_source import source_label, source_of

    app.jinja_env.globals["listing_source_for"] = source_of
    app.jinja_env.globals["listing_source_label"] = source_label

    # Who is selling: the owner, or an agency. Four states, most of the answers
    # derived from the alert link the row already carries, and the badge, the
    # filter and its counts all read this one function for the same reason the
    # source badge above does.
    from services.advertiser import from_portal_type as advertiser_state_from_portal
    from services.advertiser import read_verdict as advertiser_verdict_for

    app.jinja_env.globals["advertiser_verdict_for"] = advertiser_verdict_for
    app.jinja_env.globals["advertiser_state_from_portal"] = advertiser_state_from_portal

    # What is nearby that a buyer would walk away over (#437). Restated on
    # read against the row's *current* accuracy, because a centroid cannot
    # support "1.1 km" -- so the badge, the property card and the CSV all
    # take the same reading rather than three of them parsing the block.
    from services.hazard_service import read_verdict as hazard_verdict_for

    app.jinja_env.globals["hazard_verdict_for"] = hazard_verdict_for
    # What the owner decided, and what is still outstanding. Two readings, and
    # the second one takes the date: `overdue` is a due date compared against
    # today, and a template that let each row compute its own today would
    # disagree with the query that selected the rows, once a day, at midnight
    # in Madrid. Every list passes `review_today` explicitly
    # (services/owner_review.py).
    from services.owner_review import action_label_key, decision_label_key
    from services.owner_review import read_action as owner_action_for
    from services.owner_review import read_decision as owner_decision_for

    app.jinja_env.globals["owner_decision_for"] = owner_decision_for
    app.jinja_env.globals["owner_action_for"] = owner_action_for
    app.jinja_env.globals["owner_decision_label_key"] = decision_label_key
    app.jinja_env.globals["owner_action_label_key"] = action_label_key

    from services.owner_review import was_edited as owner_review_was_edited

    app.jinja_env.globals["owner_review_was_edited"] = owner_review_was_edited

    from services.attachments import human_size as attachment_size

    app.jinja_env.globals["attachment_size"] = attachment_size

    # #379: how much of the enabled weight a score rests on, read off the
    # stored payload (derived for rows scored before it was recorded). The
    # list and the detail page show it; the score itself never contains it.
    from services.property_scoring_service import score_coverage

    app.jinja_env.globals["score_coverage_for"] = score_coverage

    # #376 follow-up: the Manual Sync button is offered only on the machine that
    # ingests. The endpoint refuses on its own (routes/api_routes.py) — this is
    # the second reader of the same rule so the navbar does not present a control
    # that always answers 409.
    from services.ingest_policy import machine_is_ingester

    app.jinja_env.globals["machine_is_ingester"] = machine_is_ingester

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

        # The scheduler is NOT started here. It belongs to the web process and
        # is started by main.py, the module gunicorn loads -- see
        # `should_start_scheduler` below for why (issue #333).

    logger.info("Application initialized successfully")

    return app


def should_start_scheduler(app) -> bool:
    """Whether this process is the one entitled to own the scheduler.

    `create_app()` used to start it itself, gated only on
    `AUTO_START_SCHEDULER`. That flag is set on the compose *service*
    (`docker-compose.yml`: `AUTO_START_SCHEDULER=${AUTO_START_SCHEDULER:-true}`),
    so every container built from that service inherits it -- including the
    throwaway one from `docker compose run --rm app python -m utils.<backfill>`,
    whose whole job is a backfill.

    What stopped that being obvious is that the guard inside `init_scheduler`
    is an `flock` on a file in `tempfile.gettempdir()`, i.e. the *container's
    own* `/tmp`. `docker exec idealista-app ...` shares that filesystem with the
    running app and correctly logs "another scheduler instance is already
    running"; a separate container has its own `/tmp`, finds no lock, and takes
    it. The guard works within a container and silently does not work between
    containers -- so the safe-looking case was safe by accident.

    Measured on the mini, 2026-08-15: `docker compose run --rm app python -m
    utils.backfill_sea_view` logged `Acquired scheduler lock (PID: 1)`, then
    scheduled IMAP ingestion, the daily listing-status check and the lease
    reconciliation, and ran the last of those 45 times in an 18-minute run. The
    ingestion and status jobs did not fire only because the window missed their
    clock times. A run straddling 19:00 would have ingested mail from a
    throwaway container, and one straddling 10:00 would have started the
    idealista status scrape -- the one thing in this repository that is
    deliberately throttled.

    Keeping long jobs out of `idealista-app` is the owner's standing decision,
    because a deploy recreates that container and kills whatever runs inside it.
    So the two safe practices collided: the safe place to run a backfill was the
    place that stole the scheduler. The fix is to stop inferring the answer.
    `main.py` is the module gunicorn loads and the only entry point that serves
    HTTP; nothing under `utils/` imports it. Asking there, and only there, is
    what makes the intent explicit rather than environmental.

    The flock stays as a second line of defence for two workers inside one
    container. It is not this decision.
    """
    return bool(app.config.get("AUTO_START_SCHEDULER", False)) and not app.config.get(
        "TESTING", False
    )


__all__ = ["create_app", "db", "csrf", "limiter"]
