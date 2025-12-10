"""
Test package for Idealista Land Watch & Rank application.
"""

import os
import sys
import logging
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set up test logging
logging.basicConfig(
    level=logging.WARNING,  # Reduce noise in tests
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Test configuration
TEST_DATABASE_URL = "sqlite:///test.db"
TEST_GMAIL_API_KEY = "test_gmail_key"
TEST_GOOGLE_MAPS_API_KEY = "test_maps_key"
TEST_GOOGLE_PLACES_API_KEY = "test_places_key"

def setup_test_environment():
    """Set up test environment variables"""
    os.environ.update({
        'DATABASE_URL': TEST_DATABASE_URL,
        'GMAIL_API_KEY': TEST_GMAIL_API_KEY,
        'GOOGLE_MAPS_API_KEY': TEST_GOOGLE_MAPS_API_KEY,
        'GOOGLE_PLACES_API_KEY': TEST_GOOGLE_PLACES_API_KEY,
        'SECRET_KEY': 'test-secret-key',
        'SESSION_SECRET': 'test-session-secret'
    })
