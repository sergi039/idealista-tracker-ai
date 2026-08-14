"""The to-beach sort is live again (issue #271; #98 placeholder retired).

The owner approved enabling it once rows hold measured beach times, which
the Phase-2 backfill provides. The sort key is the nearest (first) beach's
drive minutes; rows without a measurement sort last, in both directions —
an unmeasured row must never pass for the closest-to-the-sea one.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


def _beaches(minutes):
    return {
        "targets": {},
        "api_status": {"state": "ok"},
        "beaches": {
            "status": "ok",
            "max_drive_min": 20,
            "items": [
                {
                    "name": f"Playa {minutes}",
                    "duration_min": minutes,
                    "lat": 43.5,
                    "lon": -6.8,
                }
            ],
        },
    }


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for email_id, travel in (
            ("far-beach", _beaches(18)),
            ("near-beach", _beaches(4)),
            ("no-beach-data", {"targets": {}, "api_status": {"state": "ok"}}),
        ):
            db.session.add(
                Property(
                    source_email_id=email_id,
                    title=f"Title-{email_id}",
                    municipality="Navia",
                    search_profile_id=profile.id,
                    listing_status="active",
                    location_lat=43.54,
                    location_lon=-6.72,
                    travel=travel,
                )
            )
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _row_order(body):
    rows = [
        (body.index(marker), marker)
        for marker in ("Title-near-beach", "Title-far-beach", "Title-no-beach-data")
    ]
    return [marker for _, marker in sorted(rows)]


class TestBeachSort:
    def test_the_option_is_no_longer_disabled(self, client):
        body = client.get("/properties").get_data(as_text=True)
        assert 'value="travel_time_nearest_beach" disabled' not in body
        assert "unavailable (#98)" not in body
        assert 'value="travel_time_nearest_beach"' in body

    def test_ascending_puts_the_nearest_beach_first_unmeasured_last(self, client):
        body = client.get(
            "/properties?sort=travel_time_nearest_beach&order=asc"
        ).get_data(as_text=True)
        assert _row_order(body) == [
            "Title-near-beach",
            "Title-far-beach",
            "Title-no-beach-data",
        ]

    def test_descending_still_keeps_unmeasured_last(self, client):
        body = client.get(
            "/properties?sort=travel_time_nearest_beach&order=desc"
        ).get_data(as_text=True)
        assert _row_order(body) == [
            "Title-far-beach",
            "Title-near-beach",
            "Title-no-beach-data",
        ]

    def test_csv_export_accepts_the_same_sort(self, client):
        resp = client.get(
            "/properties/export.csv?sort=travel_time_nearest_beach&order=asc"
        )
        # The export must not fall back to another order for a sort the page
        # itself offers (its own comment promises parity).
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert body.index("Title-near-beach") < body.index("Title-far-beach")
