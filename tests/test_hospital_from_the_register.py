"""The hospital preset is answered from the national register, not from Places.

The owner's Google invoice for 1-18 August 2026 was EUR 190, and the Places
half of an enrichment is 63% of it. `data/hospitals_cnh.json` -- the Ministry
of Health's Catálogo Nacional de Hospitales, already imported and already read
by the quality-of-life card -- answers "nearest hospital" for free, and better:
Google's `hospital` type indexes a campus room by room, which is why #323 and
#325 exist at all.

What these tests pin is not "it works" but the two ways it could quietly stop
being free or stop being honest:

* no Places call is made for this preset, and that includes the wide Text
  Search fallback the preset still carries for the day the register is removed;
* a register that cannot answer produces a *refusal*, never "no hospital
  nearby" -- the register not covering Alicante has established nothing about
  Alicante (#98).
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import property_travel_service as travel_module
from services.property_travel_service import PropertyTravelService
from services.reference_places import (
    REASON_NO_REFERENCE_DATA,
    REASON_OUTSIDE_COVERAGE,
)
from tests import setup_test_environment

# Cudillero, and Hospital Universitario San Agustín in Avilés: the pair the
# #325 measurement was taken against, so a wrong answer here is recognisable.
PLOT = (43.5629, -6.1453)
SAN_AGUSTIN = {
    "name": "Hospital Universitario San Agustin",
    "municipality": "Avilés",
    "beds": 500,
    "teaching": True,
    "high_tech_count": 3,
    "lat": 43.5547,
    "lon": -5.9248,
    "distance_km": 18.2,
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _profile():
    profile = SearchProfile(
        name="Norte",
        is_active=True,
        is_default=True,
        travel_targets={
            "presets": {"hospital": {"enabled": True, "mode": "driving"}},
            "custom": [],
        },
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _listing(profile):
    prop = Property(
        source_email_id="register-row",
        title="plot",
        property_category="land",
        location_lat=PLOT[0],
        location_lon=PLOT[1],
        location_accuracy="precise",
        search_profile_id=profile.id,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _register(monkeypatch, verdict):
    """Answer the register with `verdict`, the way the QoL service would."""
    from services import quality_of_life_service as qol

    monkeypatch.setattr(
        qol.QualityOfLifeService, "hospitals", lambda self, lat, lon: verdict
    )


def _forbid_google(monkeypatch):
    """Record every outbound call instead of raising on one.

    Raising does not work here and the first version of this file proved it:
    `_places_nearby` wraps its request in `except Exception` and turns
    anything thrown into a `GoogleApiFailure`, so an `AssertionError` from a
    stub arrives back as a tidy refusal and the test passes while the paid
    call is being made. That is #307's lesson in miniature -- the guard has to
    *record*, and the assertion has to happen in the test.

    Returns the list of calls; assert it is empty.
    """
    calls = []

    def record(*args, **kwargs):
        calls.append(kwargs.get("params") or args)
        raise AssertionError("paid Google request")

    monkeypatch.setattr(travel_module, "request_with_retries", record)
    return calls


class TestItComesFromTheRegister:
    def test_the_named_hospital_is_the_registers_own(self, app, monkeypatch):
        _register(
            monkeypatch,
            {"status": "ok", "nearest": {"general_acute": dict(SAN_AGUSTIN)}},
        )
        calls = _forbid_google(monkeypatch)
        profile = _profile()
        prop = _listing(profile)

        lookup = PropertyTravelService()._nearest_place_for_preset(
            prop.location_lat,
            prop.location_lon,
            "hospital",
            {"reference_source": "cnh_hospitals", "place_types": ["hospital"]},
        )

        assert calls == [], "the register must answer without any Google request"
        assert lookup.failure is None
        assert lookup.place["name"] == "Hospital Universitario San Agustin"
        assert lookup.place["source"] == "cnh_hospitals"
        # Provenance the page can read, and no invented Google id.
        assert lookup.place.get("place_id") is None
        assert lookup.place["preset_key"] == "hospital"

    def test_the_nearest_of_several_groupings_wins(self, app, monkeypatch):
        near = dict(SAN_AGUSTIN, name="Hospital Vital Alvarez Buylla", distance_km=9.4)
        _register(
            monkeypatch,
            {
                "status": "ok",
                "nearest": {
                    "general_acute": dict(SAN_AGUSTIN),
                    "teaching_high_tech": near,
                },
            },
        )
        calls = _forbid_google(monkeypatch)
        _profile()

        lookup = PropertyTravelService()._nearest_place_for_preset(
            PLOT[0],
            PLOT[1],
            "hospital",
            {"reference_source": "cnh_hospitals", "place_types": ["hospital"]},
        )

        assert calls == []
        assert lookup.place["name"] == "Hospital Vital Alvarez Buylla"


class TestARefusalIsNotAnAbsence:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("no_reference_data", REASON_NO_REFERENCE_DATA),
            ("outside_reference_coverage", REASON_OUTSIDE_COVERAGE),
        ],
    )
    def test_it_refuses_rather_than_reporting_no_hospital(
        self, app, monkeypatch, status, expected
    ):
        _register(monkeypatch, {"status": status})
        calls = _forbid_google(monkeypatch)
        _profile()

        lookup = PropertyTravelService()._nearest_place_for_preset(
            PLOT[0],
            PLOT[1],
            "hospital",
            {"reference_source": "cnh_hospitals", "place_types": ["hospital"]},
        )

        assert calls == [], "a register refusal must not become a paid lookup"
        assert lookup.place is None
        # A failure, not an empty answer: `place is None and failure is None`
        # is how this service says "Google looked and there is nothing there",
        # and the register has established no such thing.
        assert lookup.failure is not None
        assert lookup.failure.reason == expected

    def test_a_refusal_does_not_fall_through_to_the_paid_search(self, app, monkeypatch):
        """The preset still carries `wide_search_query`; it must not fire.

        The service is given a key on purpose. Without one every Places path
        refuses before it reaches the transport, so a test that leaves the
        suite's keyless config in place passes whether the guard exists or
        not -- which is what the first version of this test did.
        """
        _register(monkeypatch, {"status": "outside_reference_coverage"})
        calls = _forbid_google(monkeypatch)
        _profile()

        service = PropertyTravelService()
        service.google_places_key = "test-key-that-must-not-be-used"
        service.google_maps_key = "test-key-that-must-not-be-used"

        lookup = service._nearest_place_for_preset(
            PLOT[0],
            PLOT[1],
            "hospital",
            {
                "reference_source": "cnh_hospitals",
                "place_types": ["hospital"],
                "wide_search_query": "hospital",
            },
        )

        assert calls == [], "the wide Text Search fallback must not fire"
        assert lookup.place is None
        assert lookup.failure is not None


class TestOtherPresetsAreUntouched:
    def test_a_preset_with_no_register_still_asks_google(self, app, monkeypatch):
        """The change must be one preset wide, not a global switch."""
        called = {}

        def fake_nearby(self, lat, lon, place_type, keyword=None):
            called["place_type"] = place_type
            return [], None

        monkeypatch.setattr(PropertyTravelService, "_places_nearby", fake_nearby)
        _profile()

        PropertyTravelService()._nearest_place_for_preset(
            PLOT[0], PLOT[1], "train_station", {"place_types": ["train_station"]}
        )

        assert called["place_type"] == "train_station"


class TestThePresetItselfDeclaresIt:
    def test_the_shipped_hospital_preset_reads_the_register(self, app):
        from services.search_profile_service import TRAVEL_PRESET_DEFS

        assert TRAVEL_PRESET_DEFS["hospital"]["reference_source"] == "cnh_hospitals"

    def test_no_other_preset_claims_a_register(self, app):
        """One preset moved. A second would need its own measurement."""
        from services.search_profile_service import TRAVEL_PRESET_DEFS

        with_source = {
            key
            for key, spec in TRAVEL_PRESET_DEFS.items()
            if spec.get("reference_source")
        }
        assert with_source == {"hospital"}
