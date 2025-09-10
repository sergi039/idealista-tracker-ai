import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

def create_app():
    # Create the app
    app = Flask(__name__)
    # Security: Require SESSION_SECRET to be set - no fallback for production
    app.secret_key = os.environ.get("SESSION_SECRET")
    if not app.secret_key:
        raise ValueError("SESSION_SECRET environment variable must be set in Replit Secrets")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Configure the database
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Initialize the app with the extension
    db.init_app(app)

    with app.app_context():
        # Import models to ensure tables are created
        import models  # noqa: F401
        
        # Import and register routes
        from routes.main_routes import main_bp
        from routes.api_routes import api_bp
        
        app.register_blueprint(main_bp)
        app.register_blueprint(api_bp, url_prefix='/api')
        
        # Create all tables
        db.create_all()
        
        # Initialize scheduler
        from services.scheduler_service import init_scheduler
        init_scheduler(app)
        
        logger.info("Application initialized successfully")

    return app

# Create the app instance
app = create_app()
