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


def test_property_ingestion_dedups_within_profile_but_allows_cross_profile_duplicates(
    app, monkeypatch
):
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        profile_a = SearchProfile(
            name="Profile A",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        profile_b = SearchProfile(
            name="Profile B",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([profile_a, profile_b])
        db.session.commit()

        listing_url = "https://www.idealista.com/inmueble/123456/"

        emails = [
            {
                "type": "listing",
                "source_email_id": "imap_1",
                "url": listing_url,
                "idealista_property_id": 123456,
                "search_profile_id": profile_a.id,
                "title": "Listing A",
                "price": 100000,
                "area": 50,
            },
            # Same Idealista listing in a different SearchProfile should create a new Property row.
            {
                "type": "listing",
                "source_email_id": "imap_2",
                "url": listing_url,
                "idealista_property_id": 123456,
                "search_profile_id": profile_b.id,
                "title": "Listing B",
                "price": 100000,
                "area": 50,
            },
            # Same listing in Profile A again should update price instead of creating a new row.
            {
                "type": "price_change",
                "source_email_id": "imap_3",
                "url": listing_url,
                "idealista_property_id": 123456,
                "search_profile_id": profile_a.id,
                "title": "Listing A",
                "price": 90000,
                "previous_price_hint": 100000,
                "area": 50,
            },
        ]

        service = PropertyIMAPService()
        monkeypatch.setattr(
            service, "get_idealista_emails", lambda max_results=None: list(emails)
        )

        created = service.run_ingestion(sync_type="test")
        assert created == 2

        prop_a = Property.query.filter_by(
            idealista_property_id=123456, search_profile_id=profile_a.id
        ).first()
        prop_b = Property.query.filter_by(
            idealista_property_id=123456, search_profile_id=profile_b.id
        ).first()
        assert prop_a is not None
        assert prop_b is not None
        assert float(prop_a.price) == 90000.0
        assert float(prop_b.price) == 100000.0
