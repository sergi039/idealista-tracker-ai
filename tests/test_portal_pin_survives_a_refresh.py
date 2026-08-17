"""A re-geocode must not leave the row less located than it found it (#393).

Measured on production property 733, 2026-08-17. A fotocasa listing carries the
pin the portal placed for that advert. Pressing "refresh coordinates" geocodes
the *title*, and `_build_geocoding_queries` reads the text after "in" -- which
for a plot is a district or a village, not a street. Google answered with the
Llaranes district centroid: 2447 m from the portal pin, still `approximate`, so
nothing was unlocked, and the listing-specific point was gone. It was
recoverable only because a session happened to still hold the number.

Reading the code for that fix turned up a second, worse shape of the same bug:
`refresh=True` clears the coordinate *before* geocoding, so a refresh whose
every candidate query is refused leaves the row with no coordinate at all.

Both are pinned here. What is deliberately NOT pinned as a refusal is an
upgrade: a geocode that comes back `precise` is exactly what the button is for,
and it must still win.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.coordinate_quality import improves_on, portal_coordinate
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment

PORTAL_LAT, PORTAL_LON = 43.5708050, -5.8932443
# The Llaranes district centroid Google actually returned, 2447 m away.
CENTROID_LAT, CENTROID_LON = 43.5489861, -5.8972205


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Plots",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        yield app
        db.drop_all()


def _imported_row(**overrides):
    """A row as `services/fotocasa_import.py` writes it."""
    row = Property(
        source_email_id="fotocasa:190280914",
        title="Land for sale in Llaranes, Avilés",
        municipality="Avilés",
        url="https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d",
        location_lat=PORTAL_LAT,
        location_lon=PORTAL_LON,
        location_accuracy="approximate",
        enrichment={
            "import": {
                "source": "fotocasa",
                "coordinate": {
                    "source": "fotocasa",
                    "lat": str(PORTAL_LAT),
                    "lon": str(PORTAL_LON),
                },
            }
        },
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    db.session.add(row)
    db.session.commit()
    return row


class _Geocoder:
    """Stands in for Google, and records what it was asked.

    Records the queries rather than counting calls: a stub that only counts
    cannot tell "asked about the right thing" from "asked at all", and the
    whole defect here is about what the query named.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.queries = []

    def geocode_address(self, query):
        self.queries.append(query)
        return self.answers.pop(0) if self.answers else None


def _answer(lat, lon, accuracy, address="Llaranes, Avilés, Spain"):
    return {
        "lat": lat,
        "lng": lon,
        "accuracy": accuracy,
        "formatted_address": address,
        "types": ["sublocality", "political"],
        "address_components": [
            {"types": ["postal_code"], "long_name": "33460"},
        ],
    }


class TestTheRule:
    def test_only_precise_improves_on_approximate(self):
        assert improves_on("precise", "approximate") is True
        assert improves_on("precise", "unknown") is True
        assert improves_on("approximate", "approximate") is False
        assert improves_on("approximate", "precise") is False
        assert improves_on("unknown", "approximate") is False
        assert improves_on("precise", "precise") is False

    def test_the_portal_pin_is_read_off_the_row(self, app):
        with app.app_context():
            row = _imported_row()

            assert portal_coordinate(row) == (PORTAL_LAT, PORTAL_LON, "fotocasa")

    @pytest.mark.parametrize(
        "enrichment",
        [
            None,
            {},
            {"import": {}},
            {"import": {"coordinate": {}}},
            {"import": {"coordinate": {"lat": "not a number", "lon": "1"}}},
            {"import": "not a dict"},
        ],
    )
    def test_a_row_with_no_usable_pin_reads_as_none(self, app, enrichment):
        """Provenance must never be able to raise: a malformed block is None,
        and such a row geocodes like any other."""
        with app.app_context():
            row = _imported_row(enrichment=enrichment)

            assert portal_coordinate(row) is None


class TestRefresh:
    def test_an_even_trade_keeps_the_portal_pin(self, app):
        """The measured case: approximate in, approximate out, 2.4 km apart."""
        with app.app_context():
            row = _imported_row()
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder(
                [_answer(CENTROID_LAT, CENTROID_LON, "approximate")]
            )

            assert service.ensure_coordinates(row, refresh=True) is True

            assert float(row.location_lat) == pytest.approx(PORTAL_LAT)
            assert float(row.location_lon) == pytest.approx(PORTAL_LON)
            assert row.location_accuracy == "approximate"

    def test_the_attempt_is_recorded_so_nobody_pays_twice(self, app):
        with app.app_context():
            row = _imported_row()
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder(
                [_answer(CENTROID_LAT, CENTROID_LON, "approximate")]
            )

            service.ensure_coordinates(row, refresh=True)

            record = row.enrichment["geocoding"]
            assert record["kept"] == "fotocasa coordinate"
            assert record["kept_because"] == "the geocode did not improve on it"
            assert record["answered_accuracy"] == "approximate"
            assert record["formatted_address"] == "Llaranes, Avilés, Spain"

    def test_a_precise_answer_still_wins(self, app):
        """The button has to keep working: `precise` unlocks travel."""
        with app.app_context():
            row = _imported_row()
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder(
                [_answer(43.6, -5.9, "precise", "Calle Real 1, Avilés, Spain")]
            )

            assert service.ensure_coordinates(row, refresh=True) is True

            assert float(row.location_lat) == pytest.approx(43.6)
            assert row.location_accuracy == "precise"
            assert "kept" not in row.enrichment["geocoding"]

    def test_a_refresh_that_finds_nothing_leaves_the_row_located(self, app):
        """`refresh` clears the coordinate first, so without this the row ends
        the call with none at all -- worse than it started."""
        with app.app_context():
            row = _imported_row()
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder([])  # every query answers None

            assert service.ensure_coordinates(row, refresh=True) is True

            assert float(row.location_lat) == pytest.approx(PORTAL_LAT)
            assert float(row.location_lon) == pytest.approx(PORTAL_LON)
            assert row.location_accuracy == "approximate"
            assert (
                row.enrichment["geocoding"]["kept_because"]
                == "the geocode returned nothing"
            )

    def test_a_refused_result_also_leaves_the_row_located(self, app):
        """A coarse answer is refused (#331); the pin must survive that too."""
        with app.app_context():
            row = _imported_row()
            service = PropertyLocationService()
            country = _answer(40.463667, -3.749220, "approximate", "Spain")
            country["types"] = ["country", "political"]
            service.geocoding_service = _Geocoder([country, country])

            assert service.ensure_coordinates(row, refresh=True) is True

            assert float(row.location_lat) == pytest.approx(PORTAL_LAT)
            assert row.enrichment["geocoding"]["refused"] == "result_too_coarse"

    def test_a_row_with_no_portal_pin_is_untouched_by_any_of_this(self, app):
        """An idealista row has no pin to defend, and must behave as before."""
        with app.app_context():
            row = _imported_row(
                source_email_id="alert-1",
                url="https://www.idealista.com/en/inmueble/1/",
                enrichment=None,
            )
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder(
                [_answer(CENTROID_LAT, CENTROID_LON, "approximate")]
            )

            assert service.ensure_coordinates(row, refresh=True) is True

            assert float(row.location_lat) == pytest.approx(CENTROID_LAT)
            assert "kept" not in row.enrichment["geocoding"]

    def test_an_ordinary_call_still_geocodes_an_unlocated_row(self, app):
        """No refresh, no coordinate: the normal path is not affected."""
        with app.app_context():
            row = _imported_row(
                location_lat=None, location_lon=None, location_accuracy="unknown"
            )
            service = PropertyLocationService()
            service.geocoding_service = _Geocoder(
                [_answer(CENTROID_LAT, CENTROID_LON, "approximate")]
            )

            assert service.ensure_coordinates(row) is True

            assert float(row.location_lat) == pytest.approx(CENTROID_LAT)


class TestTheImporterWritesIt:
    def test_an_imported_row_carries_the_portal_pin_in_provenance(self, app):
        """Without this the guard above has nothing to read."""
        import pathlib

        from services import fotocasa_import
        from services.fotocasa_source import parse_listing

        fixture = (
            pathlib.Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"
        )
        listing = parse_listing(
            fixture.read_text(encoding="utf-8"),
            "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d",
        )
        previewed = fotocasa_import.preview_row(listing)

        with app.app_context():
            profile = SearchProfile.query.first()
            outcome = fotocasa_import.insert_rows([previewed], profile_id=profile.id)
            row = db.session.get(Property, outcome["created"][0]["id"])

            assert portal_coordinate(row) == (PORTAL_LAT, PORTAL_LON, "fotocasa")

    def test_a_listing_with_no_coordinate_records_none(self, app):
        """The portal does not always give one; a null block must not be a
        `(0, 0)` pin, which is a real place in the Gulf of Guinea."""
        from services import fotocasa_import
        from services.fotocasa_source import parse_listing

        page = (
            '<script type="application/json" id="__initial_props__">'
            '{"realEstate": {"id": 999, "price": 1000, "address": '
            '{"municipality": "Avil\\u00e9s"}}}'
            "</script>"
        )
        listing = parse_listing(page, "https://www.fotocasa.es/en/buy/land/a/b/999/d")
        previewed = fotocasa_import.preview_row(listing)

        with app.app_context():
            profile = SearchProfile.query.first()
            outcome = fotocasa_import.insert_rows([previewed], profile_id=profile.id)
            row = db.session.get(Property, outcome["created"][0]["id"])

            assert row.location_lat is None
            assert portal_coordinate(row) is None
            assert row.enrichment["import"]["coordinate"] is None
