"""
Test package for Idealista Tracker AI application.
"""

import os
import sys
import logging
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.http import OVERPASS_GATE  # noqa: E402  (needs the path above)

# Set up test logging
logging.basicConfig(
    level=logging.WARNING,  # Reduce noise in tests
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Test configuration
#
# The database URL must be in-memory, and it must reach create_app() through
# the environment. Flask-SQLAlchemy 3.x builds the engine inside init_app(),
# which create_app() calls, so a fixture that assigns
# app.config["SQLALCHEMY_DATABASE_URI"] afterwards changes nothing: the engine
# is already bound. That is how this suite spent its life sharing one on-disk
# sqlite file (instance/test.db) across every module, with isolation resting
# entirely on db.drop_all() in fixture teardown.
# tests/test_db_engine_isolation.py guards both halves of that.
TEST_DATABASE_URL = "sqlite:///:memory:"
TEST_GMAIL_API_KEY = "test_gmail_key"
TEST_GOOGLE_MAPS_API_KEY = "test_maps_key"
TEST_GOOGLE_PLACES_API_KEY = "test_places_key"

# Applied at package import, i.e. before pytest imports conftest.py or any test
# module. setup_test_environment() is called by most fixtures but not all, and
# a module that forgets it would otherwise inherit whatever DATABASE_URL the
# developer's shell exports -- possibly a real database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Overpass is mocked in every suite, so the two-second interval the shared gate
# keeps against the live instance would only add real seconds to a run that
# never reaches it. The gate's own behaviour is tested directly, by setting an
# interval and a clock -- see tests/test_issue_152_property_osm_amenities.py.
OVERPASS_GATE.min_interval_s = 0.0


def setup_test_environment():
    """Set up test environment variables"""
    os.environ.update(
        {
            "DATABASE_URL": TEST_DATABASE_URL,
            "GMAIL_API_KEY": TEST_GMAIL_API_KEY,
            "GOOGLE_MAPS_API_KEY": TEST_GOOGLE_MAPS_API_KEY,
            "GOOGLE_PLACES_API_KEY": TEST_GOOGLE_PLACES_API_KEY,
            "SECRET_KEY": "test-secret-key",
            "SESSION_SECRET": "test-session-secret",
        }
    )
