from datetime import datetime, timezone

import pytest

from app import create_app, db
from models import Land
from services.imap_service import IMAPService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def test_price_change_email_does_not_create_new_land(app, monkeypatch):
    """Legacy safety: price-change emails should only update existing lands, not create new ones."""
    with app.app_context():
        service = IMAPService()

        monkeypatch.setattr(
            service,
            "get_idealista_emails",
            lambda max_results=None: [
                {
                    "type": "price_change",
                    "source_email_id": "imap_1",
                    "email_received_at": datetime.now(timezone.utc),
                    "title": "Flat / apartment in calle Foo, Bar",
                    "url": "https://www.idealista.com/en/inmueble/123/",
                    "price": 285000.0,
                    "area": 85.0,
                    "municipality": "San Juan de Alicante",
                    "land_type": "buildable",
                    "idealista_property_id": 123,
                }
            ],
        )

        processed = service.run_ingestion(sync_type="incremental")
        assert processed == 0
        assert Land.query.count() == 0

