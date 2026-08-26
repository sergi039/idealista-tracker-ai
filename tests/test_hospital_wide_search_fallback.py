"""A real hospital past Nearby Search's one page must still resolve.

#323 taught the hospital preset to refuse primary care, and shipped
deliberately *without* `wide_search_query` on the strength of one measurement:
at the rural Salamir coordinate the real hospital was still on Nearby Search's
single 20-result page, at rank 18. That reasoning did not generalise, and the
paid recalc it authorised is what proved it -- **48 of the 187 rewritten rows
came back with no hospital at all**.

Measured 2026-08-15 against the deployed image, at 43.3622522,-5.8485461
(property 139, Oviedo): all 20 results of `rankby=distance&type=hospital` sit
within 0.7 km of the origin, and every one is a private practice -- "MUNIA
TOTAL BEAUTY CENTER", "Clínica Uria 40", "Policlinicas", "Sonrisas de fe",
"Renovación carnet de conducir | RENOVA EXPRESS", a physiotherapist and
several named individual doctors (all reproduced below, verbatim). HUCA and
Monte Naranco are both nearby and neither can appear in that response.

So the preset's rules were refusing junk correctly and the answer was never on
the page to be found -- the airport preset's situation exactly (#171/#254),
and it takes the same cure. A Places Text Search takes no `radius`, so none of
Nearby Search's ~50 km cap applies, and the preset's own rules still filter
what comes back. Measured at the two coordinates that were failing:

* Oviedo (property 139) -> "Monte Naranco Hospital", 2.1 km
* Cudillero (property 247) -> "Hospital Universitario San Agustin", 26.2 km

The fallback fires only when Nearby Search already answered and found nothing
the preset accepts, so the rows that resolve today pay nothing for it.
"""

from unittest.mock import patch

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from services.place_rules import place_rules_from  # noqa: E402
from services.property_travel_service import PropertyTravelService  # noqa: E402
from services.search_profile_service import TRAVEL_PRESET_DEFS  # noqa: E402


# The shipped hospital preset is answered from the national register since
# 2026-08-18 (services/reference_places.py), so `_nearest_place_for_preset`
# never reaches Google for it. Everything this file pins is still shipped and
# still load bearing -- the name rules, the ward and day-unit refusals, the
# cache signature, the wide fallback -- because they are what the Google path
# does, and that path is one deleted `reference_source` away from being live
# again. So these tests exercise the preset *as it behaves without the
# register*, and say so, rather than being deleted along with the bill they
# were written about.
def _google_path(preset_key: str) -> dict:
    spec = dict(TRAVEL_PRESET_DEFS[preset_key])
    spec.pop("reference_source", None)
    return spec


OVIEDO_LAT, OVIEDO_LON = 43.3622522, -5.8485461


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _place(name, lat=OVIEDO_LAT, lon=OVIEDO_LON):
    return {
        "name": name,
        "place_id": f"place-{name}",
        "types": ["hospital", "health", "point_of_interest", "establishment"],
        "geometry": {"location": {"lat": lat, "lng": lon}},
    }


# Verbatim: every result of one live Nearby Search at the Oviedo coordinate,
# 2026-08-15, in Google's own distance order. All within 0.7 km.
OVIEDO_PAGE = [
    "MUNIA TOTAL BEAUTY CENTER",
    "Clínica Uria 40",
    "Umivale Activa Oviedo",
    "Cavín- Children and Youth Medical Center",
    "Gabinete Médico Gascona",
    "Medicina Estética Dra. Julia Nieto",
    "Clínica Angioastur",
    "Policlinicas",
    "Dr. Alberto Sicilia Felechosa",
    "Sonrisas de fe",
    "Renovación carnet de conducir | RENOVA EXPRESS",
    "Policlínica Mater Dei",
    "MSK Fisioterapia Avanzada",
    "Clínica Varicis",
    "Carolina Lueje Valdes",
    "Ginemed Oviedo, Clínica de Reproducción Asistida y Fertilidad",
    "julio fuente",
    "TMS ASTURIAS",
    "Luis Palenciano Ballesteros",
    "Centro de Cabeza y Cuello Doctor Llorente",
]
MONTE_NARANCO = "Monte Naranco Hospital"


class _Response:
    status_code = 200

    def __init__(self, results):
        self._results = results

    def json(self):
        return {"status": "OK", "results": self._results}


class TestThePresetOptsIntoTheWideSearch:
    def test_the_hospital_preset_declares_a_wide_search_query(self):
        assert _google_path("hospital").get("wide_search_query") == "hospital"

    def test_the_dense_presets_still_do_not(self):
        """The fallback is a paid call; only presets that need it opt in."""
        for key in ("train_station", "police", "supermarket", "school"):
            assert "wide_search_query" not in TRAVEL_PRESET_DEFS[key], key

    def test_it_is_not_part_of_the_cache_signature(self):
        """Adding it must not invalidate the rows that already resolve.

        `place_rules_from` reads only the require/reject patterns, so the
        Places cache key is unchanged by this ticket -- which is what let the
        48 failing rows be re-run without re-billing the other 139.
        """
        with_query = place_rules_from(_google_path("hospital"))
        without_query = place_rules_from(
            {
                k: v
                for k, v in _google_path("hospital").items()
                if k != "wide_search_query"
            }
        )

        assert with_query.signature == without_query.signature


class TestTheOviedoPage:
    def test_every_result_on_the_real_page_is_refused(self):
        rules = place_rules_from(_google_path("hospital"))

        accepted = [n for n in OVIEDO_PAGE if not rules.rejects(_place(n))]

        assert accepted == [], (
            f"the whole page is private practice; got {accepted} through the rules"
        )

    def test_the_fallback_resolves_the_hospital_the_page_could_not_reach(self, app):
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        calls = []

        def _dispatch(_fn, url, **kwargs):
            calls.append(url)
            if "nearbysearch" in url:
                return _Response([_place(n) for n in OVIEDO_PAGE])
            return _Response([_place(MONTE_NARANCO, lat=43.3800, lon=-5.8600)])

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                side_effect=_dispatch,
            ):
                lookup = service._nearest_place_for_preset(
                    OVIEDO_LAT, OVIEDO_LON, "hospital", _google_path("hospital")
                )

        assert lookup.place is not None, "the fallback must rescue this coordinate"
        assert lookup.place["name"] == MONTE_NARANCO
        assert any("textsearch" in c for c in calls), "the wide search must have fired"


class TestTheSecondCallStaysTheException:
    def test_no_wide_search_when_nearby_already_found_a_hospital(self, app):
        """The 139 rows that resolve today must not start paying for a second call."""
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        calls = []

        def _dispatch(_fn, url, **kwargs):
            calls.append(url)
            return _Response([_place("Hospital Universitario Central de Asturias")])

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                side_effect=_dispatch,
            ):
                lookup = service._nearest_place_for_preset(
                    OVIEDO_LAT, OVIEDO_LON, "hospital", _google_path("hospital")
                )

        assert lookup.place["name"] == "Hospital Universitario Central de Asturias"
        assert not any("textsearch" in c for c in calls), (
            "a resolved Nearby Search must not fire the paid fallback"
        )

    def test_a_google_refusal_does_not_buy_a_second_call(self, app):
        """A failure is a failure; it must not spend another call on the same refusal."""
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        calls = []

        class _Denied:
            status_code = 200

            def json(self):
                return {"status": "REQUEST_DENIED", "error_message": "nope"}

        def _dispatch(_fn, url, **kwargs):
            calls.append(url)
            return _Denied()

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                side_effect=_dispatch,
            ):
                lookup = service._nearest_place_for_preset(
                    OVIEDO_LAT, OVIEDO_LON, "hospital", _google_path("hospital")
                )

        assert lookup.place is None
        assert lookup.failure is not None, (
            "a refusal must stay a refusal, not not_found"
        )
        assert not any("textsearch" in c for c in calls)

    def test_a_wide_search_finding_nothing_is_not_found_and_not_zero(self, app):
        """#98: still an answer, still dropped by the scorer rather than scored 0."""
        service = PropertyTravelService()
        service.google_places_key = "test-key"

        def _dispatch(_fn, url, **kwargs):
            if "nearbysearch" in url:
                return _Response([_place(n) for n in OVIEDO_PAGE])
            return _Response([_place("Clínica Dental Oviedo")])

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                side_effect=_dispatch,
            ):
                lookup = service._nearest_place_for_preset(
                    OVIEDO_LAT, OVIEDO_LON, "hospital", _google_path("hospital")
                )

        assert lookup.place is None
        assert lookup.failure is None
