import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def test_sale_only_skips_rent_ingestion(app, monkeypatch):
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        profile = SearchProfile(
            name="Default",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        emails = [
            {
                "type": "listing",
                "source_email_id": "imap_1",
                "url": "https://www.idealista.com/inmueble/999/",
                "idealista_property_id": 999,
                "search_profile_id": profile.id,
                "deal_type": "rent",
                "title": "Rental listing",
                "price": 1200,
                "area": 60,
            }
        ]

        service = PropertyIMAPService()
        monkeypatch.setattr(
            service, "get_idealista_emails", lambda max_results=None: list(emails)
        )

        Config.SALE_ONLY = True
        created = service.run_ingestion(sync_type="test")
        assert created == 0
        assert Property.query.count() == 0

        Config.SALE_ONLY = False
        created2 = service.run_ingestion(sync_type="test")
        assert created2 == 1
        prop = Property.query.first()
        assert prop is not None
        assert prop.deal_type == "rent"
