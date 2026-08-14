"""AI provenance is timezone-honest and never guessed (proposal D9).

`PropertyAiAnalysisVariant.created_at` is naive UTC; serialized without an
offset, JS `new Date()` reads it as browser-local time, shifting the shown
date and the 30-day stale cutoff (Phase-1 diff review, 2026-08-13). The
route therefore serializes with an explicit +00:00. A property without a
variant row gets nulls — "not recorded", never an invented date.
"""

from datetime import datetime

import pytest

from app import create_app, db
from models import Property, PropertyAiAnalysisVariant
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


@pytest.fixture
def client(app):
    return app.test_client()


def _add_property(app, with_variant):
    with app.app_context():
        prop = Property(
            source_email_id="provenance-fixture",
            title="ProvenanceFixture",
            municipality="Navia",
            location_lat=43.54,
            location_lon=-6.72,
        )
        db.session.add(prop)
        db.session.commit()
        if with_variant:
            db.session.add(
                PropertyAiAnalysisVariant(
                    property_id=prop.id,
                    provider="claude",
                    model="claude-test-model",
                    analysis={"price_analysis": {"verdict": "FAIR_PRICE"}},
                    created_at=datetime(2026, 7, 14, 1, 30, 0),  # naive UTC
                )
            )
            db.session.commit()
        return prop.id


def test_variant_date_carries_an_explicit_utc_offset(app, client):
    pid = _add_property(app, with_variant=True)
    body = client.get(f"/properties/{pid}").get_data(as_text=True)

    assert '"2026-07-14T01:30:00+00:00"' in body, (
        "an offset-less ISO string is parsed as browser-local time"
    )
    assert '"claude-test-model"' in body


def test_no_variant_means_null_meta_not_a_guess(app, client):
    pid = _add_property(app, with_variant=False)
    body = client.get(f"/properties/{pid}").get_data(as_text=True)

    assert "window.__CLAUDE_ANALYSIS_META__ = { model: null, dateIso: null }" in body
