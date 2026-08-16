"""A former hospital and a street named after one are not hospitals.

The tail left by #323 (refuse primary care) and #328 (Text Search fallback).
Three names in the owner's 396 rows still carried the word "hospital" through
the require rule while not being a hospital anyone can be driven to -- and the
interesting part is that checking them against the CNH catalogue
(`data/hospitals_cnh.json`, the Ministry of Health's hospital register, which
this repository already ships for the QoL card) reversed the verdict on one of
the three.

Measured 2026-08-15, each place's stored coordinate against CNH:

* **"Antiguo Hospital"** (43.0126463,-7.5694497), 2 rows -- the *old* hospital
  building in Lugo. Nearest working hospital: Hospital Quirón Salud Lugo 0.6 km
  further, Complexo Hospitalario Universitario de Lugo (817 beds) 3.0 km. A
  building, not a service. Refused.
* **"Ronda Hospital FE 13"** (43.5101180,-8.2192715), 1 row -- an address in
  Ferrol, 2.4 km from Complexo Hospitalario Universitario de Ferrol (469 beds).
  Recorded at 8 minutes while the hospital itself is further out, so this one
  understated real access. Refused.
* **"Santo Hospital de Caridad"** (43.4803537,-8.2025734), 11 rows -- **kept**.
  The name reads like a charity house and the ticket that opened this proposed
  refusing it, but the coordinate is **0.0 km** from Hospital Ribera Juan
  Cardona in CNH (150 beds): the same working hospital under the name it was
  founded with, which Google indexes as a second place. The measurement was
  already right.

That last one is the reason this file exists rather than two lines in the
preset: the rule "if the name sounds historic, refuse it" would have thrown
away eleven correct measurements.
"""

import json
import math
import os

import pytest

from tests import setup_test_environment

setup_test_environment()

from services.place_rules import place_rules_from  # noqa: E402
from services.search_profile_service import TRAVEL_PRESET_DEFS  # noqa: E402

CNH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "hospitals_cnh.json",
)

# Verbatim from the owner's database, 2026-08-15.
ANTIGUO_HOSPITAL = ("Antiguo Hospital", 43.0126463, -7.5694497)
RONDA_HOSPITAL = ("Ronda Hospital FE 13", 43.5101180, -8.2192715)
SANTO_HOSPITAL = ("Santo Hospital de Caridad", 43.4803537, -8.2025734)


def _rules():
    rules = place_rules_from(TRAVEL_PRESET_DEFS["hospital"])
    assert rules is not None
    return rules


def _place(name):
    return {
        "name": name,
        "place_id": f"place-{name}",
        "types": ["hospital", "health", "point_of_interest", "establishment"],
    }


def _km(lat_a, lon_a, lat_b, lon_b):
    radius_km = 6371.0
    d_lat = math.radians(lat_b - lat_a)
    d_lon = math.radians(lon_b - lon_a)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat_a))
        * math.cos(math.radians(lat_b))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(h))


def _nearest_cnh(lat, lon):
    """The closest hospital in the shipped CNH register, and how far."""
    with open(CNH_PATH, encoding="utf-8") as handle:
        rows = json.load(handle)["hospitals"]
    located = [r for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    assert located, "the CNH reference file must carry coordinates"
    return min(((_km(lat, lon, r["lat"], r["lon"]), r) for r in located))


class TestAFormerHospitalIsNotAHospital:
    @pytest.mark.parametrize(
        "name",
        [
            "Antiguo Hospital",
            "Antiguo Hospital de Santiago",
            # Galician spelling: the register and the signage both use it.
            "Antigo Hospital de San Roque",
        ],
    )
    def test_it_is_refused(self, name):
        assert _rules().rejects(_place(name))

    def test_the_lugo_building_is_not_the_lugo_hospital(self):
        """0.6 km to a working hospital, so refusing costs almost no distance."""
        _, lat, lon = ANTIGUO_HOSPITAL

        distance_km, nearest = _nearest_cnh(lat, lon)

        assert distance_km > 0.1, (
            f"if it were the hospital itself this refusal would be wrong; "
            f"nearest CNH is {nearest['name']} at {distance_km:.1f} km"
        )


class TestAStreetNamedAfterAHospitalIsNotOne:
    def test_the_ferrol_address_is_refused(self):
        assert _rules().rejects(_place(RONDA_HOSPITAL[0]))

    def test_it_really_was_understating_access(self):
        """2.4 km from the actual hospital, and it was recorded at 8 minutes."""
        _, lat, lon = RONDA_HOSPITAL

        distance_km, _nearest = _nearest_cnh(lat, lon)

        assert distance_km > 1.0

    @pytest.mark.parametrize(
        "name",
        [
            # `ronda` is a ring road, so the collision is one-directional: a
            # hospital named after the town of Ronda survives.
            "Hospital de Ronda",
            "Hospital Comarcal de la Serranía, Ronda",
        ],
    )
    def test_a_hospital_in_a_town_called_ronda_is_still_taken(self, name):
        assert not _rules().rejects(_place(name))


class TestTheHistoricNameThatIsAWorkingHospital:
    """The one the catalogue saved. See this module's docstring."""

    def test_santo_hospital_de_caridad_is_accepted(self):
        assert not _rules().rejects(_place(SANTO_HOSPITAL[0]))

    def test_it_shares_a_coordinate_with_a_registered_hospital(self):
        """0.0 km from Hospital Ribera Juan Cardona: the same site.

        This is the evidence for keeping it, so it is asserted rather than
        described. If Google ever moves the place away from the hospital, the
        justification is gone and this test says so.
        """
        _, lat, lon = SANTO_HOSPITAL

        distance_km, nearest = _nearest_cnh(lat, lon)

        assert distance_km < 0.2, (
            f"expected the Juan Cardona site, got {nearest['name']} "
            f"at {distance_km:.1f} km"
        )
        assert nearest["beds"] and nearest["beds"] >= 100


class TestTheRestOfThePresetIsUnchanged:
    @pytest.mark.parametrize(
        "name",
        [
            "Hospital Universitario San Agustin",
            "Monte Naranco Hospital",
            "Complexo Hospitalario Universitario de Ferrol",
            "Hospital Ribera Juan Cardona",
            "Hospital de Jarrio",
        ],
    )
    def test_a_real_hospital_is_still_taken(self, name):
        assert not _rules().rejects(_place(name))

    @pytest.mark.parametrize(
        "name",
        [
            "Centro de Salud - Muros de Nalon",
            "Hospital de Día Médico",
            "Unidad de Hospitalización de Salud Mental",
            "Clínica Dental Alicante",
        ],
    )
    def test_the_earlier_refusals_still_hold(self, name):
        assert _rules().rejects(_place(name))
