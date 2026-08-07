from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.property_imap_service import PropertyIMAPService
from services.property_location_service import PropertyLocationService
from services.property_travel_service import PropertyTravelService
from services.search_profile_service import SearchProfileService
from tests import setup_test_environment
from utils.cache import cache
from utils.idealista_extractors import (
    extract_area_m2,
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
    assert (
        extract_url("https://www.idealista.com/en/inmueble/123/")
        == "https://www.idealista.com/en/inmueble/123/"
    )
    assert (
        extract_url("https://www.idealista.com/inmueble/456/")
        == "https://www.idealista.com/inmueble/456/"
    )


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


@pytest.mark.parametrize(
    "price_text, expected",
    [
        ("59.000 €", 59000.0),
        ("85,000 €", 85000.0),
        ("59000 €", 59000.0),
        ("1.234.567 €", 1234567.0),
    ],
)
def test_extract_price_handles_thousands_grouped_and_plain_listing_emails(
    price_text, expected
):
    """Regression for GH #21: unanchored regexes matched the trailing "000"
    fragment of a thousands-grouped price and returned 0.0 for every price
    >= 1,000 EUR. Mirrors a plain (non price-change) listing alert email."""
    html = f"""
    <html><body>
      <a href="https://www.idealista.com/en/inmueble/222/">Flat / apartment in calle Foo, Bar</a>
      <div>{price_text}</div>
      <div>85 m² 2 bed</div>
    </body></html>
    """
    assert extract_price(html) == expected
    assert extract_price(price_text) == expected


@pytest.mark.parametrize(
    "area_text, expected",
    [
        ("1.373 m²", 1373.0),  # Spanish thousands grouping (dot)
        ("25.000 m²", 25000.0),  # Spanish thousands grouping, round number
        ("1373 m²", 1373.0),  # plain digits, no separator
        ("1,373 m²", 1373.0),  # English thousands grouping (comma)
    ],
)
def test_extract_area_m2_handles_spanish_format_and_unseparated_areas(area_text, expected):
    """Regression for GH #22: extract_area_m2() had the same unanchored-regex
    defect as extract_price() (#21) -- the comma-pattern matched the "373"
    tail of "1.373 m²" before the dot-pattern was tried, returning 373.0
    instead of 1373.0, and "25.000 m²" returned 0.0 outright. Mirrors a plain
    listing alert email where the area appears alongside the price."""
    html = f"""
    <html><body>
      <a href="https://www.idealista.com/en/inmueble/223/">Plot in calle Foo, Bar</a>
      <div>59.000 €</div>
      <div>{area_text}</div>
    </body></html>
    """
    assert extract_area_m2(html) == expected
    assert extract_area_m2(area_text) == expected


@pytest.mark.parametrize(
    "price_text, expected",
    [
        (
            "1.234,56 €",
            1234.56,
        ),  # dot groups thousands, comma introduces the decimal (EU grammar)
        (
            "1,234.56 €",
            1234.56,
        ),  # comma groups thousands, dot introduces the decimal (US/UK grammar)
        (
            "1.234,567 €",
            None,
        ),  # inconsistent: 3 "decimal" digits under the dot-group grammar -> reject
    ],
)
def test_extract_price_enforces_one_consistent_separator_grammar_per_number(
    price_text, expected
):
    """Regression for a PR #33 review follow-up finding: decimal endings
    (dot or comma, 1-2 digits) must be supported, but a number's separator
    usage must be internally consistent -- a mixed/invalid number like
    "1.234,567 €" must be rejected (None), not silently truncated to
    1234567.0 by stripping every '.'/',' blindly."""
    assert extract_price(price_text) == expected


@pytest.mark.parametrize(
    "sentence, expected_old, expected_new",
    [
        (
            "The price of this listing has dropped from 1.234,56€ to 1.200,00€",
            1234.56,
            1200.0,
        ),
        (
            "The price of this listing has dropped from 1,234.56€ to 1,200.00€",
            1234.56,
            1200.0,
        ),
        (
            "The price of this listing has dropped from 1.234,567€ to 1.234,000€",
            None,
            None,
        ),
    ],
)
def test_extract_price_change_enforces_one_consistent_separator_grammar_per_number(
    sentence, expected_old, expected_new
):
    """Same separator-consistency/decimal-support requirement, applied to the
    price-change ("from X€ to Y€") path, since it feeds the same
    money-critical re-ingestion pipeline as extract_price()."""
    old_price, new_price = extract_price_change(sentence)
    assert old_price == expected_old
    assert new_price == expected_new


def test_extract_price_change_strikethrough_supports_decimal_endings():
    """Same requirement, applied to the strikethrough-HTML detection path
    used by real Idealista price-drop emails."""
    html = """
    <html><body>
      <div><span style="text-decoration:line-through">1.234,56 €</span> <strong>1.200,00 €</strong></div>
    </body></html>
    """
    old_price, new_price = extract_price_change(html)
    assert old_price == 1234.56
    assert new_price == 1200.0
    assert extract_price(html) == 1200.0


def test_reingestion_with_thousands_grouped_price_does_not_zero_existing_price(
    app, monkeypatch
):
    """Regression for GH #21: before the fix, extract_price("59.000 €") returned
    0.0 (a fragment match), and property_imap_service.py's update path (which
    only checks `price is not None`, see :447) would then overwrite an
    existing stored price with 0.0, recording a bogus -100% price change.

    This exercises the real (fixed) extract_price() exactly as
    property_imap_service.py:331 does, then runs it through run_ingestion
    against an already-existing Property row.
    """
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        profile = SearchProfile(
            name="Profile A",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        listing_url = "https://www.idealista.com/inmueble/999999/"
        existing = Property(
            source_email_id="imap_seed_999999",
            idealista_property_id=999999,
            search_profile_id=profile.id,
            url=listing_url,
            title="Flat in Bar",
            deal_type="sale",
            price=Decimal("280000.00"),
            area=Decimal("85.00"),
            area_type="built",
        )
        db.session.add(existing)
        db.session.commit()

        # Re-ingestion subject line, same shape get_idealista_emails() feeds into
        # extract_price(subject) at property_imap_service.py:331.
        subject = "Flat / apartment in calle Foo, Bar - 59.000 €"
        extracted_price = extract_price(subject)
        assert (
            extracted_price == 59000.0
        )  # sanity: proves the fixed parser is exercised, not a hardcoded value

        emails = [
            {
                "type": "listing",
                "source_email_id": "imap_reingest_999999",
                "url": listing_url,
                "idealista_property_id": 999999,
                "search_profile_id": profile.id,
                "title": "Flat in Bar",
                "price": extracted_price,
                "area": 85,
            }
        ]

        service = PropertyIMAPService()
        monkeypatch.setattr(
            service, "get_idealista_emails", lambda max_results=None: list(emails)
        )

        service.run_ingestion(sync_type="test")

        db.session.refresh(existing)
        assert float(existing.price) == 59000.0
        assert float(existing.price) != 0.0


def test_reingestion_with_zero_price_never_zeroes_existing_price(app, monkeypatch):
    """Regression for the PR #33 review finding: extract_price("0 €") legitimately
    parses to 0.0 (it's a valid number, just never a real price), and the update
    path at property_imap_service.py:447-484 used to only check `price is not
    None` -- so a 0.0 price would overwrite a real stored price with 0.0 and
    record a bogus -100% price change. A parsed 0 must be treated the same as
    "no price": the stored price must stay untouched.
    """
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        profile = SearchProfile(
            name="Profile A",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        listing_url = "https://www.idealista.com/inmueble/888888/"
        existing = Property(
            source_email_id="imap_seed_888888",
            idealista_property_id=888888,
            search_profile_id=profile.id,
            url=listing_url,
            title="Flat in Bar",
            deal_type="sale",
            price=Decimal("280000.00"),
            area=Decimal("85.00"),
            area_type="built",
        )
        db.session.add(existing)
        db.session.commit()

        subject = "Price: 0 €"
        extracted_price = extract_price(subject)
        assert (
            extracted_price == 0.0
        )  # sanity: 0 is a legitimately parsed number, not a parse failure

        emails = [
            {
                "type": "listing",
                "source_email_id": "imap_reingest_888888",
                "url": listing_url,
                "idealista_property_id": 888888,
                "search_profile_id": profile.id,
                "title": "Flat in Bar",
                "price": extracted_price,
                "area": 85,
            }
        ]

        service = PropertyIMAPService()
        monkeypatch.setattr(
            service, "get_idealista_emails", lambda max_results=None: list(emails)
        )

        service.run_ingestion(sync_type="test")

        db.session.refresh(existing)
        assert float(existing.price) == 280000.0
        assert existing.price_change_percentage is None


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
                "custom": [
                    {
                        "id": "home",
                        "name": "Home",
                        "lat": 40.41,
                        "lon": -3.69,
                        "mode": "driving",
                    }
                ],
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
                                    "geometry": {
                                        "location": {"lat": 40.50, "lng": -3.60}
                                    },
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
                                    "geometry": {
                                        "location": {"lat": 40.42, "lng": -3.70}
                                    },
                                }
                            ],
                        },
                    )
                return Mock(
                    status_code=200,
                    json=lambda: {"status": "ZERO_RESULTS", "results": []},
                )

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
                return Mock(
                    status_code=200, json=lambda: {"rows": [{"elements": elements}]}
                )

            raise AssertionError(f"Unexpected URL: {url}")

        with patch(
            "services.property_travel_service.requests.get", side_effect=mock_get
        ):
            svc = PropertyTravelService(
                google_maps_key="maps", google_places_key="places"
            )
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
