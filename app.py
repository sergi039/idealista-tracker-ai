import logging
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
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

def create_app(testing: bool = False):
    """Application factory.

    Side effects (DB create_all, scheduler start) are gated by config flags.
    """
    app = Flask(__name__)
    app.config.from_object(Config)
    if testing:
        app.config['TESTING'] = True

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

    app.secret_key = app.config.get("SESSION_SECRET")
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

    # Import and register routes
    from routes.main_routes import main_bp
    from routes.api_routes import api_bp
    from routes.language_routes import language_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(language_bp, url_prefix='/api')

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

__all__ = ["create_app", "db"]
