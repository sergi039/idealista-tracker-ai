"""A measured zero is not a missing value, on either detail page.

Found by auditing every page for the defects the property page had.

* **The legacy land page still printed an invented walking time.** Seven
  blocks rendered `car_minutes * 4` behind a walking icon whenever the
  product came to 15 minutes or less. The property page lost them with #171;
  `/lands/<id>` kept all seven, and three listings actually display one.
* **`value || 'N/A'` swallows a real zero.** A plot that earns no rent yields
  0%, and a supermarket 29 m away is 0 minutes away. Both are measurements.
  The property page got its `formatMetricValue` in #170; the land page still
  had the raw `||` in six places.
* **`{% if numeric_field %}` hides a zero row.** The Transport card and the
  Dual Scoring card both dropped a field whose value was `0` -- reading the
  measurement as an absence.
"""

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Land, Property  # noqa: E402


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


def _property(travel=None, **overrides):
    """`travel_time_*` on Property are read from the JSON column, not columns."""
    fields = {
        "source_email_id": "zero-fixture",
        "title": "ZeroFixture",
        "municipality": "Gijón",
        "location_lat": 43.5,
        "location_lon": -5.6,
    }
    fields.update(overrides)
    prop = Property(**fields)
    if travel is not None:
        prop.travel = travel
    db.session.add(prop)
    db.session.commit()
    return prop.id


def _targets(**minutes):
    return {
        "targets": {
            key: {
                "kind": "preset",
                "enabled": True,
                "status": "ok",
                "duration_min": value,
                "distance_km": 0.0 if value == 0 else 1.5,
                "place": {"name": f"{key} place"},
            }
            for key, value in minutes.items()
        }
    }


def _land(**overrides):
    fields = {
        "source_email_id": f"zero-land-{overrides.get('title', 'default')}",
        "title": "ZeroLandFixture",
        "municipality": "Cudillero",
        "travel_time_police": 2,
        "distance_police": 0.4,
    }
    fields.update(overrides)
    land = Land(**fields)
    db.session.add(land)
    db.session.commit()
    return land.id


class TestNoInventedWalkingTimeAnywhere:
    def test_the_land_page_dropped_it_too(self, app, client):
        """`travel_time * 4` is arithmetic, not a measured walk."""
        land = _land()

        body = client.get(f"/lands/{land}").get_data(as_text=True)

        assert "fa-walking" not in body
        assert "walking_time" not in body

    def test_the_property_page_still_has_none(self, app, client):
        listing = _property(source_email_id="zero-walk", travel=_targets(police=2))

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "fa-walking" not in body
        assert "walking_time" not in body


class TestAZeroRowIsStillDrawn:
    def test_a_zero_travel_time_is_shown_not_hidden(self, app, client):
        """A police station two minutes away, and a supermarket at the door."""
        listing = _property(source_email_id="zero-travel", travel=_targets(police=0))

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Police Station Distance" in body, (
            "a zero-minute drive is a measurement; the row must survive it"
        )

    def test_a_zero_score_still_renders_its_card(self, app, client):
        listing = _property(
            source_email_id="zero-score", score_investment=0, score_lifestyle=0
        )

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Investment Score" in body
        assert "Lifestyle Score" in body

    def test_a_scored_property_is_unaffected(self, app, client):
        listing = _property(
            source_email_id="scored", score_investment=30, score_lifestyle=60
        )

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Investment Score" in body
        assert "Lifestyle Score" in body


class TestTheLandPageFormatsMetricsLikeTheProperty:
    def test_it_has_the_shared_renderer(self, app, client):
        land = _land(title="LandMetricsFixture")

        body = client.get(f"/lands/{land}").get_data(as_text=True)

        assert "function formatMetricValue" in body

    @pytest.mark.parametrize(
        "field",
        [
            "rental_yield",
            "cap_rate",
            "price_to_rent_ratio",
            "payback_period_years",
            "total_investment",
        ],
    )
    def test_no_metric_falls_back_to_the_or_na_idiom(self, app, client, field):
        """`x || 'N/A'` turns a measured 0 into "no data"."""
        land = _land(title=f"LandMetric-{field}")

        body = client.get(f"/lands/{land}").get_data(as_text=True)

        assert f"{field} || 'N/A'" not in body

    def test_the_page_no_longer_labels_a_missing_price_as_na(self, app, client):
        land = _land(title="LandPriceFixture")

        body = client.get(f"/lands/{land}").get_data(as_text=True)

        assert "'Price N/A'" not in body
        assert "'Area N/A'" not in body
        assert "Price not stated" in body
