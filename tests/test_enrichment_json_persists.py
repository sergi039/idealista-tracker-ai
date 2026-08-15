"""A write to `Property.enrichment` has to survive the commit.

`enrichment` is a plain `db.Column(JSON)`, so SQLAlchemy tracks assignment and
not mutation -- and `prop.enrichment or {}` returns the object already on the
instance, so mutating it and assigning it back is not a change. The value looks
right on the instance for the rest of the request and is gone from the row.

Measured on production 2026-08-15: a re-geocode of 168 rows wrote every scalar
column correctly and not one `enrichment["geocoding"]` record. The tool reads
that record to decide which rows still need work, so the next run would have
re-geocoded -- and re-paid for -- all 168 while calling itself resumable.

These tests therefore **commit and re-read from the database**. Asserting on
the in-memory instance is exactly what would have passed while the row stayed
unchanged; there is no point pinning this any other way.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _stored(prop_id):
    """Read the row back the way another process would see it."""
    db.session.expire_all()
    return db.session.get(Property, prop_id)


def _service(answer):
    service = PropertyLocationService()
    service.geocoding_service.geocode_address = lambda address: dict(
        answer, formatted_address=f"formatted:{address}"
    )
    return service


class TestTheGeocodingRecordReachesTheRow:
    def test_a_row_that_already_has_enrichment_still_records_the_geocode(self, app):
        """The case that failed in production: enrichment is already a dict."""
        with app.app_context():
            prop = Property(
                source_email_id="persist_existing",
                title="Chalet in Luarca",
                municipality="Luarca",
                enrichment={
                    "legacy_land": {"id": 7},
                    "environment": {"sea_view": "no"},
                },
            )
            db.session.add(prop)
            db.session.commit()

            service = _service({"lat": 43.54, "lng": -6.53, "accuracy": "approximate"})
            assert service.ensure_coordinates(prop, refresh=True) is True
            db.session.commit()

            stored = _stored(prop.id)
            assert stored.location_accuracy == "approximate"
            assert isinstance(stored.enrichment, dict)
            assert (
                stored.enrichment.get("geocoding", {}).get("accuracy") == "approximate"
            )

    def test_the_rest_of_the_column_survives(self, app):
        """One JSON column holds every enricher's output; none may be dropped."""
        with app.app_context():
            prop = Property(
                source_email_id="persist_keeps_siblings",
                title="Chalet in Luarca",
                municipality="Luarca",
                enrichment={
                    "legacy_land": {"id": 7},
                    "environment": {"sea_view": "likely"},
                    "quality_of_life": {"score": 42},
                },
            )
            db.session.add(prop)
            db.session.commit()

            _service(
                {"lat": 43.54, "lng": -6.53, "accuracy": "precise"}
            ).ensure_coordinates(prop, refresh=True)
            db.session.commit()

            stored = _stored(prop.id)
            assert stored.enrichment["legacy_land"] == {"id": 7}
            assert stored.enrichment["environment"] == {"sea_view": "likely"}
            assert stored.enrichment["quality_of_life"] == {"score": 42}
            assert stored.enrichment["geocoding"]["accuracy"] == "precise"

    def test_a_row_with_no_enrichment_at_all_also_records_it(self, app):
        """The path that always worked -- the `or {}` builds a new dict here."""
        with app.app_context():
            prop = Property(
                source_email_id="persist_fresh",
                title="Chalet in Luarca",
                municipality="Luarca",
            )
            db.session.add(prop)
            db.session.commit()

            _service(
                {"lat": 43.54, "lng": -6.53, "accuracy": "precise"}
            ).ensure_coordinates(prop)
            db.session.commit()

            assert _stored(prop.id).enrichment["geocoding"]["accuracy"] == "precise"

    def test_a_refresh_that_finds_nothing_still_clears_the_stale_record(self, app):
        """A failed refresh must not leave the previous geocode looking current."""
        with app.app_context():
            prop = Property(
                source_email_id="persist_cleared",
                title="Chalet in Luarca",
                municipality="Luarca",
                enrichment={"geocoding": {"query": "old", "accuracy": "precise"}},
            )
            db.session.add(prop)
            db.session.commit()

            service = PropertyLocationService()
            with patch.object(
                service.geocoding_service, "geocode_address", return_value=None
            ):
                assert service.ensure_coordinates(prop, refresh=True) is False
            db.session.commit()

            stored = _stored(prop.id)
            assert "geocoding" not in (stored.enrichment or {})
            assert stored.location_accuracy == "unknown"
