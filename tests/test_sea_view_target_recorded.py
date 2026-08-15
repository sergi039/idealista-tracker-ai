"""A sea-view verdict has to say what water it looked at.

The "Selorio report" of 2026-08-15, referred to below, arrived as a direct
owner request rather than a GitHub issue, so it carries no issue number.

The ticket behind this file reported property 125 -- "Land in Selorio - Tornón,
Villaviciosa", `likely`, `clear_line_of_sight`, 4173.8 m -- as scoring a sea
view against the ría de Villaviciosa instead of the open Cantabrian. Measured
against real OpenStreetMap data, the mechanism it described does not exist
here: `natural=coastline` does **not** follow these estuaries inland. It closes
across each mouth, and the ría is mapped separately as a `natural=water`
multipolygon reaching kilometres further in (Villaviciosa 7.13 km, Avilés 4.60,
Navia 4.90, Nalón 4.37 and 7.29). So the nearest coastline to an inland plot is
the mouth -- open sea -- and property 125's verdict is correct on the geometry.

What was missing is what a reader could *do* with it. The verdict stored a
distance and a bearing, and answering "what water is that?" from those two
numbers took a day of OSM archaeology; even then the reconstruction lands
~4 m off the node actually chosen, because both are stored rounded to one
decimal. So the verdict now records the point, the page and the export show it,
and a `likely` that rests on terrain alone is named for the terrain rather than
asserting a view -- because on this coast that line of sight can run 2.8 km up
an estuary channel before it reaches the sea.

The coastline here is the real thing (`tests/data/`), so these tests measure
what production measured. Nothing touches the network -- Overpass was in fact
unreachable from the development machine on the capture date, and
`tests/network_guard.py` would refuse the call regardless.
"""

import csv
import io
import json
from pathlib import Path

import pytest

from app import create_app, db
from models import Property
from services import sea_view_service as svc
from tests import setup_test_environment

_FIXTURE = json.loads(
    (Path(__file__).parent / "data" / "coastline_ria_villaviciosa.json").read_text()
)

# Every coastline node in the fixture, flattened the way `fetch_coastline_points`
# returns them.
COASTLINE = [(lat, lon) for way in _FIXTURE["ways"] for lat, lon in way["geometry"]]

# Property 125, exactly as the database holds it.
PROPERTY_125 = (43.4981229, -5.4009101)

# The node `evaluate_geometry` picks out of COASTLINE for it: the southern end
# of the line that closes the ría mouth at Rodiles.
MOUTH_NODE = (43.534121, -5.386247)

# The inland limit of the Ría de Villaviciosa water multipolygon (OSM relation
# 2166590). Nothing in COASTLINE comes near it -- that is the finding.
RIA_INLAND_LIMIT_LAT = 43.4838

# EU-DEM 25 m along the sight line from PROPERTY_125 to MOUTH_NODE, read from
# OpenTopoData on 2026-08-15: the observer's hill, then the ría's tidal flats at
# roughly sea level, the El Puntal dune ridge at 13.9 m, and the mouth. 28
# values because `_profile_sample_count(4173.8)` is 27, plus the observer.
EUDEM_PROFILE = [
    110.8885269165039,
    50.539283752441406,
    28.204587936401367,
    33.38331604003906,
    31.046361923217773,
    28.267234802246094,
    23.09457778930664,
    20.20933723449707,
    8.842016220092773,
    0.25574594736099243,
    -2.9461023807525635,
    -2.0269615650177,
    -1.1390795707702637,
    0.010950420051813126,
    0.0,
    0.011477073654532433,
    -0.46999797224998474,
    0.01145682018250227,
    0.0,
    0.0,
    0.03807954862713814,
    0.011090355925261974,
    3.985079526901245,
    13.932409286499023,
    10.068267822265625,
    0.0,
    0.0,
    0.030666759237647057,
]


@pytest.fixture
def real_coastline(monkeypatch):
    """The real sources, without the network."""
    monkeypatch.setattr(
        svc, "fetch_coastline_points", lambda lat, lon, session=None: list(COASTLINE)
    )

    def _elevations(points, session=None):
        # Fail loudly rather than pad: a profile of a different length means
        # the sampling changed, and quietly reusing the first N values would
        # keep this test green while measuring a different sight line.
        assert len(points) == len(EUDEM_PROFILE), (
            f"the profile is {len(points)} points but the captured EU-DEM run "
            f"has {len(EUDEM_PROFILE)}"
        )
        return list(EUDEM_PROFILE)

    monkeypatch.setattr(svc, "fetch_elevations", _elevations)


def test_geometry_records_the_point_it_measured_to(real_coastline):
    """Property 125, reproduced -- and now it says where it looked.

    The distance and bearing are the ones in the database, so this is the same
    computation production ran. The target is the assertion that matters: it is
    the mouth-closing node, 5.7 km seaward of the ría's inland limit, which is
    what makes "the nearest coastline is the estuary" false here.
    """
    detail = svc.evaluate_geometry(*PROPERTY_125, "precise", use_cache=False)

    assert detail["state"] == svc.LIKELY
    assert detail["reason"] == "clear_line_of_sight"
    assert detail["distance_m"] == pytest.approx(4173.8, abs=0.05)
    assert detail["bearing_deg"] == pytest.approx(16.5, abs=0.05)

    assert (detail["target_lat"], detail["target_lon"]) == MOUTH_NODE
    assert detail["target_lat"] > RIA_INLAND_LIMIT_LAT


def test_the_target_is_not_recoverable_from_distance_and_bearing(real_coastline):
    """Why the point is stored rather than left to be re-derived.

    Both numbers are rounded to one decimal before they are written, so casting
    the ray back out lands metres away from the node that was actually chosen.
    Metres are enough to put the reconstruction on the far bank of a channel
    300 m wide, which is the whole question this ticket asked.
    """
    detail = svc.evaluate_geometry(*PROPERTY_125, "precise", use_cache=False)

    import math

    bearing = math.radians(detail["bearing_deg"])
    angular = detail["distance_m"] / svc.EARTH_RADIUS_M
    phi1 = math.radians(PROPERTY_125[0])
    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular)
        + math.cos(phi1) * math.sin(angular) * math.cos(bearing)
    )
    lambda2 = math.radians(PROPERTY_125[1]) + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    rebuilt = (math.degrees(phi2), math.degrees(lambda2))

    drift = svc.haversine_m(rebuilt[0], rebuilt[1], *MOUTH_NODE)
    assert drift > 1.0, "if rounding stopped losing the point, say so here"
    assert (
        svc.haversine_m(detail["target_lat"], detail["target_lon"], *MOUTH_NODE) < 0.2
    )


def test_a_refusal_still_records_no_target(monkeypatch):
    """No coastline, no target -- and no zeros standing in for one.

    The #98 rule in this file's own currency: a source that refused knows
    nothing about this property, so it must not leave coordinates behind that
    a page would happily draw on a map.
    """

    def _refuse(lat, lon, session=None):
        raise svc.SeaViewSourceError("Overpass returned HTTP 504")

    monkeypatch.setattr(svc, "fetch_coastline_points", _refuse)

    detail = svc.evaluate_geometry(*PROPERTY_125, "precise", use_cache=False)
    assert detail["state"] == svc.UNKNOWN
    assert detail["reason"] == "coastline_source_unavailable"
    assert "target_lat" not in detail and "target_lon" not in detail


def test_no_coastline_in_range_records_no_target(monkeypatch):
    """A *measured* negative has no point to report either."""
    monkeypatch.setattr(
        svc, "fetch_coastline_points", lambda lat, lon, session=None: []
    )

    detail = svc.evaluate_geometry(*PROPERTY_125, "precise", use_cache=False)
    assert detail["state"] == svc.NO
    assert detail["reason"] == "no_coastline_in_range"
    assert "target_lat" not in detail and "target_lon" not in detail


# --- how the verdict is named ------------------------------------------------


@pytest.mark.parametrize(
    "verdict,expected",
    [
        # Terrain alone. It says the ground does not block the line, and on
        # this coast the line can run up an estuary; do not call that a view.
        ({"state": "likely", "source": "geometry"}, "likely_geometry"),
        # The listing itself claims one. That is a different claim and keeps
        # its own wording, including when the terrain disagrees with it.
        ({"state": "likely", "source": "text"}, "likely"),
        ({"state": "yes", "source": "text+geometry"}, "yes"),
        ({"state": "no", "source": "geometry"}, "no"),
        ({"state": "unknown", "source": "none"}, "unknown"),
        # A hand-set verdict outranks both models and is never softened.
        ({"state": "likely", "source": "manual"}, "likely"),
    ],
)
def test_state_label_key(verdict, expected):
    assert svc.state_label_key(verdict) == expected


# --- reading it back out -----------------------------------------------------


class _StoredProperty:
    def __init__(self, environment):
        self.environment = environment


def _verdict_with_geometry(geometry):
    return _StoredProperty(
        {
            "sea_view": "likely",
            "sea_view_detail": {"source": "geometry", "geometry": geometry},
        }
    )


def test_read_verdict_exposes_the_target():
    verdict = svc.read_verdict(
        _verdict_with_geometry(
            {"target_lat": 43.534121, "target_lon": -5.386247, "distance_m": 4173.8}
        )
    )
    assert verdict["target"] == {"lat": 43.534121, "lon": -5.386247}
    assert verdict["distance_m"] == 4173.8


@pytest.mark.parametrize(
    "geometry",
    [
        # A verdict computed before the target was recorded. Most rows, today.
        {"distance_m": 4173.8},
        # `enrichment` is a JSON column; anything may have written into it.
        {"target_lat": "43.53", "target_lon": -5.386247},
        {"target_lat": True, "target_lon": -5.386247},
        {"target_lat": 43.534121, "target_lon": None},
        {"target_lat": 943.5, "target_lon": -5.386247},
        {"target_lat": 43.534121, "target_lon": -999.0},
        {"target_lat": float("nan"), "target_lon": -5.386247},
    ],
)
def test_read_verdict_refuses_a_target_it_cannot_trust(geometry):
    assert svc.read_verdict(_verdict_with_geometry(geometry))["target"] is None


# --- the surfaces ------------------------------------------------------------


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
def property_125(app):
    """Property 125's stored shape, target included."""
    with app.app_context():
        from services.search_profile_service import SearchProfileService

        profile = SearchProfileService.get_default_profile(create=True)
        prop = Property(
            source_email_id="sea_view_target_selorio",
            title="Land in Selorio - Tornón, Villaviciosa",
            url="https://www.idealista.com/inmueble/109107202/",
            municipality="Villaviciosa",
            search_profile_id=profile.id,
            location_lat=PROPERTY_125[0],
            location_lon=PROPERTY_125[1],
            location_accuracy="precise",
            enrichment={
                "environment": {
                    "sea_view": "likely",
                    "sea_view_detail": {
                        "source": "geometry",
                        "reason": "terrain allows a view, listing text does not claim one",
                        "geometry": {
                            "state": "likely",
                            "reason": "clear_line_of_sight",
                            "distance_m": 4173.8,
                            "bearing_deg": 16.5,
                            "observer_elevation_m": 110.9,
                            "target_lat": MOUTH_NODE[0],
                            "target_lon": MOUTH_NODE[1],
                        },
                    },
                }
            },
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


def test_detail_page_names_the_terrain_and_links_the_target(app, property_125):
    page = app.test_client().get(f"/properties/{property_125}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)

    assert "Terrain allows a sea view" in body
    # The old wording survives in exactly one place -- the hand-set dropdown,
    # where the owner is choosing a *state* by name and "Terrain allows..."
    # would be the wrong thing to pick. Anywhere else it is the badge speaking
    # for the terrain again.
    stale = [
        line
        for line in body.splitlines()
        if "Sea view likely" in line and "<option" not in line
    ]
    assert stale == []
    # The point, pinned on a map, at the precision maps_place_url writes.
    assert "43.534121%2C-5.386247" in body


def test_list_badge_names_the_terrain_too(app, property_125):
    """The list draws the same badge from the same rule.

    Both list views share one macro, and both hit it: the row is rendered by
    the card view and the table view alike, so a page that opens on the table
    (which a bare /properties does) must not go on asserting a sea view after
    the detail page stopped.
    """
    page = app.test_client().get("/properties?profile_id=all")
    assert page.status_code == 200
    body = page.get_data(as_text=True)

    assert "Terrain allows a sea view" in body
    assert "Sea view likely" not in body


def test_csv_export_carries_the_target(app, property_125):
    response = app.test_client().get("/properties/export.csv?profile_id=all")
    assert response.status_code == 200

    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    header, row = rows[0], rows[1]
    picked = dict(zip(header, row))

    assert picked["Sea View"] == "likely"
    assert picked["Sea View Distance (m)"] == "4173.8"
    assert picked["Sea View Target Lat"] == str(MOUTH_NODE[0])
    assert picked["Sea View Target Lon"] == str(MOUTH_NODE[1])
