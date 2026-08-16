"""The Nominatim fallback reports `types`, so the one coarse-result rule sees
it too (issue #342, residue item 4).

`_is_too_coarse` (services/property_location_service.py, issue #331) and
`_result_province` (issue #348) both read Google's own vocabulary --
`types` and a `postal_code` address component. Before this fix
`GeocodingService._fallback_geocoding` (the Nominatim branch) returned
`"types": None`-shaped nothing and `"address_components": []`, so a Nominatim
country-level answer sailed straight past `_is_too_coarse` and could not be
compared for province either. The fix asks Nominatim for `addressdetails=1`
and maps its `addresstype`/`place_rank`/`address.postcode` into the same
vocabulary Google's branch already returns -- it does not add a second
refusal rule.
"""

from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from config import Config
from models import Property
from services.property_location_service import (
    PropertyLocationService,
    _is_too_coarse,
    _result_province,
)
from tests import setup_test_environment
from utils.geocoding import GeocodingService

# A real Nominatim `addressdetails=1` answer for a bare "Spain" query --
# shaped like the live API's response to a query it cannot resolve any
# further than the country.
NOMINATIM_COUNTRY = {
    "place_id": 111,
    "lat": "40.4637000",
    "lon": "-3.7492200",
    "display_name": "España",
    "addresstype": "country",
    "place_rank": 4,
    "address": {"country": "España", "country_code": "es"},
}

# A municipality-level answer carrying a postcode in province 33 (Asturias).
NOMINATIM_TOWN = {
    "place_id": 222,
    "lat": "43.5000000",
    "lon": "-5.6500000",
    "display_name": "Pola de Siero, Asturias, 33199, España",
    "addresstype": "town",
    "place_rank": 16,
    "address": {
        "town": "Pola de Siero",
        "postcode": "33199",
        "state": "Principado de Asturias",
        "country": "España",
    },
}


def _mock_response(status_code=200, json_data=None):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _fallback(nominatim_payload):
    """Call the real `_fallback_geocoding` with Nominatim's transport mocked."""
    service = GeocodingService()
    with patch("utils.geocoding.request_with_retries") as mock_request:
        mock_request.return_value = _mock_response(json_data=[nominatim_payload])
        return service._fallback_geocoding("some query, Spain")


class TestTheFallbackReportsTypes:
    def test_a_country_answer_yields_country_types(self):
        result = _fallback(NOMINATIM_COUNTRY)
        assert result["types"] == ["country"]

    def test_a_country_answer_is_refused_as_too_coarse(self):
        """The one existing rule, unmodified, now sees the fallback too."""
        result = _fallback(NOMINATIM_COUNTRY)
        assert _is_too_coarse(result) is True

    def test_a_town_answer_does_not_trip_the_coarse_rule(self):
        result = _fallback(NOMINATIM_TOWN)
        assert result["types"] == ["locality"]
        assert _is_too_coarse(result) is False

    def test_the_fallback_still_returns_lat_lng_and_address_unchanged(self):
        result = _fallback(NOMINATIM_COUNTRY)
        assert result["lat"] == pytest.approx(40.4637000)
        assert result["lng"] == pytest.approx(-3.7492200)
        assert result["formatted_address"] == "España"
        assert result["accuracy"] == "approximate"


class TestTheFallbackReportsAPostcodeComponent:
    def test_a_postcode_becomes_a_postal_code_component(self):
        result = _fallback(NOMINATIM_TOWN)
        components = result["address_components"]
        assert components == [
            {"long_name": "33199", "short_name": "33199", "types": ["postal_code"]}
        ]

    def test_the_existing_province_rule_reads_it(self):
        """No second rule -- the fallback's answer feeds #348's own reader."""
        result = _fallback(NOMINATIM_TOWN)
        assert _result_province(result) == "33"

    def test_a_country_answer_carries_no_postcode(self):
        result = _fallback(NOMINATIM_COUNTRY)
        assert result["address_components"] == []
        assert _result_province(result) is None


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


def _prop(**kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", "issue_342_item4"),
        title=kw.pop("title", "Finca offers for"),
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _stored(prop_id):
    db.session.expire_all()
    return db.session.get(Property, prop_id)


def _google_fails_then_nominatim(nominatim_payload):
    """`request_with_retries` fake: Google's endpoint answers ZERO_RESULTS,
    which sends `geocode_address` down the real fallback path to Nominatim,
    whose transport is mocked here too."""

    def fake(request_fn, url, **kwargs):
        if "nominatim.openstreetmap.org" in url:
            return _mock_response(json_data=[nominatim_payload])
        return _mock_response(json_data={"status": "ZERO_RESULTS", "results": []})

    return fake


class TestEndToEndThroughPropertyLocationService:
    """Google refuses, the real fallback runs, and the existing refusal rule
    in `ensure_coordinates` catches the country-level Nominatim answer --
    with no change to that rule."""

    def test_a_nominatim_country_fallback_is_refused_not_stored(self, app):
        with app.app_context():
            prop = _prop(municipality="Siero")
            # Force the Google branch to run and fail at the HTTP layer,
            # rather than relying on the ambient test environment carrying no
            # Google Maps key -- that would prove the same thing by accident.
            with patch.object(Config, "GOOGLE_MAPS_API_KEY", "fake-key-for-this-test"):
                service = PropertyLocationService()
                with patch("utils.geocoding.request_with_retries") as mock_request:
                    mock_request.side_effect = _google_fails_then_nominatim(
                        NOMINATIM_COUNTRY
                    )
                    ok = service.ensure_coordinates(prop)
            db.session.commit()

            assert ok is False
            stored = _stored(prop.id)
            assert stored.location_lat is None
            assert stored.location_lon is None
            assert stored.enrichment["geocoding"]["refused"] == "result_too_coarse"
            assert stored.enrichment["geocoding"]["result_types"] == ["country"]
