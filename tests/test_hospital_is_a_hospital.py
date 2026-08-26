"""The nearest "hospital" is a hospital, not the village GP surgery.

Google's `hospital` place type covers a *centro de salud* -- an outpatient
primary-care surgery with no beds and no emergency department -- and the
preset recorded the nearest one. Measured 2026-08-15 on the Salamir listing
(43.568817,-6.211955, Cudillero): the app read "hospital 11 min", which is the
Centro de Salud in Muros de Nalón, 9.0 km away. The assigned hospital is
Hospital Universitario San Agustín in Avilés (Área I Occidente), ~27 min by
road; HUCA in Oviedo is ~44 min. A 2.5x overstatement of medical access, on a
number the scorer reads, and 187 of the owner's 396 travel rows held one.

The same defect class as #171 (`type=airport` covering helipads and aeroclubs,
which had the legacy path storing 145 helipads as airports), so it takes the
same cure: `services/place_rules.py` over patterns declared on the preset in
`services/search_profile_service.py`. This file pins the hospital half of it.

The candidate list in `SALAMIR_PAGE` is the real one -- a live Nearby Search at
that coordinate on 2026-08-15, all 20 results in the order Google ranked them.
It is the fixture because the interesting failure is not any single name: it is
that Google indexes the San Agustín campus room by room, every room tagged
`hospital`, so 13 departments sort ahead of the hospital itself at rank 18 of
20, and two of those departments carry the word "hospital" in their names.
"""

from unittest.mock import patch

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from services.property_travel_service import (  # noqa: E402
    PropertyTravelService,
    _place_rules,
)
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


SALAMIR_LAT, SALAMIR_LON = 43.568817, -6.211955


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


def _rules():
    rules = _place_rules(_google_path("hospital"))
    assert rules is not None, "the hospital preset must declare place rules"
    return rules


def _place(name, types=("hospital", "health", "point_of_interest", "establishment")):
    """A Nearby Search result, shaped as Google returns it."""
    return {
        "name": name,
        "place_id": f"place-{name}",
        "types": list(types),
        "geometry": {"location": {"lat": SALAMIR_LAT, "lng": SALAMIR_LON}},
    }


# Verbatim from a live Nearby Search at the Salamir coordinate, 2026-08-15:
# `rankby=distance`, `type=hospital`, all 20 results, Google's own order.
SALAMIR_PAGE = [
    "Centro de Salud - Muros de Nalon",
    "Consultorio de Malleza",
    "Consultorio Médico Local Soto del Barco",
    "ASCIVITAS Centro de Apoyo a la Integración",
    "Consultorio Local San Román De Cándamo",
    "Centro de reconocimiento de conductores Castrillón",
    "Centro de salud Piedras Blancas",
    "Área Consultas de Dermatología, Endocrinología y Neurología",
    "Archivo de Historias Clínicas",
    "Área consultas de Hematología, Medicina Interna, Nefrología",
    "Admisión de consultas externas",
    "Unidad de Hospitalización de Obstetricia",
    "Área de Partos",
    "Sala de autopsias y cámaras frigoríficas",
    "Nutrición parenteral",
    "Ala Norte",
    "Electroencefalo-grafía",
    "Hospital de Día Médico",
    "Hospital Universitario San Agustin",
    "Atención al Paciente e Información",
]
SAN_AGUSTIN = "Hospital Universitario San Agustin"


class TestTheSalamirCase:
    def test_the_centro_de_salud_that_started_this_is_refused(self):
        """Rank 0 at 9.0 km, and what the app recorded as "hospital 11 min"."""
        assert _rules().rejects(_place("Centro de Salud - Muros de Nalon"))

    def test_the_real_hospital_is_accepted(self):
        assert not _rules().rejects(_place(SAN_AGUSTIN))

    def test_san_agustin_is_the_first_name_the_rules_accept(self):
        """The whole page, in Google's order: rank 18 must be the first hit.

        Ranks 0-17 are six primary-care or unrelated places and twelve
        departments of San Agustín's own campus. Refusing each individually is
        not the claim -- the claim is that after refusing them the *next* name
        the lookup reaches is the hospital.
        """
        rules = _rules()

        accepted = [name for name in SALAMIR_PAGE if not rules.rejects(_place(name))]

        assert accepted == [SAN_AGUSTIN], (
            f"expected only the hospital to survive the page, got {accepted}"
        )

    def test_the_lookup_walks_the_real_page_to_the_hospital(self, app):
        """End to end through `_nearest_place_for_preset`, not just the matcher."""
        service = PropertyTravelService()
        service.google_places_key = "test-key"

        class _Response:
            status_code = 200

            def json(self):
                return {
                    "status": "OK",
                    "results": [_place(name) for name in SALAMIR_PAGE],
                }

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                return_value=_Response(),
            ):
                lookup = service._nearest_place_for_preset(
                    SALAMIR_LAT,
                    SALAMIR_LON,
                    "hospital",
                    _google_path("hospital"),
                )

        assert lookup.place is not None
        assert lookup.place["name"] == SAN_AGUSTIN


class TestPrimaryCareIsNotAHospital:
    @pytest.mark.parametrize(
        "name",
        [
            # Every primary-care name in the owner's 396 travel rows.
            "Centro de Salud - Muros de Nalon",
            "Centro De Salud Cerdido",
            "Centro de Salud de Las Vegas",
            "Centro de Salud de Bimenes",
            "Urgencias, Centro de Salud La Ería",
            "Centro de saúde de Cambre",
            "Centro de Saúde de Paderne",
            "CAPTADOR CENTRO DE SALUD LOS MALLOS",
            "Ambulatorio de La Felguera",
            "Policlinicas",
            "Consultorio de Malleza",
            "Consultorio Médico Local Soto del Barco",
            "Centro Médico El Coto",
        ],
    )
    def test_it_is_refused(self, name):
        assert _rules().rejects(_place(name))

    @pytest.mark.parametrize(
        "name",
        [
            "Centro de Salud Mental I Área Sanitaria III",
            "Unidad de Hospitalización de Salud Mental",
        ],
    )
    def test_a_mental_health_facility_is_refused(self, name):
        """No emergency department, under either of the two names in the data.

        The second one is why this is a reject pattern and not merely an
        omission from the require list: "Hospitalización" contains "hospital".
        """
        assert _rules().rejects(_place(name))

    @pytest.mark.parametrize(
        "name",
        [
            # Both carry "hospital" and both outrank their own parent campus.
            "Hospital de Día Médico",
            "Unidad de Hospitalización de Obstetricia",
        ],
    )
    def test_a_department_that_says_hospital_is_refused(self, name):
        assert _rules().rejects(_place(name))

    @pytest.mark.parametrize(
        "name",
        [
            "Clínica Dental Alicante",
            "Clínica Veterinaria Gijón",
            "Nefer Clínica de Medicina Estética",
            "Helipuerto Hospital Universitario de Cabueñes",
        ],
    )
    def test_the_pre_existing_refusals_still_hold(self, name):
        assert _rules().rejects(_place(name))


class TestARealHospitalIsStillTaken:
    @pytest.mark.parametrize(
        "name",
        [
            "Hospital Universitario San Agustin",
            "Hospital Universitario Central de Asturias",
            # Google returns English names for Spanish hospitals.
            "Central University Hospital of Asturias",
            "Hospital of Cabueñes",
            "Monte Naranco Hospital",
            "Aviles Hospital Foundation",
            # "hospitalario"/"hospitalaria" contains "hospital".
            "Complejo Hospitalario Universitario de Ourense",
            "Complexo Hospitalario Universitario A Coruña",
            "H.U. Central de Asturias",
            "Hospital de Jarrio",
            "Hospital Valle del Nalón",
            "Hospital Público da Mariña",
        ],
    )
    def test_it_is_accepted(self, name):
        assert not _rules().rejects(_place(name))


class TestNothingQualifyingIsNotFoundAndNotZero:
    """#98, in the shape the hospital preset can now reach it.

    Narrowing the rules means some remote listing will have no acceptable
    hospital in Google's page. That must arrive as "not found" -- which the
    scorer drops -- and never as a failure and never as a nearby value.
    """

    def _service(self):
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        return service

    def _response(self, results):
        class _Response:
            status_code = 200

            def json(self):
                return {"status": "OK", "results": results}

        return _Response()

    def test_a_page_of_clinics_is_an_answer_not_a_failure(self, app):
        service = self._service()
        clinics = [
            _place(name)
            for name in ("Centro de Salud de Cudillero", "Consultorio de Malleza")
        ]

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                return_value=self._response(clinics),
            ):
                lookup = service._nearest_place_for_preset(
                    SALAMIR_LAT,
                    SALAMIR_LON,
                    "hospital",
                    _google_path("hospital"),
                )

        assert lookup.place is None, "a refused clinic must not become the hospital"
        assert lookup.failure is None, (
            "everything refused is Google answering, not Google refusing (#98)"
        )

    def test_a_refused_clinic_is_never_cached_as_the_hospital(self, app):
        """Nothing accepted means nothing written, so the next run re-asks."""
        service = self._service()
        written = []

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            return_value=None,
        ):
            with patch(
                "services.property_travel_service.cache_enrichment_data",
                side_effect=lambda *a, **kw: written.append(a),
            ):
                with patch(
                    "utils.google_spend.request_with_retries",
                    return_value=self._response(
                        [_place("Centro de Salud - Muros de Nalon")]
                    ),
                ):
                    service._nearest_place_for_preset(
                        SALAMIR_LAT,
                        SALAMIR_LON,
                        "hospital",
                        _google_path("hospital"),
                    )

        assert written == [], "a refusal must not be cached as a measurement"


class TestTheRulesChangeInvalidatesTheOldCache:
    """187 rows were resolved under the old rules; their cache must not serve.

    `_nearest_place` folds the rules' signature into the cache key precisely so
    that a place cached before a preset learned to refuse it stops coming back
    -- the same guarantee #171 needed for helipads.
    """

    def test_the_hospital_cache_key_carries_the_rules_signature(self, app):
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        seen = []

        class _Response:
            status_code = 200

            def json(self):
                return {"status": "OK", "results": [_place(SAN_AGUSTIN)]}

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            side_effect=lambda lat, lon, cache_type: seen.append(cache_type),
        ):
            with patch(
                "utils.google_spend.request_with_retries",
                return_value=_Response(),
            ):
                service._nearest_place_for_preset(
                    SALAMIR_LAT, SALAMIR_LON, "hospital", _google_path("hospital")
                )

        assert seen and seen[0] != "places_nearest_v1:hospital", (
            "the hospital key must change when the preset's rules change"
        )
