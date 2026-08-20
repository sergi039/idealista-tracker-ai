"""Hazardous neighbours (#437) — the acceptance matrix the issue set.

The fixture is not written by the same hand as the parser: it is the verbatim
answer `overpass.openstreetmap.fr` gave on 2026-08-20 for the ten candidate
tags within 6 km of property 793's own coordinate (43.5702843, -5.7276638),
144 elements, committed as `tests/data/osm_hazards_xivares_793.json`. That
matters for the reason `tests/test_fotocasa_source.py` gives about its own
fixture — a rules table tested against data invented alongside it tests the
author's idea of the data.

What must never break:

* the rules table refuses `Alskin Cosmetics`, `Neoalgae`, `Fábrica de Hielo`
  and `Talleres Prendes`, and accepts `Tudela Veguín`, `Aboño`,
  `Repsol Butano` and `ArcelorMittal`;
* `Turbina A` and `Turbina B` collapse into the ArcelorMittal facility rather
  than standing as two more hazards;
* an Overpass refusal is `unavailable` and never an empty list, and the page
  says it was not measured rather than showing nothing;
* an approximate origin produces a band and never a point distance, and the
  surfaces caption it;
* the criterion ships at weight 0.0 in every category;
* there is no second Overpass client, no second copy of the place rules and
  no second coordinate-quality policy.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import hazard_rules, hazard_service
from services.hazard_service import HazardService
from services.property_scoring_service import (
    HousingPropertyScorer,
    PropertyScoringService,
)
from tests import setup_test_environment

# Captured before the conftest stub is applied: `pytest_runtest_setup` runs
# after collection, so the name bound here is the real transport-backed one.
# The same trick `tests/test_osm_places.py` reaches for with `importlib.reload`,
# without the reload's side effect of replacing the class other modules hold.
_REAL_FETCH = hazard_service.fetch_elements

FIXTURE = json.loads(
    (Path(__file__).parent / "data" / "osm_hazards_xivares_793.json").read_text(
        encoding="utf-8"
    )
)
XIVARES = (43.5702843, -5.7276638)


def _tagged(name):
    """Every fixture element whose `name` is exactly this, tags only."""
    return [
        element.get("tags") or {}
        for element in FIXTURE["elements"]
        if (element.get("tags") or {}).get("name") == name
    ]


class _FakeEnrichment:
    """Stands in for `EnrichmentService`, answering the one method used."""

    def __init__(self, elements=None, failure=None):
        self.elements = elements
        self.failure = failure
        self.queries = []

    def _overpass_elements(self, query):
        self.queries.append(query)
        if self.failure is not None:
            return None, SimpleNamespace(reason=self.failure)
        return self.elements, None


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


@pytest.fixture
def real_fetch(monkeypatch):
    """Undo the conftest stub for this file, keeping the cache out of the way.

    The cache is a no-op here on purpose: a test that measured the same
    coordinate twice would otherwise be answering from the first run's entry
    and would go on passing with the transport removed.
    """
    monkeypatch.setattr(hazard_service, "fetch_elements", _REAL_FETCH)
    monkeypatch.setattr(
        hazard_service, "get_cached_enrichment_data", lambda *a, **k: None
    )
    monkeypatch.setattr(hazard_service, "cache_enrichment_data", lambda *a, **k: None)


@pytest.fixture
def profile(app):
    """A live subscription, because a bare `/properties` draws those.

    Rows with no subscription land under `unassigned`, which the page offers
    but does not open on -- so a list test without one asserts against an
    empty table and passes for the wrong reason.
    """
    profile = SearchProfile(name="Hazard fixtures", is_active=True)
    db.session.add(profile)
    db.session.commit()
    return profile


def _prop(**overrides):
    fields = dict(
        source_email_id=f"hazard-{overrides.get('title', 'x')}",
        title="HazardFixture",
        municipality="Carreño",
        location_lat=XIVARES[0],
        location_lon=XIVARES[1],
        location_accuracy="precise",
        property_category="land",
    )
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def _measure(app, real_fetch, **kwargs):
    service = HazardService(
        enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"], **kwargs)
    )
    return service.measure(*XIVARES)


class TestTheRulesTable:
    """A tag is not a severity — measured on the live answer, not invented."""

    @pytest.mark.parametrize(
        "name",
        ["Alskin Cosmetics", "Neoalgae", "Fábrica de Hielo", "Talleres Prendes"],
    )
    def test_noise_on_the_hazard_tags_is_refused(self, name):
        tags = _tagged(name)
        assert tags, f"{name} is not in the fixture any more"
        for tag_set in tags:
            assert hazard_rules.classify(tag_set) is None, tag_set

    @pytest.mark.parametrize(
        "name,kind",
        [
            ("Fábrica de Cementos Tudela Veguín", "cement_works"),
            ("Central Térmica de Aboño", "power_plant"),
            ("Factoría Repsol Butano", "lpg_storage"),
            ("Acería de Veriña - ArcelorMittal", "steelworks"),
        ],
    )
    def test_the_real_facilities_are_accepted(self, name, kind):
        tags = _tagged(name)
        assert tags, f"{name} is not in the fixture any more"
        verdicts = [hazard_rules.classify(tag_set) for tag_set in tags]
        assert all(verdict is not None for verdict in verdicts), name
        assert kind in {verdict.kind for verdict in verdicts}

    def test_zoning_alone_never_qualifies(self):
        """`landuse=industrial` with nothing else is a claim about zoning."""
        assert hazard_rules.classify({"landuse": "industrial"}) is None
        assert (
            hazard_rules.classify(
                {"landuse": "industrial", "name": "Polígono Industrial Somonte"}
            )
            is None
        )
        assert hazard_rules.classify({"man_made": "works"}) is None

    def test_an_unclaimed_tank_is_not_a_hazard_and_a_declared_one_is(self):
        assert (
            hazard_rules.classify({"man_made": "storage_tank", "building": "yes"})
            is None
        )
        verdict = hazard_rules.classify(
            {"man_made": "storage_tank", "content": "LNG", "building": "industrial"}
        )
        assert verdict is not None and verdict.kind == "lng_terminal"

    def test_a_wind_farm_is_not_a_hazard_and_a_coal_plant_is(self):
        assert hazard_rules.classify({"power": "plant", "plant:source": "wind"}) is None
        assert hazard_rules.classify({"power": "plant"}) is None
        verdict = hazard_rules.classify({"power": "plant", "plant:source": "coal"})
        assert verdict is not None and verdict.severity == hazard_rules.SEVERITY_HIGH

    def test_a_name_never_downgrades_what_the_tag_states(self):
        """The more severe of the two verdicts wins, and the tag can be it.

        A gas tank standing inside a sewage works is an ordinary thing to
        find, and its name says *depuradora* -- a moderate nuisance -- while
        its own `content=gas` says high. Reading the name first and stopping
        there reported the moderate one; understating a real hazard is
        strictly worse than reporting a spurious one (review, 2026-08-20).
        """
        tank = {
            "man_made": "storage_tank",
            "content": "gas",
            "name": "Deposito de la Depuradora Municipal",
        }
        by_name_only = hazard_rules._name_verdict(tank)
        assert by_name_only is not None
        assert by_name_only.severity == hazard_rules.SEVERITY_MODERATE

        verdict = hazard_rules.classify(tank)
        assert verdict is not None
        assert verdict.kind == "lpg_storage"
        assert verdict.severity == hazard_rules.SEVERITY_HIGH

    def test_the_ambiguous_quarry_name_is_gone(self):
        """`cantera` is also the word for a club's youth academy.

        It caught nothing on the measured data -- both quarries at property
        793 are tagged `landuse=quarry` -- and misfired on an everyday word.
        """
        assert (
            hazard_rules.classify(
                {"landuse": "industrial", "name": "Poligono La Cantera"}
            )
            is None
        )
        by_tag = hazard_rules.classify(
            {"landuse": "quarry", "name": "Cantera de Abono"}
        )
        assert by_tag is not None and by_tag.kind == "quarry"

    def test_a_name_is_read_as_words_and_not_as_a_substring(self):
        """`quimica` inside *Bioquímica* is a different word."""
        assert (
            hazard_rules.classify(
                {
                    "landuse": "industrial",
                    "industrial": "laboratory",
                    "name": "Laboratorio de Bioquimica Analitica S.L.",
                }
            )
            is None
        )
        # The plural still counts, which is what the stems are for.
        verdict = hazard_rules.classify(
            {"landuse": "industrial", "name": "Industrias Quimicas del Norte"}
        )
        assert verdict is not None and verdict.kind == "chemical_works"

    def test_a_rename_is_not_a_closure(self):
        """`was:name` records a former name, not a former plant.

        The steelworks in the fixture has been Ensidesa, Aceralia, Arcelor and
        ArcelorMittal in turn, so a contributor recording that on this very
        object is entirely plausible -- and used to erase it.
        """
        verdict = hazard_rules.classify(
            {
                "landuse": "industrial",
                "industrial": "steelmaking",
                "name": "Aceria de Verina - ArcelorMittal",
                "was:name": "Ensidesa",
            }
        )
        assert verdict is not None and verdict.kind == "steelworks"

    def test_history_underneath_is_not_history_on_top(self):
        """`historic=archaeological_site` describes the ground, not the tip."""
        verdict = hazard_rules.classify(
            {"landuse": "landfill", "historic": "archaeological_site"}
        )
        assert verdict is not None and verdict.kind == "landfill"
        assert (
            hazard_rules.classify({"landuse": "landfill", "historic": "monument"})
            is None
        )

    def test_a_plant_that_says_what_it_makes_is_read(self):
        """`product=*` is the most direct claim an industrial plant can make.

        `relation/11519713` is *Asturiana de Zinc* in San Juan de Nieva -- a
        zinc smelter and sulfuric-acid plant inside the owner's own search
        area, `man_made=works` + `product=zinc` + `operator=Glencore`, with a
        name no industry vocabulary can read. It classified as nothing at all
        until `product` was read (codex review, 2026-08-20).
        """
        verdict = hazard_rules.classify(
            {
                "man_made": "works",
                "name": "Asturiana de Zinc",
                "operator": "Glencore",
                "product": "zinc",
                "type": "multipolygon",
            }
        )
        assert verdict is not None
        assert verdict.severity == hazard_rules.SEVERITY_HIGH
        assert verdict.evidence == "product=zinc"

    def test_a_closed_power_station_is_not_an_emitting_one(self):
        """Spain shut both of these on 2020-06-30, and both are still mapped.

        `way/16851312` (Narcea) and `way/88799255` (Meirama) carry
        `disused:power=plant`, `disused:plant:source=coal` and `end_date`, on
        an element still tagged `landuse=industrial` and still named *Central
        térmica* -- so the name rule reported them as live (codex review).
        """
        narcea = {
            "disused:plant:method": "combustion",
            "disused:plant:source": "coal",
            "disused:power": "plant",
            "end_date": "2020-06-30",
            "landuse": "industrial",
            "name": "Central termica del Narcea",
        }
        assert hazard_rules.classify(narcea) is None
        # Each half refuses on its own: a mapper who wrote only one still gets
        # the right answer.
        assert hazard_rules.classify({**narcea, "end_date": ""}) is None
        no_prefixes = {
            key: value for key, value in narcea.items() if not key.startswith("disused")
        }
        assert hazard_rules.classify(no_prefixes) is None
        # And a lifecycle prefix on something that was never evidence is not a
        # closure -- a disused siding at a live steelworks.
        alive = hazard_rules.classify(
            {
                "landuse": "industrial",
                "industrial": "steelmaking",
                "disused:railway": "rail",
                "name": "Aceria",
            }
        )
        assert alive is not None and alive.kind == "steelworks"

    def test_a_galician_depuradora_may_be_purifying_shellfish(self):
        """`way/407548492` and `way/498273059` are cetáreas, not sewage works."""
        assert (
            hazard_rules.classify(
                {"landuse": "industrial", "name": "Depuradora e Cetaria de Mariscos"}
            )
            is None
        )
        real = hazard_rules.classify(
            {
                "landuse": "industrial",
                "name": "Depuradora de Aguas Residuales de Villaviciosa",
            }
        )
        assert real is not None and real.kind == "wastewater_plant"

    def test_a_listed_chimney_is_history_not_combustion(self):
        tags = _tagged("Antigua chimenea de Cristasa")
        assert tags and tags[0].get("historic")
        assert hazard_rules.classify(tags[0]) is None


class TestGrouping:
    def test_turbines_collapse_into_the_facility(self, app, real_fetch):
        measurement = _measure(app, real_fetch)
        names = [item["name"] for item in measurement["items"]]
        assert "Turbina A" not in names and "Turbina B" not in names

        arcelor = [
            item for item in measurement["items"] if item["name"] == "ArcelorMittal"
        ]
        assert len(arcelor) == 1, names
        assert arcelor[0]["element_count"] >= 4
        assert "steelworks" in arcelor[0]["kinds"]
        assert "power_plant" in arcelor[0]["kinds"]

    def test_the_tank_farm_is_one_facility(self, app, real_fetch):
        measurement = _measure(app, real_fetch)
        repsol = [
            item
            for item in measurement["items"]
            if (item["name"] or "").startswith("Factoría Repsol")
        ]
        assert len(repsol) == 1
        # Fourteen `content=gas` spheres plus the compound around them.
        assert repsol[0]["element_count"] == 15

    def test_the_measured_facilities_are_all_there(self, app, real_fetch):
        measurement = _measure(app, real_fetch)
        assert measurement["status"] == hazard_service.STATUS_OK
        assert measurement["candidates_seen"] == 144
        nearest = measurement["items"][0]
        assert nearest["name"] == "Fábrica de Cementos Tudela Veguín"
        assert 1100 <= nearest["origin_distance_m"] <= 1150
        assert nearest["kind"] == "cement_works"
        assert 150 <= nearest["bearing_deg"] <= 165

    def test_a_qualifying_facility_past_the_radius_is_not_reported(
        self, app, real_fetch
    ):
        """The block claims to have searched `SEARCH_RADIUS_M` and no further.

        Overpass filters by radius itself, so this cannot arrive from a fresh
        query -- it arrives from the cache, which is keyed on the coordinate
        rounded to four decimals, so a neighbouring point's answer reaches a
        little further than this one's own radius. The fixture holds nothing
        qualifying past 6 km, so the element is synthetic on purpose: without
        it the guard is untestable and the assertion is decoration.
        """
        far = {
            "type": "way",
            "id": 999_000_001,
            "center": {"lat": XIVARES[0] + 0.063, "lon": XIVARES[1]},
            "tags": {"landuse": "landfill", "name": "Vertedero Lejano"},
        }
        near = {
            "type": "way",
            "id": 999_000_002,
            "center": {"lat": XIVARES[0] + 0.045, "lon": XIVARES[1]},
            "tags": {"landuse": "landfill", "name": "Vertedero Cercano"},
        }
        service = HazardService(
            enrichment_service=_FakeEnrichment(elements=[far, near])
        )
        measurement = service.measure(*XIVARES)
        names = [item["name"] for item in measurement["items"]]
        assert "Vertedero Cercano" in names
        assert "Vertedero Lejano" not in names
        for item in measurement["items"]:
            assert item["origin_distance_m"] <= hazard_rules.SEARCH_RADIUS_M


class TestTruncationIsDisclosed:
    """The one way this feature can quietly show a short list."""

    def test_a_scan_that_reached_the_cap_says_so(self, app, client, real_fetch):
        # `out ... N` truncates in the server's own order, so the elements we
        # get are not the nearest N -- they are simply not all of them.
        filler = [
            {
                "type": "node",
                "id": 10_000 + index,
                "lat": XIVARES[0] + 0.001,
                "lon": XIVARES[1] + 0.001,
                "tags": {"landuse": "industrial"},
            }
            for index in range(hazard_rules.ELEMENT_LIMIT)
        ]
        prop = _prop(title="Truncated")
        HazardService(enrichment_service=_FakeEnrichment(elements=filler)).enrich(
            prop, commit=True
        )
        assert prop.enrichment["hazards"]["truncated"] is True
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert response_is_the_card(body)
        assert "element limit" in body

    def test_a_hazard_osm_could_not_place_makes_the_scan_incomplete(
        self, app, real_fetch
    ):
        """`out center` normally gives every way and relation a point.

        A relation whose geometry does not resolve arrives without one, and
        dropping it silently would be a hazard the scan never mentions
        (codex review, 2026-08-20).
        """
        elements = [
            {
                "type": "relation",
                "id": 50_001,
                "tags": {"landuse": "landfill", "name": "Vertedero sin geometria"},
            }
        ]
        prop = _prop(title="Unplaced")
        HazardService(enrichment_service=_FakeEnrichment(elements=elements)).enrich(
            prop, commit=True
        )
        stored = prop.enrichment["hazards"]
        assert stored["unplaced"] == 1
        assert stored["truncated"] is True
        # And the score refuses rather than calling it a clean neighbourhood.
        score, meta = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None
        assert meta["status"] == "scan_truncated"

    def test_a_tie_between_a_name_and_a_tag_goes_to_the_tag(self):
        """`way/459067378` is `landuse=landfill` named *Escombrera central
        térmica* -- the power station's own spoil tip, not the station."""
        verdict = hazard_rules.classify(
            {"landuse": "landfill", "name": "Escombrera central termica"}
        )
        assert verdict is not None and verdict.kind == "landfill"
        # And a name that is *more* severe than the tag still wins: El Musel's
        # coal yard is mapped `landuse=quarry`.
        coal = hazard_rules.classify(
            {"landuse": "quarry", "name": "Parque de carbones"}
        )
        assert coal is not None and coal.kind == "coal_yard"

    def test_an_element_with_no_readable_centre_does_not_shorten_the_count(
        self, app, real_fetch
    ):
        """Trimming happens after the count, so it cannot fake a full scan."""
        elements = [
            {"type": "node", "id": 1, "tags": {"landuse": "landfill"}}
        ] * hazard_rules.ELEMENT_LIMIT
        service = HazardService(enrichment_service=_FakeEnrichment(elements=elements))
        measurement = service.measure(*XIVARES)
        assert measurement["truncated"] is True
        assert measurement["items"] == []


def response_is_the_card(body):
    return "Industrial neighbours" in body


class TestGroupingBounds:
    def test_one_operator_two_sites_is_two_hazards(self, app, real_fetch):
        """A key says who runs it, never where it is.

        Reproduced in review: two `operator=ArcelorMittal` elements 5.6 km
        apart collapsed into one item, and the far one then wore the near
        one's distance and bearing.
        """
        elements = [
            {
                "type": "way",
                "id": 30_001,
                "lat": XIVARES[0] + 0.010,
                "lon": XIVARES[1],
                "tags": {
                    "landuse": "industrial",
                    "industrial": "steelmaking",
                    "operator": "Enagas",
                },
            },
            {
                "type": "way",
                "id": 30_002,
                "lat": XIVARES[0] + 0.045,
                "lon": XIVARES[1],
                "tags": {"landuse": "landfill", "operator": "Enagas Transporte SAU"},
            },
        ]
        measurement = HazardService(
            enrichment_service=_FakeEnrichment(elements=elements)
        ).measure(*XIVARES)
        assert len(measurement["items"]) == 2
        assert [item["element_count"] for item in measurement["items"]] == [1, 1]

    def test_a_generic_name_never_absorbs_a_specific_one(self, app, real_fetch):
        """`way/231335217` is a quarry named *Cantera*; `way/169318445` is a
        different quarry named *Cantera Blokdegal S.A.*

        Only an operator may absorb another key. A name is just what somebody
        typed, and the generic one swallowed the specific one, reporting two
        workings as one (codex review, 2026-08-20).
        """
        merged = hazard_rules.merge_keys(
            ["cantera", "cantera blokdegal s a"], absorbing=set()
        )
        assert merged["cantera blokdegal s a"] == "cantera blokdegal s a"
        # An operator still absorbs, which is what the acceptance criteria need.
        by_operator = hazard_rules.merge_keys(
            ["arcelormittal", "vertedero arcelormittal"], absorbing={"arcelormittal"}
        )
        assert by_operator["vertedero arcelormittal"] == "arcelormittal"

    def test_one_plant_mapped_in_parts_is_still_one_hazard(self, app, real_fetch):
        """And the bound must not undo what the acceptance criteria ask for."""
        measurement = _measure(app, real_fetch)
        arcelor = [
            item for item in measurement["items"] if item["name"] == "ArcelorMittal"
        ]
        assert len(arcelor) == 1 and arcelor[0]["element_count"] == 6

    def test_a_keyless_cluster_is_a_disc_and_not_a_chain(self, app, real_fetch):
        """Three tanks 400 m apart in a line are not one 800 m facility."""
        points = [0.0, 0.0036, 0.0072]  # ~0 m, ~400 m, ~800 m north
        elements = [
            {
                "type": "way",
                "id": 20_000 + index,
                "lat": XIVARES[0] + offset,
                "lon": XIVARES[1],
                "tags": {"man_made": "storage_tank", "content": "LNG"},
            }
            for index, offset in enumerate(points)
        ]
        measurement = HazardService(
            enrichment_service=_FakeEnrichment(elements=elements)
        ).measure(*XIVARES)
        assert len(measurement["items"]) == 2
        assert [item["element_count"] for item in measurement["items"]] == [2, 1]


class TestHonestAbsence:
    def test_a_refusal_is_unavailable_and_not_an_empty_list(self, app, real_fetch):
        service = HazardService(
            enrichment_service=_FakeEnrichment(failure="http_error")
        )
        measurement = service.measure(*XIVARES)
        assert measurement["status"] == hazard_service.STATUS_UNAVAILABLE
        assert "items" not in measurement
        assert measurement["reason"] == "http_error"

    def test_a_refusal_never_overwrites_a_measurement(self, app, real_fetch):
        prop = _prop(title="RefusalKeepsTheAnswer")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        before = dict(prop.enrichment["hazards"])
        assert before["status"] == hazard_service.STATUS_OK

        HazardService(enrichment_service=_FakeEnrichment(failure="timeout")).enrich(
            prop, commit=True
        )
        after = prop.enrichment["hazards"]
        assert after["status"] == hazard_service.STATUS_OK
        assert after["items"] == before["items"]
        assert after["last_attempt_status"] == hazard_service.STATUS_UNAVAILABLE
        assert after["last_attempt_reason"] == "timeout"

    def test_nothing_nearby_is_a_measurement(self, app, real_fetch):
        service = HazardService(enrichment_service=_FakeEnrichment(elements=[]))
        measurement = service.measure(*XIVARES)
        assert measurement["status"] == hazard_service.STATUS_NONE
        assert measurement["items"] == []

    def test_a_row_with_no_coordinate_says_so(self, app, real_fetch):
        prop = _prop(title="NoCoordinate", location_lat=None, location_lon=None)
        payload = HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        assert payload["status"] == hazard_service.STATUS_NO_COORDINATES

    def test_losing_the_coordinate_does_not_delete_the_measurement(
        self, app, real_fetch
    ):
        """`refresh=True` clears the coordinate before geocoding (#393).

        A refusal leaves it cleared, the free pass then runs on a row with no
        point, and overwriting here would delete an answer that cost a round
        trip -- and, since `no_coordinates` is not retryable, take the row out
        of the backfill's scope for good (review, 2026-08-20).
        """
        prop = _prop(title="LostItsCoordinate")
        service = HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        )
        service.enrich(prop, commit=True)
        before = list(prop.enrichment["hazards"]["items"])

        prop.location_lat = None
        prop.location_lon = None
        db.session.commit()
        payload = service.enrich(prop, commit=True)

        assert payload["status"] == hazard_service.STATUS_OK
        assert payload["items"] == before
        assert payload["last_attempt_status"] == hazard_service.STATUS_NO_COORDINATES

        # Kept in storage, and *not* asserted: a measurement of a place the
        # listing may no longer be near says nothing about its parcel, and a
        # `none_within_radius` block read this way would be a clean
        # neighbourhood for a listing that is nowhere (codex review).
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_STALE_ORIGIN
        assert verdict["measured"] is False
        counted = (
            Property.query.filter(Property.id == prop.id)
            .filter(hazard_service.measured_expression(Property))
            .count()
        )
        assert counted == 0

    def test_a_half_read_block_still_renders(self, app, client):
        """The page must not raise on a shape an older run could have left.

        `routes/main_routes.py` turns a render error into a flash and a second
        render with nothing, so a raising template looks like an empty page
        rather than a failure -- which is why this asserts the card is there
        and not merely that the response was 200.
        """
        prop = _prop(title="HalfRead")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "items": [
                    {"kind": "a_kind_with_no_translation", "severity": "high"},
                    "not even a dict",
                ],
            }
        }
        db.session.commit()
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert response_is_the_card(body)
        assert " km" not in body.split("Industrial neighbours", 1)[1].split("</div>")[0]

    def test_an_unscanned_row_is_not_a_clean_one(self, app):
        prop = _prop(title="NeverScanned")
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False
        assert verdict["flagged"] is False

    def test_the_scope_keeps_a_refusal_and_drops_an_answer(self, app, real_fetch):
        prop = _prop(title="ScopeRules")
        assert hazard_service.needs_hazards(prop) is True
        prop.enrichment = {"hazards": {"status": hazard_service.STATUS_UNAVAILABLE}}
        assert hazard_service.needs_hazards(prop) is True
        prop.enrichment = {"hazards": {"status": hazard_service.STATUS_NONE}}
        assert hazard_service.needs_hazards(prop) is False


class TestApproximateOrigin:
    """5 km of slack destroys "1.1 km" outright (#358)."""

    def test_a_centroid_gets_a_band_and_never_a_point(self, app, real_fetch):
        prop = _prop(title="Centroid", location_accuracy="approximate")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        verdict = hazard_service.read_verdict(prop)
        assert verdict["approximate_origin"] is True
        nearest = verdict["nearest"]
        assert nearest["distance_m"] is None
        assert nearest["min_distance_m"] == 0.0
        assert nearest["max_distance_m"] == pytest.approx(
            nearest["origin_distance_m"] + 5000.0
        )
        # The scan covered 6 km around a point the parcel may be 5 km from.
        assert verdict["guaranteed_m"] == pytest.approx(1000.0)

    def test_a_precise_row_gets_one_number_twice(self, app, real_fetch):
        prop = _prop(title="Precise")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        verdict = hazard_service.read_verdict(prop)
        nearest = verdict["nearest"]
        assert verdict["approximate_origin"] is False
        assert (
            nearest["distance_m"]
            == nearest["min_distance_m"]
            == nearest["max_distance_m"]
        )
        assert verdict["guaranteed_m"] == pytest.approx(
            float(hazard_rules.SEARCH_RADIUS_M)
        )

    def test_a_relabelled_coordinate_is_restated_without_a_rescan(
        self, app, real_fetch
    ):
        """The row's *current* accuracy decides, not the one it was measured
        at -- as long as the point itself has not moved."""
        prop = _prop(title="Relabelled", location_accuracy="approximate")
        service = HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        )
        service.enrich(prop, commit=True)
        assert hazard_service.read_verdict(prop)["nearest"]["distance_m"] is None

        prop.location_accuracy = "precise"
        db.session.commit()
        restated = hazard_service.read_verdict(prop)
        assert restated["approximate_origin"] is False
        assert restated["nearest"]["distance_m"] is not None

    def test_a_moved_coordinate_is_not_restated_at_all(self, app, client, real_fetch):
        """A measurement of the old point is not a measurement of this one.

        `utils/refresh_property_accuracy.py` and a `refresh=True` enrich both
        move the coordinate without touching this block. Restating the old
        distance against the new accuracy printed a centroid's 1.1 km as an
        exact one (review, 2026-08-20).
        """
        prop = _prop(title="Moved", location_accuracy="approximate")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)

        prop.location_lat = XIVARES[0] + 0.02
        prop.location_accuracy = "precise"
        db.session.commit()

        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_STALE_ORIGIN
        assert verdict["measured"] is False
        assert verdict["nearest"] is None
        # And it goes back into the backfill's scope, because a rescan is one
        # free query and the row currently has no answer about where it is.
        assert hazard_service.needs_hazards(prop) is True

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert response_is_the_card(body)
        assert "re-located since the scan" in body
        assert "1.1 km" not in body


class TestOneAnswerInTwoLanguages:
    """`read_verdict` and `measured_expression` must agree, row for row.

    A coverage line that disagrees with the badges under it is a third wrong
    number rather than a disclosure (`services/listing_verification.py` wrote
    that rule down), so the same matrix goes through both readings -- and it
    carries the origin cases, because the moment one side learned about a
    moved coordinate the other had to as well.
    """

    _HERE = {"lat": XIVARES[0], "lon": XIVARES[1]}
    _ELSEWHERE = {"lat": XIVARES[0] + 0.02, "lon": XIVARES[1]}

    @pytest.mark.parametrize(
        "stored,measured",
        [
            (None, False),
            ({"status": hazard_service.STATUS_OK, "items": []}, True),
            ({"status": hazard_service.STATUS_NONE, "items": []}, True),
            ({"status": hazard_service.STATUS_UNAVAILABLE}, False),
            ({"status": hazard_service.STATUS_NO_COORDINATES}, False),
            ({"nonsense": 1}, False),
            # The origin the block was measured from, against the row's own.
            ({"status": hazard_service.STATUS_OK, "items": [], "origin": _HERE}, True),
            (
                {
                    "status": hazard_service.STATUS_OK,
                    "items": [],
                    "origin": _ELSEWHERE,
                },
                False,
            ),
            # Unreadable on either side is not a move, in both languages.
            (
                {"status": hazard_service.STATUS_OK, "items": [], "origin": "junk"},
                True,
            ),
        ],
    )
    def test_python_and_sql_read_the_same_matrix(self, app, stored, measured):
        prop = _prop(title=f"Matrix{measured}{stored}")
        if stored is not None:
            prop.enrichment = {"hazards": stored}
            db.session.commit()
        assert hazard_service.read_verdict(prop)["measured"] is measured

        counted = (
            Property.query.filter(Property.id == prop.id)
            .filter(hazard_service.measured_expression(Property))
            .count()
        )
        assert bool(counted) is measured


class TestTheCriterionShipsWeightless:
    @pytest.mark.parametrize(
        "scorer_name",
        [
            "HousingPropertyScorer",
            "LandPropertyScorer",
            "GaragePropertyScorer",
            "CommercialPropertyScorer",
            "BuildingPropertyScorer",
            "NewDevelopmentPropertyScorer",
        ],
    )
    def test_every_category_ships_it_at_zero(self, scorer_name):
        import services.property_scoring_service as scoring

        scorer = getattr(scoring, scorer_name)
        assert scorer.DEFAULT_INVESTMENT_WEIGHTS["hazard_score"] == 0.0
        assert scorer.DEFAULT_LIFESTYLE_WEIGHTS["hazard_score"] == 0.0

    def test_the_preview_gate_knows_about_it(self):
        from services.property_scoring_service import WEIGHTLESS_SCORE_KEYS

        assert "hazard_score" in WEIGHTLESS_SCORE_KEYS
        assert "pool_score" in WEIGHTLESS_SCORE_KEYS

    def test_the_editor_offers_the_weight_and_its_thresholds(self):
        service = PropertyScoringService()
        assert "hazard_score" in service.WEIGHT_KEYS
        assert service.EDITABLE_SECTIONS["hazard"] == (
            "near_m",
            "far_m",
            "moderate_factor",
        )
        assert service.defaults_for("land")["hazard"]["far_m"] == 5000.0

    def test_hazard_data_moves_no_score_at_the_shipped_weight(self, app, real_fetch):
        prop = _prop(title="Weightless", property_category="housing")
        scoring = PropertyScoringService()
        scoring.calculate_for_property(prop, commit=True)
        before = (prop.score_investment, prop.score_lifestyle, prop.score_total)

        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        scoring.calculate_for_property(prop, commit=True)
        after = (prop.score_investment, prop.score_lifestyle, prop.score_total)
        assert before == after


class TestTheComponent:
    """What the criterion would say once somebody raises the weight."""

    def _score(self, prop, **overrides):
        scorer = HousingPropertyScorer()
        cfg = dict(scorer.DEFAULT_HAZARD)
        cfg.update(overrides)
        return scorer._hazard_score(
            prop,
            near_m=cfg["near_m"],
            far_m=cfg["far_m"],
            moderate_factor=cfg["moderate_factor"],
        )

    def test_a_row_nobody_scanned_scores_none_never_a_clean_hundred(self, app):
        score, meta = self._score(_prop(title="ComponentUnscanned"))
        assert score is None
        assert meta["status"] == hazard_service.STATUS_MISSING

    def test_a_refusal_scores_none(self, app):
        prop = _prop(title="ComponentRefused")
        prop.enrichment = {"hazards": {"status": hazard_service.STATUS_UNAVAILABLE}}
        db.session.commit()
        score, _ = self._score(prop)
        assert score is None

    def test_a_measured_absence_scores_a_hundred_when_the_scan_reached_far_enough(
        self, app, real_fetch
    ):
        prop = _prop(title="ComponentClear")
        HazardService(enrichment_service=_FakeEnrichment(elements=[])).enrich(
            prop, commit=True
        )
        score, meta = self._score(prop)
        assert score == 100.0
        assert meta["status"] == hazard_service.STATUS_NONE

    def test_a_truncated_empty_scan_is_not_a_clean_neighbourhood(self, app, real_fetch):
        """The card discloses `truncated`; the score has to read it too."""
        filler = [
            {
                "type": "node",
                "id": 40_000 + index,
                "lat": XIVARES[0] + 0.001,
                "lon": XIVARES[1] + 0.001,
                "tags": {"landuse": "industrial"},
            }
            for index in range(hazard_rules.ELEMENT_LIMIT)
        ]
        prop = _prop(title="ComponentTruncated")
        HazardService(enrichment_service=_FakeEnrichment(elements=filler)).enrich(
            prop, commit=True
        )
        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_NONE
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == "scan_truncated"

    def test_a_truncated_scan_carrying_items_still_abstains(self, app):
        """The elements a capped scan did not see could be anywhere.

        The first version of this guard sat inside the "no items" branch, so a
        truncated scan carrying one distant facility still scored 100 (codex
        review, 2026-08-20).
        """
        prop = _prop(title="ComponentTruncatedWithItems")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "searched_m": 6000,
                "truncated": True,
                "item_count": 1,
                "items": [
                    {
                        "kind": "landfill",
                        "severity": "high",
                        "origin_distance_m": 5000,
                    }
                ],
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == "scan_truncated"

    def test_an_unreadable_item_is_not_walked_past(self, app):
        """Dropping it reports the rest as the whole picture -- #98 inside one
        listing. Measured: the block below scored 100 (codex review)."""
        prop = _prop(title="ComponentUnreadableItem")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "searched_m": 6000,
                "item_count": 2,
                "items": [
                    {
                        "kind": "landfill",
                        "severity": "high",
                        "origin_distance_m": "bad",
                    },
                    {
                        "kind": "landfill",
                        "severity": "high",
                        "origin_distance_m": 5000,
                    },
                ],
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == "unreadable_item"

    def test_a_cut_list_scores_only_while_the_cut_cannot_matter(self, app):
        """Everything dropped is further away than everything kept.

        So the worst a dropped item can score is the farthest kept item's
        distance at full severity -- and while the worst kept item is at or
        under that bound, the answer is safe. When it is not, it is not
        knowable from what was stored.
        """
        block = {
            "status": hazard_service.STATUS_OK,
            "searched_m": 6000,
            "item_count": 25,
            "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
        }
        safe = _prop(title="ComponentCutSafe")
        safe.enrichment = {
            "hazards": {
                **block,
                "items": [
                    {"kind": "landfill", "severity": "high", "origin_distance_m": 500}
                ],
            }
        }
        db.session.commit()
        assert self._score(safe)[0] == 0.0

        unsafe = _prop(title="ComponentCutUnsafe")
        unsafe.enrichment = {
            "hazards": {
                **block,
                "items": [
                    {
                        "kind": "quarry",
                        "severity": "moderate",
                        "origin_distance_m": 1000,
                    }
                ],
            }
        }
        db.session.commit()
        score, meta = self._score(unsafe)
        assert score is None
        assert meta["status"] == "list_truncated"

    def test_a_stale_origin_scores_none(self, app, real_fetch):
        prop = _prop(title="ComponentMoved")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        prop.location_lat = XIVARES[0] + 0.02
        db.session.commit()
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == hazard_service.STATUS_STALE_ORIGIN

    def test_a_measured_absence_from_a_centroid_scores_none(self, app, real_fetch):
        """The 6 km scan guarantees 1 km around the parcel, and `far_m` is 5."""
        prop = _prop(title="ComponentCentroidClear", location_accuracy="approximate")
        HazardService(enrichment_service=_FakeEnrichment(elements=[])).enrich(
            prop, commit=True
        )
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == "searched_radius_too_small"

    def test_a_measured_hazard_scores_and_a_banded_one_abstains(self, app, real_fetch):
        precise = _prop(title="ComponentPrecise")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(precise, commit=True)
        score, meta = self._score(precise)
        assert score is not None and score < 20.0
        assert meta["kind"] == "cement_works"

        precise.location_accuracy = "approximate"
        db.session.commit()
        banded, banded_meta = self._score(precise)
        assert banded is None
        assert banded_meta["status"] == "approximate_origin"

    def test_a_bad_override_falls_back_and_says_so(self, app, real_fetch):
        from services.property_scoring_service import _resolve_hazard_config

        defaults = HousingPropertyScorer.DEFAULT_HAZARD
        resolved, error = _resolve_hazard_config({"far_m": 100.0}, defaults)
        assert resolved == dict(defaults)
        assert "far_m" in error

    def test_the_override_reaches_the_scorer(self, app, real_fetch, profile):
        """A subscription that turns the weight on gets the weight applied."""
        profile.scoring_config = {
            "categories": {"housing": {"lifestyle": {"hazard_score": 0.5}}}
        }
        db.session.commit()

        prop = _prop(
            title="ComponentWeighted",
            property_category="housing",
            search_profile_id=profile.id,
        )
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        PropertyScoringService().calculate_for_property(prop, commit=True)
        components = prop.scoring["profiles"]["lifestyle"]["components"]
        assert components["hazard_score"] is not None
        assert prop.scoring["profiles"]["lifestyle"]["weights"]["hazard_score"] == 0.5


class TestTheSurfaces:
    def test_the_property_page_says_it_was_not_measured(self, app, client):
        prop = _prop(title="PageUnscanned")
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200, (
            "the page has to render for this to mean anything"
        )
        body = response.get_data(as_text=True)
        assert "Industrial neighbours" in body
        assert "Not scanned yet" in body

    def test_the_property_page_names_the_facility_and_its_bearing(
        self, app, client, real_fetch
    ):
        prop = _prop(title="PageFlagged")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "Fábrica de Cementos Tudela Veguín" in body
        assert "1.1 km" in body
        assert "SSE" in body

    def test_the_property_page_bands_an_approximate_row(self, app, client, real_fetch):
        prop = _prop(title="PageBanded", location_accuracy="approximate")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "0.0–6.1 km" in body
        assert "locality centroid" in body
        assert "1.1 km" not in body

    def test_the_property_page_says_a_refusal_refused(self, app, client, real_fetch):
        prop = _prop(title="PageRefused")
        HazardService(enrichment_service=_FakeEnrichment(failure="blocked")).enrich(
            prop, commit=True
        )
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "OpenStreetMap refused" in body

    def test_the_list_badges_only_a_flagged_row_and_counts_the_rest(
        self, app, client, real_fetch, profile
    ):
        flagged = _prop(title="ListFlagged", search_profile_id=profile.id)
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(flagged, commit=True)
        _prop(title="ListUnscanned", search_profile_id=profile.id)

        body = client.get("/properties").get_data(as_text=True)
        assert body.count('class="badge bg-danger fw-normal"') >= 1
        assert "Industry nearby" in body
        assert "1 of 2 listings scanned" in body

    def test_the_csv_carries_the_verdict(self, app, client, real_fetch, profile):
        prop = _prop(
            title="CsvRow",
            location_accuracy="approximate",
            search_profile_id=profile.id,
        )
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        body = client.get("/properties/export.csv").get_data(as_text=True)
        header, *rows = [line for line in body.splitlines() if line.strip()]
        assert "Nearest Hazard Distance Min (m)" in header
        columns = header.split(",")
        values = rows[0].split(",")
        by_name = dict(zip(columns, values))
        assert by_name["Hazards"] == "ok"
        # Blank on an approximate row, exactly as the page refuses to print it.
        assert by_name["Nearest Hazard Distance (m)"] == ""
        assert by_name["Nearest Hazard Distance Max (m)"]


class TestOneHomePerRule:
    """No second Overpass client, no second rules table, no second policy.

    Pinned the way `tests/test_deploy_page_check_shared.py` pins its own shared
    contract: a rule in two places is one that eventually ships half-changed.
    """

    def test_the_hazard_service_owns_no_transport(self):
        source = Path(hazard_service.__file__).read_text(encoding="utf-8")
        assert "requests" not in source
        assert "OVERPASS_GATE" not in source
        assert "_overpass_elements" in source, "it must still go through the one client"

    def test_the_rules_table_has_one_home(self):
        import subprocess

        root = Path(hazard_service.__file__).resolve().parent.parent
        hits = subprocess.run(
            [
                "grep",
                "-rl",
                "plant:source",
                "--include=*.py",
                str(root / "services"),
                str(root / "routes"),
                str(root / "utils"),
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
        modules = {Path(path).name for path in hits}
        assert modules <= {"hazard_rules.py"}, modules

    def test_the_coordinate_policy_is_imported_not_rewritten(self):
        source = Path(hazard_service.__file__).read_text(encoding="utf-8")
        assert "from services.coordinate_quality import" in source
        assert "5_000" not in source and "5000" not in source

    def test_the_free_pass_really_scans(self, app, real_fetch):
        """The hook, exercised rather than grepped.

        A green unit suite over a dead hook is the defect #309 names, and a
        substring assertion on the call site is what #309 was actually about
        -- so this runs the free pass and looks at the row afterwards. The
        other free steps are allowed to fail here; the point is that a failure
        in one of them does not take the scan with it.
        """
        from services.property_enrichment_service import PropertyEnrichmentService

        prop = _prop(title="FreePass")
        service = PropertyEnrichmentService(
            # The amenity and quality-of-life halves reach Overpass through
            # this instance; a stub that answers neither method leaves them
            # failing and logging, which is what the free pass is built to
            # survive -- and keeps `tests/network_guard.py` out of it.
            enrichment_service=_FakeEnrichment(failure="stubbed"),
            hazard_service=HazardService(
                enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
            ),
            sea_view_calculator=lambda prop, commit, use_ai: None,
        )
        service.enrich_free_sources(prop, commit=True, use_ai=False)
        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_OK
        assert prop.enrichment["hazards"]["items"]
