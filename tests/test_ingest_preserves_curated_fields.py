"""Re-ingesting a listing must not erase what a human recorded about the row.

Nothing pinned this, and a growing amount now rests on it. Roughly 320 plot
rows carry hand-recorded `attributes` — the land classification and its
source, `needs_urbanistic_check`, the price-per-m² outlier flag, the PGOU zone
warning, a geocoding-failure note — and a curated `search_profile_id` that
groups the ones whose verdict survived measurement. None of that can be
recovered from an email; the emails never had it.

The behaviour is already correct: `run_ingestion` finds an existing row, edits
only the price fields, and `continue`s before reaching the block that assigns
`attributes` and `search_profile_id` (that block builds a *new* `Property`).
These tests exist so a later "simplify the ingest into one upsert" cannot take
it away silently — the failure mode would be several hundred rows quietly
losing their curation, with no error and nothing in a log to notice.

The third test pins the consequence that *is* real and is by design: a plain
listing email resolving to a different profile does not match the curated row
and creates its own, per issue #25 and `test_property_profile_dedup.py`. The
curated row keeps everything; the duplicate is the cost of one listing
belonging to two saved searches.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment

INTERNAL_DATE = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
LISTING_ID = 880001
URL = f"https://www.idealista.com/inmueble/{LISTING_ID}/"

# What a human recorded, and what no email can reconstruct.
CURATED = {
    "plot_m2": 2453,
    "land_classification": "urbano_solar",
    "classification_source": "idealista card: Urbano (solar)",
    "needs_urbanistic_check": True,
    "price_per_m2_outlier": "3.2 EUR/m2 — bottom decile",
    "pgou_zone_warning": "Pillarno is not among the PGOU urban zones",
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False
        db.create_all()
        yield app
        db.drop_all()


def _profiles():
    """`curated` is where a human filed the row; `inbound` is what mail resolves to."""
    curated = SearchProfile(
        name="Plots vetted",
        is_active=True,
        is_default=False,
        travel_targets={"presets": {}, "custom": []},
    )
    inbound = SearchProfile(
        name="Autocreated from the saved search",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add_all([curated, inbound])
    db.session.commit()
    return curated, inbound


def _seed(profile_id):
    prop = Property(
        source_email_id=f"manual:vetted:{LISTING_ID}",
        idealista_property_id=LISTING_ID,
        search_profile_id=profile_id,
        url=URL,
        title="Lugar Soto de Luiña, 61, Cudillero",
        deal_type="sale",
        property_category="land",
        property_subtype="plot",
        price=Decimal("39000.00"),
        area=Decimal("2453.00"),
        area_type="plot",
        listing_status="active",
        attributes=dict(CURATED),
    )
    db.session.add(prop)
    db.session.commit()
    return prop.id


def _ingest(monkeypatch, emails):
    service = PropertyIMAPService()
    monkeypatch.setattr(
        service, "get_idealista_emails", lambda max_results=None: list(emails)
    )
    service.run_ingestion(sync_type="test")


class TestCuratedFieldsSurviveReIngestion:
    def test_a_price_change_updates_the_price_and_nothing_else(self, app, monkeypatch):
        """The one email kind that legitimately edits an existing row."""
        with app.app_context():
            curated, inbound = _profiles()
            row_id = _seed(curated.id)

            _ingest(
                monkeypatch,
                [
                    {
                        "type": "price_change",
                        "source_email_id": f"imap_price_{LISTING_ID}",
                        "email_received_at": INTERNAL_DATE,
                        "url": URL,
                        "idealista_property_id": LISTING_ID,
                        # Resolved from the alert, not from where the row lives.
                        "search_profile_id": inbound.id,
                        "title": "Lugar Soto de Luiña, 61, Cudillero",
                        "price": 35000.0,
                        "previous_price_hint": 39000.0,
                        "area": 2453,
                        # An email carries only what it can parse.
                        "attributes": {"bedrooms": 0},
                    }
                ],
            )

            row = db.session.get(Property, row_id)
            assert float(row.price) == 35000.0, "the price change must land"
            assert row.attributes == CURATED, "curation must survive the update"
            assert row.search_profile_id == curated.id, "the row must not be refiled"

    def test_a_listing_email_for_the_same_profile_changes_nothing(
        self, app, monkeypatch
    ):
        """A repeat listing email finds the row and leaves it entirely alone."""
        with app.app_context():
            curated, _inbound = _profiles()
            row_id = _seed(curated.id)

            _ingest(
                monkeypatch,
                [
                    {
                        "source_email_id": f"imap_listing_{LISTING_ID}",
                        "email_received_at": INTERNAL_DATE,
                        "url": URL,
                        "idealista_property_id": LISTING_ID,
                        "search_profile_id": curated.id,
                        "title": "Lugar Soto de Luiña, 61, Cudillero",
                        "price": 39000.0,
                        "area": 2453,
                        "property_category": "land",
                        "property_subtype": "plot",
                        "attributes": {"bedrooms": 0},
                    }
                ],
            )

            assert Property.query.count() == 1, "no duplicate under the same profile"
            row = db.session.get(Property, row_id)
            assert row.attributes == CURATED
            assert row.search_profile_id == curated.id

    def test_a_listing_email_for_another_profile_leaves_the_curated_row_intact(
        self, app, monkeypatch
    ):
        """By design (#25): a second saved search gets its own row.

        The point being pinned is not the duplicate — that is the documented
        trade-off — but that the curated row is not the one edited.
        """
        with app.app_context():
            curated, inbound = _profiles()
            row_id = _seed(curated.id)

            _ingest(
                monkeypatch,
                [
                    {
                        "source_email_id": f"imap_listing_other_{LISTING_ID}",
                        "email_received_at": INTERNAL_DATE,
                        "url": URL,
                        "idealista_property_id": LISTING_ID,
                        "search_profile_id": inbound.id,
                        "title": "Lugar Soto de Luiña, 61, Cudillero",
                        "price": 39000.0,
                        "area": 2453,
                        "property_category": "land",
                        "property_subtype": "plot",
                        "attributes": {"bedrooms": 0},
                    }
                ],
            )

            row = db.session.get(Property, row_id)
            assert row.attributes == CURATED, "the curated row keeps its curation"
            assert row.search_profile_id == curated.id, "and keeps its subscription"

            others = Property.query.filter(Property.id != row_id).all()
            assert len(others) == 1, "the second saved search gets its own row (#25)"
            assert others[0].search_profile_id == inbound.id
