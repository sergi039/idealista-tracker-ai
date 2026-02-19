import json
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_location_service import PropertyLocationService
from services.property_travel_service import PropertyTravelService
from services.search_profile_service import SearchProfileService
from tests import setup_test_environment
from utils.cache import cache
from utils.idealista_extractors import (
    extract_listing_title,
    extract_municipality_from_title,
    extract_price,
    extract_price_change,
    extract_url,
)


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


def test_extract_url_supports_language_and_root_paths():
    assert extract_url("https://www.idealista.com/en/inmueble/123/") == "https://www.idealista.com/en/inmueble/123/"
    assert extract_url("https://www.idealista.com/inmueble/456/") == "https://www.idealista.com/inmueble/456/"


def test_legacy_lands_page_redirects_to_properties(app):
    client = app.test_client()
    response = client.get("/lands?view_type=list&search=foo", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers.get("Location") == "/properties?view_type=list&search=foo"


def test_extract_listing_title_and_municipality_from_email_html():
    html = """
    <html><body>
      <a href="https://www.idealista.com/en/inmueble/987654321/">Detached house in calle Asturias, Ciudad Quesada</a>
      <div>560,000 €</div>
      <div>169 m² 3 bed</div>
      <a href="https://www.idealista.com/en/inmueble/987654321/">See 26 photos</a>
    </body></html>
    """
    title = extract_listing_title(html, idealista_property_id=987654321)
    assert title == "Detached house in calle Asturias, Ciudad Quesada"
    assert extract_municipality_from_title(title) == "Ciudad Quesada"


def test_extract_price_prefers_new_price_for_price_reduction_emails():
    html = """
    <html><body>
      <p>The price of this listing has dropped from 290,000€ to 285,000€</p>
      <div><span style="text-decoration:line-through">290,000 €</span> <strong>285,000 €</strong></div>
      <a href="https://www.idealista.com/en/inmueble/111/">Flat / apartment in calle Foo, Bar</a>
    </body></html>
    """
    old_price, new_price = extract_price_change(html)
    assert old_price == 290000.0
    assert new_price == 285000.0
    assert extract_price(html) == 285000.0


def test_extract_price_change_from_strikethrough_only():
    html = """
    <html><body>
      <div><span style="text-decoration: line-through;">290.000 €</span></div>
      <div><strong>285.000 €</strong></div>
    </body></html>
    """
    old_price, new_price = extract_price_change(html)
    assert old_price == 290000.0
    assert new_price == 285000.0
    assert extract_price(html) == 285000.0


def test_default_classification_rules_do_not_misclassify_ambiguous_local_word(app):
    from services.settings_service import SettingsService

    rules = SettingsService.get_property_classification_rules()
    # "local" as in "local amenities" should not trigger commercial retail by default.
    text = "Apartment in Madrid with local amenities nearby"

    matched = None
    for rule in rules:
        pattern = rule.get("pattern")
        if not pattern:
            continue
        import re

        if re.search(pattern, text, re.IGNORECASE):
            matched = rule
            break

    assert matched is None or matched.get("category") != "commercial"


def test_property_location_service_sets_coordinates_from_title():
    class DummyGeocoder:
        def __init__(self):
            self.calls = []

        def geocode_address(self, address: str):
            self.calls.append(address)
            return {
                "lat": 40.0,
                "lng": -3.0,
                "formatted_address": "Madrid, Spain",
                "accuracy": "precise",
            }

    geocoder = DummyGeocoder()
    svc = PropertyLocationService(geocoding_service=geocoder)

    prop = Property(source_email_id="loc_test_1")
    prop.title = "Detached house in calle Asturias, Ciudad Quesada"
    ok = svc.ensure_coordinates(prop)

    assert ok is True
    assert float(prop.location_lat) == 40.0
    assert float(prop.location_lon) == -3.0
    assert prop.location_accuracy == "precise"
    assert prop.enrichment["geocoding"]["query"].endswith("Spain")
    assert geocoder.calls


def test_property_travel_service_populates_travel_for_enabled_presets(app):
    with app.app_context():
        cache.clear()

        preset_defs = SearchProfileService.get_travel_preset_defs()
        presets = {d["key"]: {"enabled": False, "mode": "driving"} for d in preset_defs}
        presets["airport"]["enabled"] = True
        presets["supermarket"]["enabled"] = True

        profile = SearchProfile(
            name="Junio",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": presets,
                "custom": [{"id": "home", "name": "Home", "lat": 40.41, "lon": -3.69, "mode": "driving"}],
            },
        )
        db.session.add(profile)
        db.session.commit()

        prop = Property(
            source_email_id="travel_test_1",
            title="Apartment in Centro, Madrid",
            municipality="Madrid",
            search_profile_id=profile.id,
            location_lat=Decimal("40.4168"),
            location_lon=Decimal("-3.7038"),
        )
        db.session.add(prop)
        db.session.commit()

        def mock_get(url, params=None, timeout=0, headers=None):
            if "place/nearbysearch" in url:
                place_type = (params or {}).get("type")
                if place_type == "airport":
                    return Mock(
                        status_code=200,
                        json=lambda: {
                            "status": "OK",
                            "results": [
                                {
                                    "name": "Airport A",
                                    "place_id": "pid-air",
                                    "types": ["airport"],
                                    "geometry": {"location": {"lat": 40.50, "lng": -3.60}},
                                }
                            ],
                        },
                    )
                if place_type == "supermarket":
                    return Mock(
                        status_code=200,
                        json=lambda: {
                            "status": "OK",
                            "results": [
                                {
                                    "name": "Market",
                                    "place_id": "pid-sup",
                                    "types": ["supermarket"],
                                    "geometry": {"location": {"lat": 40.42, "lng": -3.70}},
                                }
                            ],
                        },
                    )
                return Mock(status_code=200, json=lambda: {"status": "ZERO_RESULTS", "results": []})

            if "distancematrix" in url:
                dests = (params or {}).get("destinations", "").split("|")
                elements = []
                for idx, _ in enumerate(dests):
                    elements.append(
                        {
                            "status": "OK",
                            "distance": {"value": 1000 * (idx + 1)},
                            "duration": {"value": 600 * (idx + 1)},
                        }
                    )
                return Mock(status_code=200, json=lambda: {"rows": [{"elements": elements}]})

            raise AssertionError(f"Unexpected URL: {url}")

        with patch("services.property_travel_service.requests.get", side_effect=mock_get):
            svc = PropertyTravelService(google_maps_key="maps", google_places_key="places")
            ok = svc.calculate_for_property(prop, commit=True)
            assert ok is True

        # Reload from DB to ensure persisted.
        refreshed = db.session.get(Property, prop.id)
        assert refreshed and isinstance(refreshed.travel, dict)

        targets = refreshed.travel.get("targets") or {}
        assert targets["airport"]["place"]["name"] == "Airport A"
        assert targets["airport"]["distance_m"] == 1000
        assert targets["airport"]["duration_min"] == 10

        assert targets["supermarket"]["place"]["name"] == "Market"
        assert targets["supermarket"]["distance_m"] == 2000
        assert targets["supermarket"]["duration_min"] == 20

        assert targets["custom:home"]["distance_m"] == 3000
        assert targets["custom:home"]["duration_min"] == 30
