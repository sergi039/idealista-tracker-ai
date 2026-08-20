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

import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import coordinate_quality, hazard_rules, hazard_service
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

    def test_the_ambiguous_coal_name_is_gone(self):
        """`carbonera` matches *Carboneras*, an Almerían municipality whose
        name is on every industrial estate in it -- and it caught nothing the
        plural entry misses (found in review, 2026-08-20)."""
        assert (
            hazard_rules.classify(
                {"landuse": "industrial", "name": "Poligono Industrial de Carboneras"}
            )
            is None
        )
        coal = hazard_rules.classify(
            {"landuse": "quarry", "name": "Parque de carbones"}
        )
        assert coal is not None and coal.kind == "coal_yard"

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

    def test_a_product_that_names_a_process_is_read(self):
        """`product=*` is a direct claim, but only for the values that name a
        process nobody runs by accident.

        The issue named it first (*"claims about what the thing is --
        `plant:source=coal`, `product=cement`"*) and it was missing entirely.
        """
        verdict = hazard_rules.classify({"man_made": "works", "product": "cement"})
        assert verdict is not None
        assert verdict.kind == "cement_works"
        assert verdict.evidence == "product=cement"

    def test_a_bare_metal_product_is_not_a_smelter(self):
        """And this one costs a real facility, deliberately.

        `way/1068457365` is *Balumco*, `man_made=works` + `product=aluminum`,
        and the Catalan environmental register describes extrusion and
        anodising rather than primary smelting. Nothing structural separates
        it from `relation/11519713`, *Asturiana de Zinc*, which really is a
        smelter and carries the same shape. So OSM cannot answer "is this a
        smelter" from `product` alone and the table does not pretend to --
        which means AZSA classifies as nothing until somebody tags it with a
        process (codex review, 2026-08-20).
        """
        assert (
            hazard_rules.classify(
                {"man_made": "works", "name": "Balumco", "product": "aluminum"}
            )
            is None
        )
        assert (
            hazard_rules.classify(
                {
                    "man_made": "works",
                    "name": "Asturiana de Zinc",
                    "operator": "Glencore",
                    "product": "zinc",
                }
            )
            is None
        )
        # A process claim still qualifies, which is the way back for both.
        smelting = hazard_rules.classify(
            {"man_made": "works", "industrial": "smelting"}
        )
        assert smelting is not None and smelting.severity == hazard_rules.SEVERITY_HIGH

    def test_a_solar_thermal_plant_burns_nothing(self):
        """Spain's concentrated-solar plants are *centrales térmicas solares*.

        The name rule would have reported one as a coal station. A declared
        `plant:source` contradicts the name, and the name entry carries the
        words on its own for a plant with no power tag at all.
        """
        # The tag alone: a plant whose name says nothing about the sun.
        assert (
            hazard_rules.classify(
                {
                    "power": "plant",
                    "plant:source": "solar",
                    "name": "Central Termica Andasol",
                }
            )
            is None
        )
        # The name alone: no power tag to contradict it, so the words have to.
        assert (
            hazard_rules.classify(
                {"landuse": "industrial", "name": "Central termica solar de Sevilla"}
            )
            is None
        )
        coal = hazard_rules.classify(
            {"landuse": "industrial", "name": "Central Termica de Soto de Ribera"}
        )
        assert coal is not None and coal.kind == "power_plant"

    def test_a_nuclear_plant_is_not_walked_past_for_burning_nothing(self):
        """`plant:source=nuclear` is not combustion and is not harmless."""
        verdict = hazard_rules.classify(
            {"power": "plant", "plant:source": "nuclear", "name": "CN Trillo"}
        )
        assert verdict is not None and verdict.kind == "nuclear_plant"
        assert verdict.severity == hazard_rules.SEVERITY_HIGH
        # And the renewables still are.
        assert hazard_rules.classify({"power": "plant", "plant:source": "wind"}) is None

    def test_a_tag_a_real_plant_carries_is_accepted(self):
        """`industrial=concrete_plant` is the tag CEMEX's own objects carry
        (`way/1221635493`), and only the bare word was listed (codex review,
        2026-08-20)."""
        for value in ("concrete", "concrete_plant", "cement_plant"):
            verdict = hazard_rules.classify(
                {"landuse": "industrial", "industrial": value, "name": "CEMEX"}
            )
            assert verdict is not None, value

    def test_an_oil_works_may_be_pressing_olives(self):
        """`way/591673652` is *Almazara Molino de las Torres*.

        The same `industrial=oil` on El Musel is Exolum's petroleum terminals,
        which is why the entry stays and carries the words that say which
        sense is meant (codex review, 2026-08-20).
        """
        assert (
            hazard_rules.classify(
                {
                    "landuse": "industrial",
                    "industrial": "oil",
                    "name": "Almazara Molino de las Torres",
                }
            )
            is None
        )
        petroleum = hazard_rules.classify(
            {
                "landuse": "industrial",
                "industrial": "oil",
                "name": "Exolum - Musel II",
                "operator": "Exolum",
            }
        )
        assert petroleum is not None and petroleum.kind == "fuel_depot"

    def test_a_generic_product_is_not_a_hazardous_process(self):
        """`product=metal` is a parts maker; `product=oil` is often olive oil.

        `node/13016693457` is *Alcyon* (`man_made=works`, `product=metal`) on
        a Basque industrial estate, and `way/485376150` is *Molino aceitero*
        (`man_made=works`, `product=oil`) -- an oil mill. Both were read as
        high-severity plants (codex review, 2026-08-20).
        """
        assert (
            hazard_rules.classify(
                {"man_made": "works", "name": "Alcyon", "product": "metal"}
            )
            is None
        )
        assert (
            hazard_rules.classify(
                {"man_made": "works", "name": "Molino aceitero", "product": "oil"}
            )
            is None
        )
        # `steel` and `iron` went the same way: a fabricator that cuts and
        # welds sections tags `product=steel` exactly as a mill does.
        assert (
            hazard_rules.classify(
                {"man_made": "works", "name": "Talleres Metalicos", "product": "steel"}
            )
            is None
        )
        # What survives is a product that names a process, and a tag about
        # what the place *does*.
        assert hazard_rules.classify({"man_made": "works", "product": "cement"})
        assert hazard_rules.classify(
            {"landuse": "industrial", "industrial": "steelmaking"}
        )

    def test_a_retired_process_does_not_retire_the_rest_of_the_element(self):
        """A lifecycle prefix refuses the *name*, never a live bare tag.

        Two real shapes it got wrong (codex review): a chemical works with a
        dead power plant on site, and a smelter that changed what it makes.
        """
        chemical = hazard_rules.classify(
            {
                "industrial": "chemical",
                "name": "Quimica Activa",
                "disused:power": "plant",
            }
        )
        assert chemical is not None and chemical.kind == "chemical_works"

        renamed = hazard_rules.classify(
            {"man_made": "works", "product": "cement", "was:product": "lime"}
        )
        assert renamed is not None and renamed.kind == "cement_works"

        # A prefixed key whose bare form is still there says nothing at all:
        # the bare one is the current state. El Musel's coal yard is mapped
        # `landuse=quarry` and only its *name* says coal, so suppressing the
        # name over a `was:landuse` would quietly demote it to a quarry.
        coal = hazard_rules.classify(
            {
                "landuse": "quarry",
                "name": "Parque de carbones",
                "was:landuse": "industrial",
            }
        )
        assert coal is not None and coal.kind == "coal_yard"

        # A retired thing silences the name only when it is the kind of thing
        # the name is about. `disused:amenity=fuel` is a closed petrol
        # station, and on a refinery whose only evidence is its name it
        # dropped the refinery (found in review, 2026-08-20).
        refinery = hazard_rules.classify(
            {
                "landuse": "industrial",
                "name": "Refineria de Puertollano",
                "disused:amenity": "fuel",
            }
        )
        assert refinery is not None and refinery.kind == "refinery"

        # And the case the rule exists for still refuses: the name is all the
        # evidence there is, and the process behind it is under `disused:`.
        assert (
            hazard_rules.classify(
                {
                    "disused:power": "plant",
                    "disused:plant:source": "coal",
                    "landuse": "industrial",
                    "name": "Central termica del Narcea",
                }
            )
            is None
        )

    def test_a_lifecycle_range_and_the_other_prefixes(self):
        """OSM's date specification allows a range, and reading the whole
        string as one date failed to parse and therefore read as *active*.
        `removed:`, `destroyed:`, `proposed:` and `construction:` were missing
        from the prefix list altogether (codex review, 2026-08-20)."""
        named = {"landuse": "industrial", "name": "Central termica de Prueba"}
        for gone in (
            {"end_date": "2020..2022"},
            {"end_date": "2020-2022"},
            # The forms OSM's date specification documents and the parser did
            # not read: full dates on both sides, and an open start.
            {"end_date": "2020-01-01-2022-06-30"},
            {"end_date": "2020-01-01--2022-06-30"},
            {"end_date": "-1964"},
            {"removed:power": "plant"},
            {"destroyed:power": "plant"},
            {"proposed:power": "plant"},
            {"construction:power": "plant"},
            {"planned:power": "plant"},
            {"abandoned:power": "plant"},
            {"razed:power": "plant"},
            {"ruins:power": "plant"},
        ):
            assert hazard_rules.classify({**named, **gone}) is None, gone
        # A date still ahead of us is not a closure, and a month is not a
        # range: `2020-06` must not be read as "2020 to 06".
        for still_here in ("2099", "2099-01-01", "2099..2100", "junk", "2020-W53"):
            future = hazard_rules.classify({**named, "end_date": still_here})
            assert future is not None and future.kind == "power_plant", still_here
        assert hazard_rules.classify({**named, "end_date": "2020-06"}) is None
        # And the dates that describe a survey, not an ending, are ignored.
        for ignored in ({"check_date": "2020-01-01"}, {"start_date": "1990"}):
            alive = hazard_rules.classify({"landuse": "landfill", **ignored})
            assert alive is not None and alive.kind == "landfill"

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

    def test_a_stored_null_where_a_list_belongs_still_renders(self, app, client):
        """Jinja iterates an undefined and raises on a None.

        `routes/main_routes.py` turns that into a flash and an empty page, so
        it would be invisible to a test that only checked the status code
        (codex review, 2026-08-20).
        """
        prop = _prop(title="NullLists")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "searched_m": 6000,
                "item_count": 1,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
                "items": [
                    {
                        "name": "Algo",
                        "kind": "landfill",
                        "severity": "high",
                        "origin_distance_m": 1200,
                        "bearing_deg": 90.0,
                        "kinds": None,
                        "elements": None,
                        "evidence": None,
                        "names": None,
                    }
                ],
            }
        }
        db.session.commit()
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert response_is_the_card(body)
        assert "Algo" in body

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

    def test_an_incomplete_answer_is_not_cached(self, app, real_fetch, monkeypatch):
        """The row is in the backfill's scope *because* the scan came back
        short, and a cached partial entry would answer the retry for a month
        (codex review, 2026-08-20)."""
        written = []
        monkeypatch.setattr(
            hazard_service,
            "cache_enrichment_data",
            lambda lat, lon, key, payload, timeout: written.append(key),
        )
        monkeypatch.setattr(
            hazard_service, "get_cached_enrichment_data", lambda *a, **k: None
        )
        service = HazardService(enrichment_service=_FakeEnrichment(elements=[42]))
        service.measure(*XIVARES)
        assert written == [], "an unreadable answer must not be cached"

        capped = [
            {
                "type": "node",
                "id": 95_000 + index,
                "lat": XIVARES[0] + 0.001,
                "lon": XIVARES[1],
                "tags": {"landuse": "industrial"},
            }
            for index in range(hazard_rules.ELEMENT_LIMIT)
        ]
        HazardService(enrichment_service=_FakeEnrichment(elements=capped)).measure(
            *XIVARES
        )
        assert written == [], "a capped answer must not be cached either"

        # ...and a complete one still is.
        HazardService(enrichment_service=_FakeEnrichment(elements=[])).measure(*XIVARES)
        assert written, "a complete answer is what the cache is for"

    def test_an_element_nobody_can_read_makes_the_scan_incomplete(
        self, app, real_fetch
    ):
        """A 200 carrying `[42]`, or an element with `tags: null`, used to be
        dropped and the scan then reported a clean neighbourhood built out of
        a response nobody could read (codex review, 2026-08-20)."""
        for elements in (
            [42],
            [{"type": "way", "id": 1, "lat": XIVARES[0], "lon": XIVARES[1]}],
            [
                {
                    "type": "way",
                    "id": 2,
                    "lat": XIVARES[0],
                    "lon": XIVARES[1],
                    "tags": None,
                }
            ],
        ):
            measurement = HazardService(
                enrichment_service=_FakeEnrichment(elements=elements)
            ).measure(*XIVARES)
            assert measurement["unreadable"] == 1, elements
            assert measurement["truncated"] is True, elements

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

    def test_merge_keys_will_not_absorb_without_being_told_who_may(self):
        """The default used to be "anything may absorb" -- the defect itself,
        one forgotten keyword away from coming back."""
        with pytest.raises(TypeError):
            hazard_rules.merge_keys(["cantera", "cantera blokdegal s a"])

    def test_two_operators_are_two_facilities(self, app, real_fetch):
        """`operator=Norte Ambiental` and `operator=Servicios Norte Ambiental`
        are two companies, and folding one into the other produced a single
        item wearing the far facility's identity and the near one's distance
        (codex review, 2026-08-20)."""
        elements = [
            {
                "type": "way",
                "id": 70_001,
                "lat": XIVARES[0] + 0.002,
                "lon": XIVARES[1],
                "tags": {
                    "landuse": "quarry",
                    "operator": "Norte Ambiental",
                },
            },
            {
                "type": "way",
                "id": 70_002,
                "lat": XIVARES[0] + 0.010,
                "lon": XIVARES[1],
                "tags": {
                    "landuse": "landfill",
                    "operator": "Servicios Norte Ambiental",
                },
            },
        ]
        measurement = HazardService(
            enrichment_service=_FakeEnrichment(elements=elements)
        ).measure(*XIVARES)
        assert len(measurement["items"]) == 2
        assert {item["name"] for item in measurement["items"]} == {
            "Norte Ambiental",
            "Servicios Norte Ambiental",
        }

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

    def test_a_stale_measurement_does_not_overwrite_a_current_one(
        self, app, real_fetch
    ):
        """The row can move while the network call is in flight.

        Codex reproduced it: A measures from origin A, B moves the row and
        stores a B-origin measurement, and A then refreshes under the lock and
        writes its stale result over B's good one. Readers call the result
        `stale_origin`, so nothing wrong is shown -- but a measurement of
        where the listing actually is was lost, and only a re-scan brings it
        back. Simulated here by moving the row *during* the lookup.
        """
        prop = _prop(title="MovedMidFlight")
        moved_to = (XIVARES[0] + 0.02, XIVARES[1])

        class _MovesTheRowMidLookup:
            """Stands in for the other session, committing while A is out."""

            def _overpass_elements(self, query):
                prop.location_lat = moved_to[0]
                prop.enrichment = {
                    "hazards": {
                        "status": hazard_service.STATUS_NONE,
                        "searched_m": 5984.0,
                        "truncated": False,
                        "item_count": 0,
                        "items": [],
                        "origin": {"lat": moved_to[0], "lon": moved_to[1]},
                    }
                }
                db.session.commit()
                return FIXTURE["elements"], None

        payload = HazardService(enrichment_service=_MovesTheRowMidLookup()).enrich(
            prop, commit=True
        )
        # B's measurement survives; A's says only that it tried.
        assert payload["status"] == hazard_service.STATUS_NONE
        assert payload["items"] == []
        assert payload["origin"]["lat"] == pytest.approx(moved_to[0])
        assert payload["last_attempt_status"] == hazard_service.STATUS_STALE_ORIGIN

    def test_a_row_with_no_coordinate_says_so(self, app, client, real_fetch):
        prop = _prop(title="NoCoordinate", location_lat=None, location_lon=None)
        payload = HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        assert payload["status"] == hazard_service.STATUS_NO_COORDINATES

        # And it reads back as that. The writer's own block cannot carry an
        # origin, and asking for one first made this branch unreachable: the
        # card said the listing had been re-located, for a row that has never
        # had a coordinate (codex review, 2026-08-20).
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_NO_COORDINATES
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert response_is_the_card(body)
        assert "no coordinate" in body
        assert "re-located" not in body

    def test_losing_the_coordinate_does_not_delete_the_measurement(
        self, app, client, real_fetch
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
        # `no_coordinates`, and not `stale_origin`: a row that lost its
        # coordinate has not been *re-located*, and saying so put this card in
        # direct contradiction with the travel card beside it, which correctly
        # says the listing has no coordinate (found in review, 2026-08-20).
        assert verdict["status"] == hazard_service.STATUS_NO_COORDINATES
        assert verdict["measured"] is False
        assert verdict["nearest"] is None
        # It still *carries* a complete scan, which is all the coverage line
        # claims; that the scan is not about this listing any more is what the
        # card says, and no badge is drawn either way.
        assert verdict["complete"] is True

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert response_is_the_card(body)
        assert "no coordinate" in body
        assert "re-located" not in body

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

    def test_the_scope_is_everything_without_a_complete_current_answer(
        self, app, real_fetch
    ):
        """Read through the verdict, not off the raw status.

        Three rows fell out of scope for good by reading the column directly
        (codex review, 2026-08-20): a truncated scan a re-run may complete, a
        block nobody can read, and a row stored `no_coordinates` that has
        since gained one.
        """
        prop = _prop(title="ScopeRules")
        origin = {"lat": XIVARES[0], "lon": XIVARES[1]}
        assert hazard_service.needs_hazards(prop) is True

        for label, block in (
            ("a refusal", {"status": hazard_service.STATUS_UNAVAILABLE}),
            (
                "no coordinate then one",
                {"status": hazard_service.STATUS_NO_COORDINATES},
            ),
            (
                "a truncated scan",
                {
                    "status": hazard_service.STATUS_NONE,
                    "items": [],
                    "item_count": 0,
                    "truncated": True,
                    "searched_m": 5984.0,
                    "origin": origin,
                },
            ),
            (
                "a block nobody can read",
                {
                    "status": hazard_service.STATUS_OK,
                    "items": "not a list",
                    "item_count": 1,
                    "origin": origin,
                },
            ),
            (
                "a scan of somewhere else",
                {
                    "status": hazard_service.STATUS_NONE,
                    "items": [],
                    "item_count": 0,
                    "searched_m": 5984.0,
                    "origin": {"lat": XIVARES[0] + 0.02, "lon": XIVARES[1]},
                },
            ),
        ):
            prop.enrichment = {"hazards": block}
            db.session.commit()
            assert hazard_service.needs_hazards(prop) is True, label

        # Only a complete measurement of where this listing is leaves the
        # scope.
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "truncated": False,
                "searched_m": 5984.0,
                "origin": origin,
            }
        }
        db.session.commit()
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
        # The scan covered 6 km (less the cache cell) around a point the
        # parcel may be 5 km from.
        assert verdict["guaranteed_m"] == pytest.approx(
            hazard_rules.SEARCH_RADIUS_M - hazard_service._CACHE_CELL_SLACK_M - 5000.0
        )

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
            hazard_rules.SEARCH_RADIUS_M - hazard_service._CACHE_CELL_SLACK_M
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


_ITEM = {
    "name": "Algo",
    "kind": "landfill",
    "severity": "high",
    "origin_distance_m": 1200,
    "bearing_deg": 90.0,
}


class TestOneAnswerInTwoLanguages:
    """`read_verdict` and `measured_expression` must agree, row for row.

    A coverage line that disagrees with the badges under it is a third wrong
    number rather than a disclosure (`services/listing_verification.py` wrote
    that rule down). What the two share is **`complete`** -- "carries a
    complete scan" -- and deliberately not `measured`, which additionally asks
    whether the scan is about the row's *current* coordinate. Answering that
    second question in SQL means casting stored JSON to a number, and on
    PostgreSQL one hand-edited `"junk"` then raises and takes the whole
    coverage query down (codex review, 2026-08-20).
    """

    _HERE = {"lat": XIVARES[0], "lon": XIVARES[1]}
    _ELSEWHERE = {"lat": XIVARES[0] + 0.02, "lon": XIVARES[1]}

    @pytest.mark.parametrize(
        "stored,complete",
        [
            (None, False),
            ({"status": hazard_service.STATUS_OK, "items": []}, True),
            ({"status": hazard_service.STATUS_NONE, "items": []}, True),
            ({"status": hazard_service.STATUS_UNAVAILABLE}, False),
            ({"status": hazard_service.STATUS_NO_COORDINATES}, False),
            ({"nonsense": 1}, False),
            # A moved coordinate does not make the scan incomplete -- it makes
            # it a complete scan of somewhere else, which is what the card
            # says and what keeps this answerable without a cast.
            (
                {
                    "status": hazard_service.STATUS_OK,
                    "items": [],
                    "origin": _ELSEWHERE,
                },
                True,
            ),
        ],
    )
    def test_python_and_sql_read_the_same_matrix(self, app, stored, complete):
        prop = _prop(title=f"Matrix{complete}{stored}")
        if stored is not None:
            prop.enrichment = {"hazards": stored}
            db.session.commit()
        assert hazard_service.read_verdict(prop)["complete"] is complete

        counted = (
            Property.query.filter(Property.id == prop.id)
            .filter(hazard_service.measured_expression(Property))
            .count()
        )
        assert bool(counted) is complete

    @pytest.mark.parametrize(
        "truncated,complete",
        [
            ({}, True),
            ({"truncated": True}, False),
            ({"truncated": False}, True),
            # A JSON boolean does not render the same on both backends, and a
            # hand-edited row can hold anything at all. Everything that is not
            # plainly "not truncated" reads as truncated, in both languages.
            ({"truncated": "true"}, False),
            ({"truncated": "false"}, True),
            ({"truncated": 1}, False),
            ({"truncated": 0}, True),
            ({"truncated": None}, True),
            ({"truncated": {}}, False),
            ({"truncated": []}, False),
            ({"truncated": "junk"}, False),
            # Python strips, and SQL did not: `" false "` was complete in one
            # language and truncated in the other (codex review, 2026-08-20).
            ({"truncated": " false "}, True),
            ({"truncated": "\tfalse\n"}, False),
            ({"truncated": " true "}, False),
            ({"truncated": "   "}, True),
        ],
    )
    def test_the_truncation_flag_reads_the_same_in_both_languages(
        self, app, truncated, complete
    ):
        """And none of these may raise. On PostgreSQL a Boolean cast over
        `{}` fails the query outright, which would remove the coverage line --
        and with it the page -- for every row, over one bad row."""
        prop = _prop(title=f"Flag{truncated}")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "searched_m": 5984.0,
                "origin": self._HERE,
                **truncated,
            }
        }
        db.session.commit()
        assert hazard_service.read_verdict(prop)["complete"] is complete
        counted = (
            Property.query.filter(Property.id == prop.id)
            .filter(hazard_service.measured_expression(Property))
            .count()
        )
        assert bool(counted) is complete

    @pytest.mark.parametrize(
        "block",
        [
            # A count that disagrees with what is stored.
            {"status": "ok", "items": [], "item_count": 1},
            {"status": "none_within_radius", "items": [], "item_count": 3},
            {"status": "ok", "items": [{"kind": "landfill"}], "item_count": 0},
            # An undercount on its own: `ok`, a non-empty list, and a count
            # that is smaller than it. Fewer stored than counted is the
            # ordinary `MAX_ITEMS` cap; more stored than counted is not a
            # shape anything produces.
            {
                "status": "ok",
                "items": [
                    {"kind": "landfill", "origin_distance_m": 1200},
                    {"kind": "quarry", "origin_distance_m": 1300},
                ],
                "item_count": 1,
            },
            # Items that are not a list at all -- `value or []` turned these
            # into a clean neighbourhood, and `items: 1` raised outright.
            {"status": "ok", "items": {}, "item_count": 1},
            {"status": "ok", "items": 1, "item_count": 1},
            {"status": "ok", "items": "none", "item_count": 1},
            # A count nobody can read.
            {"status": "ok", "items": [], "item_count": "1"},
            {"status": "ok", "items": [], "item_count": -1},
        ],
    )
    def test_a_block_that_contradicts_itself_is_a_block_nobody_read(self, app, block):
        """Every one of these scored 100 or raised (codex review,
        2026-08-20). None of them may assert anything."""
        prop = _prop(title=f"Contradicts{block['items']}{block['item_count']}")
        prop.enrichment = {
            "hazards": {
                "searched_m": 5984.0,
                "truncated": False,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
                **block,
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False
        score, _ = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None

    @pytest.mark.parametrize(
        "block,why",
        [
            (
                {"item_count": 25, "items": [_ITEM]},
                "a count past the cap with one item",
            ),
            ({"item_count": True, "items": [_ITEM]}, "a boolean count"),
            ({"item_count": 1.0, "items": [_ITEM]}, "a floating-point count"),
            (
                {"item_count": 100_000, "items": [_ITEM]},
                "a count past what a scan can even return",
            ),
            (
                {"item_count": 1, "items": [{**_ITEM, "origin_distance_m": None}]},
                "an item with no distance",
            ),
            (
                {"item_count": 1, "items": [{**_ITEM, "origin_distance_m": -5}]},
                "a negative distance",
            ),
            (
                {"item_count": 1, "items": [{**_ITEM, "origin_distance_m": True}]},
                "a boolean distance",
            ),
            (
                {
                    "item_count": 1,
                    "items": [{**_ITEM, "origin_distance_m": "Infinity"}],
                },
                "an infinite distance",
            ),
            (
                {"item_count": 1, "items": [{**_ITEM, "severity": "HIGH"}]},
                "a severity nobody writes",
            ),
            (
                {"item_count": 1, "items": [{**_ITEM, "severity": None}]},
                "no severity at all",
            ),
        ],
    )
    def test_a_block_the_writer_cannot_produce_asserts_nothing(self, app, block, why):
        """Each of these was measured, flagged or scored (codex review,
        2026-08-20). An unknown severity was the sharpest: at `near_m` a
        `high` item scores 0 and an unrecognised one scored 50."""
        prop = _prop(title=f"Impossible{why}")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "searched_m": 5984.0,
                "truncated": False,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
                **block,
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["measured"] is False, why
        assert verdict["flagged"] is False, why
        assert hazard_service.needs_hazards(prop) is True, why
        score, _ = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None, why

    def test_a_radius_larger_than_the_writer_can_store_is_not_a_radius(self, app):
        """`1e300` cleared every horizon the scorer checks and turned an empty
        scan into a clean 100 (codex review, 2026-08-20)."""
        prop = _prop(title="HugeRadius")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "truncated": False,
                "searched_m": 1e300,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False
        score, _ = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None

    def test_a_radius_of_zero_is_not_a_radius(self, app):
        """Dropping the positivity check rendered "Scanned 0.0 km around the
        stored coordinate" from a block the writer never produces (found in
        review, 2026-08-20)."""
        prop = _prop(title="ZeroRadius")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "truncated": False,
                "searched_m": 0,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False

    def test_an_element_a_template_cannot_link_is_dropped(self, app, client):
        """`element.type[:1]` on an integer raised, and the route turned that
        into a redirect nobody sees (found in review, 2026-08-20)."""
        prop = _prop(title="UnlinkableElement")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "item_count": 1,
                "truncated": False,
                "searched_m": 5984.0,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
                "items": [
                    {
                        **_ITEM,
                        "elements": [
                            {"type": 42, "id": "abc"},
                            {"type": "way", "id": True},
                            {"type": "way", "id": 7},
                        ],
                    }
                ],
            }
        }
        db.session.commit()
        assert hazard_service.read_verdict(prop)["items"][0]["elements"] == [
            {"type": "way", "id": 7}
        ]
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert response_is_the_card(body)
        assert "openstreetmap.org/way/7" in body

    def test_a_number_too_wide_to_parse_is_not_a_measurement(self, app):
        """A JSON integer has no width limit and PostgreSQL stores it: a
        310-digit one raised out of `float()` and out of every caller (codex
        review, 2026-08-20)."""
        huge = int("9" * 310)
        prop = _prop(title="TooWide")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "truncated": False,
                "searched_m": huge,
                "origin": {"lat": huge, "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["measured"] is False
        assert verdict["status"] == hazard_service.STATUS_STALE_ORIGIN

    def test_an_unknown_status_is_normalised_rather_than_echoed(self, app):
        """The surfaces branch on a fixed set, and a sixth value nobody wrote
        a branch for renders as silence."""
        prop = _prop(title="UnknownStatus")
        prop.enrichment = {"hazards": {"status": "something_new", "items": []}}
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False

    def test_an_unreadable_measurement_grants_no_horizon(self, app):
        """`searched_m: "Infinity"` parses, and it cleared every horizon the
        scorer checks (codex review, 2026-08-20)."""
        prop = _prop(title="InfiniteHorizon")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": [],
                "item_count": 0,
                "truncated": False,
                "searched_m": "Infinity",
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        # Not "a measurement with no radius" -- a block claiming a radius it
        # cannot support is not a measurement at all, so it reads as one
        # nobody has taken and goes back into the backfill's scope.
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False
        assert verdict["searched_m"] is None
        assert verdict["guaranteed_m"] is None
        assert hazard_service.needs_hazards(prop) is True
        score, meta = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None
        assert meta["status"] == hazard_service.STATUS_MISSING

    def test_a_block_with_no_readable_origin_asserts_nothing(self, app):
        """It cannot be shown to be about this coordinate, and reading that as
        "cannot tell" let a moved precise row keep its old distances."""
        for origin in (None, {}, "somewhere", {"lat": None, "lon": None}):
            prop = _prop(title=f"NoOrigin{origin}")
            prop.enrichment = {
                "hazards": {
                    "status": hazard_service.STATUS_NONE,
                    "items": [],
                    "item_count": 0,
                    "truncated": False,
                    "searched_m": 5984.0,
                    "origin": origin,
                }
            }
            db.session.commit()
            verdict = hazard_service.read_verdict(prop)
            assert verdict["status"] == hazard_service.STATUS_STALE_ORIGIN, origin
            assert verdict["measured"] is False

    def test_an_item_nobody_can_read_costs_the_block_its_completeness(self, app):
        """Dropping it silently reported the rest as the whole list, with a
        clean badge and a 100 from the scorer (codex review, 2026-08-20)."""
        prop = _prop(title="MalformedItem")
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_OK,
                "item_count": 1,
                "items": ["not a dict"],
                "truncated": False,
                "searched_m": 5984.0,
                "origin": self._HERE,
            }
        }
        db.session.commit()
        verdict = hazard_service.read_verdict(prop)
        # Nobody has read this block, so nothing is asserted from it: no
        # badge, no score, and the card says so. `complete` stays as stored,
        # because that is what the coverage line counts and SQL cannot see an
        # item's shape -- the two readings must not diverge over something one
        # of them is blind to.
        assert verdict["status"] == hazard_service.STATUS_MISSING
        assert verdict["measured"] is False
        assert verdict["flagged"] is False
        assert verdict["items"] == []
        # And it is not a complete scan either: the card, the badge and the
        # CSV all read this verdict, and they have to agree with each other
        # before they agree with a count that cannot see an item's shape
        # (codex review, 2026-08-20).
        assert verdict["complete"] is False
        score, meta = HousingPropertyScorer()._hazard_score(
            prop, near_m=1000.0, far_m=5000.0, moderate_factor=0.5
        )
        assert score is None
        assert meta["status"] == hazard_service.STATUS_MISSING


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

    @pytest.mark.parametrize("branch", ["investment", "lifestyle"])
    def test_turning_it_on_is_previewed_even_beside_another_live_criterion(
        self, app, client, branch
    ):
        """The gate asked "is *any* weightless criterion on", so with the pool
        weight already positive the transition read `True -> True` and
        enabling hazards re-scored the subscription on an ordinary save, with
        no preview and no confirm (codex review, 2026-08-20)."""
        profile = SearchProfile(name=f"Both {branch}", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.commit()
        _prop(title="GateRow", search_profile_id=profile.id, price=100000, area=200)

        # Pool first, confirmed, so it is genuinely stored as positive.
        client.post(
            f"/profiles/{profile.id}/edit",
            data={
                "action": "save_scoring_weights",
                "scoring__land__%s__pool_score" % branch: "0.2",
            },
        )
        client.post(
            f"/profiles/{profile.id}/edit", data={"action": "confirm_pool_scoring"}
        )
        db.session.refresh(profile)
        stored = (profile.scoring_config or {})["categories"]["land"][branch]
        assert stored["pool_score"] == 0.2

        # Now hazards, beside it. This must not apply on the save.
        response = client.post(
            f"/profiles/{profile.id}/edit",
            data={
                "action": "save_scoring_weights",
                "scoring__land__%s__pool_score" % branch: "0.2",
                "scoring__land__%s__hazard_score" % branch: "0.3",
            },
            follow_redirects=True,
        )
        assert "preview" in response.get_data(as_text=True).lower()
        db.session.refresh(profile)
        still = (profile.scoring_config or {})["categories"]["land"][branch]
        assert still.get("hazard_score") in (None, 0, 0.0), (
            "the hazard weight must wait for the confirm, not ride the save"
        )

        client.post(
            f"/profiles/{profile.id}/edit", data={"action": "confirm_pool_scoring"}
        )
        db.session.refresh(profile)
        applied = (profile.scoring_config or {})["categories"]["land"][branch]
        assert applied["hazard_score"] == 0.3

    def test_the_editor_really_renders_the_weight_and_its_thresholds(self, app, client):
        """Asserting the constant passes when the form skips the criterion."""
        profile = SearchProfile(name="Editor", is_active=True)
        db.session.add(profile)
        db.session.commit()
        body = client.get(f"/profiles/{profile.id}/edit").get_data(as_text=True)
        # Both branches: removing only the investment field passed a test that
        # checked lifestyle (codex review, 2026-08-20).
        assert "scoring__land__lifestyle__hazard_score" in body
        assert "scoring__land__investment__hazard_score" in body
        assert "scoring__land__hazard__near_m" in body
        assert "scoring__land__hazard__far_m" in body
        assert "scoring__land__hazard__moderate_factor" in body

    def test_hazard_data_moves_no_score_at_the_shipped_weight(self, app, real_fetch):
        """Weightless, and *computed* -- deleting the component would pass a
        test that only compared the score columns (codex review)."""
        prop = _prop(title="Weightless", property_category="housing")
        scoring = PropertyScoringService()
        scoring.calculate_for_property(prop, commit=True)
        before = (prop.score_investment, prop.score_lifestyle, prop.score_total)
        coverage_before = prop.scoring["coverage"]["share"]

        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        scoring.calculate_for_property(prop, commit=True)

        assert (prop.score_investment, prop.score_lifestyle, prop.score_total) == before
        # ...and the criterion really ran: the component is in both branches
        # and its meta names the facility it measured.
        for branch in ("investment", "lifestyle"):
            components = prop.scoring["profiles"][branch]["components"]
            assert "hazard_score" in components
            assert components["hazard_score"] is not None
        assert prop.scoring["details"]["hazard"]["kind"] == "cement_works"
        # A weight of 0 must not reach the coverage share either.
        assert prop.scoring["coverage"]["share"] == coverage_before


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
        assert hazard_service.read_verdict(prop)["measured"] is False
        score, meta = self._score(prop)
        assert score is None
        assert meta["status"] == hazard_service.STATUS_MISSING

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
            # The only shape a cut list can have: a count past the cap, with
            # exactly the cap's worth stored.
            "item_count": 25,
            "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
        }

        def _items(kind, severity, nearest):
            return [
                {
                    "kind": kind,
                    "severity": severity,
                    "origin_distance_m": nearest + index,
                }
                for index in range(hazard_service.MAX_ITEMS)
            ]

        safe = _prop(title="ComponentCutSafe")
        safe.enrichment = {
            "hazards": {**block, "items": _items("landfill", "high", 500)}
        }
        db.session.commit()
        assert self._score(safe)[0] == 0.0

        unsafe = _prop(title="ComponentCutUnsafe")
        unsafe.enrichment = {
            "hazards": {**block, "items": _items("quarry", "moderate", 1000)}
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
        # The scan was long enough; the slack is what eats the difference, and
        # that is the reason the owner can act on.
        assert meta["status"] == "approximate_origin"

    def test_a_scan_shorter_than_far_m_cannot_answer_even_with_items(
        self, app, real_fetch
    ):
        """The horizon check used to cover only an empty scan.

        `far_m` is configurable per subscription, so a scan that covered less
        than it cannot answer for the ground past its edge: a moderate quarry
        at 5 km scored 72 while an unseen high-severity facility at 6 km would
        have scored 55 and decided the component (codex review, 2026-08-20).
        """
        prop = _prop(title="ShortHorizonWithItems")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        assert hazard_service.read_verdict(prop)["items"]

        # Inside the scan's own horizon it answers...
        assert self._score(prop, far_m=5000.0)[0] is not None
        # ...and past it, it does not.
        score, meta = self._score(prop, far_m=10000.0)
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

    def test_the_weight_really_reaches_the_average_and_the_page(
        self, app, client, real_fetch, profile
    ):
        """Dropping `hazard_score` from the weighted average left every test
        green, and the score breakdown on the card never listed it at all
        (codex review, 2026-08-20)."""
        profile.scoring_config = {
            "categories": {"land": {"lifestyle": {"hazard_score": 1.0}}}
        }
        db.session.commit()
        prop = _prop(
            title="WeightMoves", search_profile_id=profile.id, price=100000, area=1000
        )
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        PropertyScoringService().calculate_for_property(prop, commit=True)

        lifestyle = prop.scoring["profiles"]["lifestyle"]
        component = lifestyle["components"]["hazard_score"]
        assert component is not None
        # The whole lifestyle branch is this one criterion, so the branch score
        # *is* the component -- which is what fails if it never reaches
        # `_weighted_average`.
        assert lifestyle["score"] == pytest.approx(component)

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        # Twice: the card's own header, and the line in the score breakdown
        # that explains where the number came from. The breakdown never listed
        # the criterion at all (codex review, 2026-08-20), and asserting the
        # phrase once cannot tell the two apart.
        assert body.count("Industrial neighbours") >= 2
        assert "component_hazard" not in body, "the label must be translated"

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

    def test_an_approximate_row_does_not_get_a_parcel_level_badge(
        self, app, client, real_fetch, profile
    ):
        """532 of 725 rows sit on a locality centroid, so "Industry nearby"
        from one scan would make that claim for every listing in the village
        (codex review, 2026-08-20)."""
        prop = _prop(
            title="CentroidBadge",
            location_accuracy="approximate",
            search_profile_id=profile.id,
        )
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        body = client.get("/properties").get_data(as_text=True)
        assert "Industry near the locality" in body
        assert "Industry nearby" not in body

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
        assert "1 of 2 listings hold a complete scan" in body

    def test_an_incomplete_scan_is_not_a_clean_row_on_the_list(
        self, app, client, real_fetch, profile
    ):
        """A short scan that found nothing showed no badge at all, which made
        it indistinguishable from a clean neighbourhood, and the coverage line
        counted it as scanned (codex review, 2026-08-20)."""
        filler = [
            {
                "type": "node",
                "id": 60_000 + index,
                "lat": XIVARES[0] + 0.001,
                "lon": XIVARES[1] + 0.001,
                "tags": {"landuse": "industrial"},
            }
            for index in range(hazard_rules.ELEMENT_LIMIT)
        ]
        prop = _prop(title="ListTruncated", search_profile_id=profile.id)
        HazardService(enrichment_service=_FakeEnrichment(elements=filler)).enrich(
            prop, commit=True
        )
        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_NONE

        body = client.get("/properties").get_data(as_text=True)
        assert "Scan incomplete" in body
        assert "0 of 1 listings hold a complete scan" in body

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
        # `csv.reader`, and an explicit length check: splitting on commas
        # breaks on a title that contains one, and `zip` silently tolerates a
        # row that is a cell short (codex review, 2026-08-20).
        header, *rows = list(csv.reader(io.StringIO(body)))
        assert "Nearest Hazard Distance Min (m)" in header
        assert len(rows[0]) == len(header)
        by_name = dict(zip(header, rows[0]))
        assert by_name["Hazards"] == "ok"
        assert by_name["Hazard Scan Complete"] == "True"
        # Blank on an approximate row, exactly as the page refuses to print it.
        assert by_name["Nearest Hazard Distance (m)"] == ""
        assert by_name["Nearest Hazard Distance Max (m)"]

    def test_the_csv_says_when_a_scan_came_back_short(
        self, app, client, real_fetch, profile
    ):
        """Exporting `True` for every measured row passed the test above
        (codex review, 2026-08-20)."""
        filler = [
            {
                "type": "node",
                "id": 80_000 + index,
                "lat": XIVARES[0] + 0.001,
                "lon": XIVARES[1] + 0.001,
                "tags": {"landuse": "industrial"},
            }
            for index in range(hazard_rules.ELEMENT_LIMIT)
        ]
        prop = _prop(title="CsvTruncated", search_profile_id=profile.id)
        HazardService(enrichment_service=_FakeEnrichment(elements=filler)).enrich(
            prop, commit=True
        )
        body = client.get("/properties/export.csv").get_data(as_text=True)
        header, *rows = list(csv.reader(io.StringIO(body)))
        by_name = dict(zip(header, rows[0]))
        assert by_name["Hazards"] == "none_within_radius"
        assert by_name["Hazard Scan Complete"] == "False"

    def test_the_csv_never_turns_an_unreadable_block_into_a_measurement(
        self, app, client, real_fetch, profile
    ):
        """Exporting the stored status rather than the verdict's turned a
        block nobody can read into a measured `none_within_radius` (codex
        review, 2026-08-20)."""
        prop = _prop(title="CsvUnreadable", search_profile_id=profile.id)
        prop.enrichment = {
            "hazards": {
                "status": hazard_service.STATUS_NONE,
                "items": "not a list",
                "item_count": 0,
                "truncated": False,
                "searched_m": 5984.0,
                "origin": {"lat": XIVARES[0], "lon": XIVARES[1]},
            }
        }
        db.session.commit()
        body = client.get("/properties/export.csv").get_data(as_text=True)
        header, *rows = list(csv.reader(io.StringIO(body)))
        by_name = dict(zip(header, rows[0]))
        assert by_name["Hazards"] == hazard_service.STATUS_MISSING
        assert by_name["Hazard Scan Complete"] == "False"
        assert by_name["Hazard Facilities"] == ""

    def test_the_csv_says_a_moved_row_still_holds_a_complete_scan(
        self, app, client, real_fetch, profile
    ):
        """The column is `complete`, the same fact the coverage line counts.

        Gating it on `measured` blanked the cell for a row measured before it
        moved, so the export disagreed with the number above it (codex review,
        2026-08-20).
        """
        prop = _prop(title="CsvMoved", search_profile_id=profile.id)
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        prop.location_lat = XIVARES[0] + 0.02
        db.session.commit()

        assert hazard_service.read_verdict(prop)["measured"] is False
        body = client.get("/properties/export.csv").get_data(as_text=True)
        header, *rows = list(csv.reader(io.StringIO(body)))
        by_name = dict(zip(header, rows[0]))
        assert by_name["Hazards"] == "stale_origin"
        assert by_name["Hazard Scan Complete"] == "True"


class TestOneHomePerRule:
    """No second Overpass client, no second rules table, no second policy.

    Pinned the way `tests/test_deploy_page_check_shared.py` pins its own shared
    contract: a rule in two places is one that eventually ships half-changed.
    The textual half of these checks is deliberately kept **and** paired with a
    behavioural one, because a grep is bypassed by a direct `urllib`, a
    string-concatenated duplicate rule or an inline `5e3` (codex review,
    2026-08-20).
    """

    def test_the_scan_goes_through_the_one_overpass_client(self, app, real_fetch):
        """Behavioural: the transport it uses is the shared method, and the
        query it hands over is the one the rules table wrote."""
        seen = {}

        class _Spy:
            def _overpass_elements(self, query):
                seen["query"] = query
                return [], None

        HazardService(enrichment_service=_Spy()).measure(*XIVARES)
        assert seen["query"] == hazard_rules.overpass_query(*XIVARES)

        source = Path(hazard_service.__file__).read_text(encoding="utf-8")
        assert "requests" not in source
        assert "OVERPASS_GATE" not in source

    def test_every_element_is_judged_by_the_one_table(
        self, app, real_fetch, monkeypatch
    ):
        """Behavioural: a duplicate rule elsewhere is invisible to a grep, so
        what is pinned is that the scan asks *this* function about every
        element it saw (codex review, 2026-08-20)."""
        asked = []
        real_classify = hazard_rules.classify
        monkeypatch.setattr(
            hazard_service.hazard_rules,
            "classify",
            lambda tags: (asked.append(tags), real_classify(tags))[1],
        )
        service = HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        )
        measurement = service.measure(*XIVARES)
        assert len(asked) == measurement["candidates_seen"] == 144

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

    def test_the_coordinate_policy_is_the_shared_one(
        self, app, real_fetch, monkeypatch
    ):
        """Behavioural: change the shared slack and the verdict follows it.

        A grep for `5000` is bypassed by an inline `5e3`; this cannot be.
        """
        prop = _prop(title="SharedSlack", location_accuracy="approximate")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(prop, commit=True)
        assert hazard_service.read_verdict(prop)["slack_m"] == 5000.0

        monkeypatch.setattr(coordinate_quality, "APPROXIMATE_COORD_SLACK_M", 250)
        restated = hazard_service.read_verdict(prop)
        assert restated["slack_m"] == 250.0
        assert restated["nearest"]["max_distance_m"] == pytest.approx(
            restated["nearest"]["origin_distance_m"] + 250.0
        )

    def test_the_write_is_locked_and_validated_before_the_lookup(
        self, app, real_fetch, monkeypatch
    ):
        """The #339 order: caller validated first, row locked after.

        A sequential test cannot observe a lock, so this observes the calls --
        `check_writable` before anything reaches Overpass, and the refresh
        taken `with_for_update` (codex review, 2026-08-20).
        """
        order = []
        real_check = hazard_service.check_writable

        def _check(prop, commit):
            order.append("check_writable")
            return real_check(prop, commit)

        def _fetch(service, lat, lon):
            order.append("fetch")
            return {"elements": [], "returned": 0}, None

        refreshes = []
        real_refresh = db.session.refresh
        monkeypatch.setattr(hazard_service, "check_writable", _check)
        monkeypatch.setattr(hazard_service, "fetch_elements", _fetch)
        monkeypatch.setattr(
            db.session,
            "refresh",
            lambda obj, **kwargs: (
                refreshes.append(kwargs.get("with_for_update")),
                order.append("refresh"),
                real_refresh(obj, **kwargs),
            )[2],
        )

        prop = _prop(title="LockOrder")
        HazardService(enrichment_service=_FakeEnrichment(elements=[])).enrich(
            prop, commit=True
        )
        # The lock comes *after* the measurement, which is the half of #339
        # that a list of two names could not see: moving the refresh in front
        # of the network call left this green (codex review, 2026-08-20).
        assert order == ["check_writable", "fetch", "refresh"]
        assert refreshes == [True]

        # ...and on the path that never reaches the network at all.
        order.clear()
        refreshes.clear()
        nowhere = _prop(title="LockOrderNoCoords", location_lat=None, location_lon=None)
        HazardService(enrichment_service=_FakeEnrichment(elements=[])).enrich(
            nowhere, commit=True
        )
        assert order == ["check_writable", "refresh"]
        assert refreshes == [True]

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

    def test_the_scan_never_rides_a_transaction_it_cannot_lock(self, app, real_fetch):
        """`commit=False` takes no lock, so the scan does not run there.

        Reproduced on the ordinary Enrich flow: session B committed a
        measurement while A was out on the network, and A's refusal -- read
        from its own unlocked copy -- was committed over it (codex review,
        2026-08-20). The Enrich path runs the scan on its own beforehand,
        under its own lock; the free pass skips it rather than doing it twice.
        """
        from services.property_enrichment_service import PropertyEnrichmentService

        prop = _prop(title="NoUnlockedScan")
        service = PropertyEnrichmentService(
            enrichment_service=_FakeEnrichment(failure="stubbed"),
            hazard_service=HazardService(
                enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
            ),
            sea_view_calculator=lambda prop, commit, use_ai: None,
        )
        service.enrich_free_sources(prop, commit=False, use_ai=False)
        assert "hazards" not in (prop.enrichment or {})

        # ...and the path that owns its commits still scans.
        service.enrich_free_sources(prop, commit=True, use_ai=False)
        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_OK

    def test_the_enrich_button_scans_after_its_shared_transaction(
        self, app, real_fetch, monkeypatch
    ):
        """And the scan the Enrich flow does run is the locked one."""
        from services import property_enrichment_service as pes

        locked = []
        real_refresh = db.session.refresh
        monkeypatch.setattr(
            db.session,
            "refresh",
            lambda obj, **kwargs: (
                locked.append(kwargs.get("with_for_update")),
                real_refresh(obj, **kwargs),
            )[1],
        )

        prop = _prop(title="EnrichLocked")
        scans = []
        order = []

        class _CountingHazards(HazardService):
            def enrich(self, prop, commit=False):
                scans.append(commit)
                order.append("hazards")
                return super().enrich(prop, commit=commit)

        service = pes.PropertyEnrichmentService(
            enrichment_service=_FakeEnrichment(failure="stubbed"),
            hazard_service=_CountingHazards(
                enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
            ),
            sea_view_calculator=lambda prop, commit, use_ai: None,
            location_service=SimpleNamespace(
                ensure_coordinates=lambda prop, refresh, commit: order.append(
                    "coordinates"
                )
            ),
            travel_service=SimpleNamespace(
                calculate_for_property=lambda prop, commit: True
            ),
            scoring_service=SimpleNamespace(
                calculate_for_property=lambda prop, commit: (
                    order.append("scoring") or True
                )
            ),
            sea_distance_service=SimpleNamespace(
                update_property=lambda prop, commit: None
            ),
            pool_service=SimpleNamespace(enrich=lambda prop, commit: None),
        )
        monkeypatch.setattr(pes, "travel_api_state", lambda prop: "ok")
        monkeypatch.setattr(
            pes.advertiser,
            "enrich",
            lambda prop, commit=False: order.append("advertiser"),
        )
        real_commit = db.session.commit
        monkeypatch.setattr(
            db.session,
            "commit",
            lambda: (
                order.append("shared commit")
                if order and order[-1] == "advertiser"
                else None,
                real_commit(),
            )[1],
        )
        service.enrich_property(prop)

        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_OK
        assert True in locked, "the scan must take the row under FOR UPDATE"
        # Exactly once, with its own commit, and **after** the shared
        # transaction has been committed -- everything in that phase assigns
        # the whole `enrichment` column from a copy loaded before its network
        # calls, so a locked write placed ahead of it is restored to that
        # older value by its commit (codex review, 2026-08-20). Scoring runs
        # again afterwards, because the first pass could not see this block.
        assert scans == [True]
        # Measure, then score, with the scan behind the shared commit and
        # scoring behind the scan -- one scoring pass that has seen every
        # measurement it reads.
        assert order == [
            "coordinates",
            "advertiser",
            "shared commit",
            "hazards",
            "scoring",
        ]

    def test_a_row_with_no_coordinate_still_gets_a_block(self, app, real_fetch):
        """`enrich_property` returns early for a coordinate-less row, before
        the scan at the end of the method -- so the free pass is the only
        thing that can record its `no_coordinates` block, and it stopped doing
        so when the scan moved (found in review, 2026-08-20)."""
        from services.property_enrichment_service import PropertyEnrichmentService

        prop = _prop(title="EnrichNoCoords", location_lat=None, location_lon=None)
        scans = []

        class _CountingHazards(HazardService):
            def enrich(self, prop, commit=False):
                scans.append(commit)
                return super().enrich(prop, commit=commit)

        service = PropertyEnrichmentService(
            enrichment_service=_FakeEnrichment(failure="stubbed"),
            hazard_service=_CountingHazards(
                enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
            ),
            sea_view_calculator=lambda prop, commit, use_ai: None,
            location_service=SimpleNamespace(
                ensure_coordinates=lambda prop, refresh, commit: None
            ),
        )
        assert service.enrich_property(prop) is False
        assert scans == [True], "once, and under its own lock"
        assert (
            prop.enrichment["hazards"]["status"] == hazard_service.STATUS_NO_COORDINATES
        )

    def test_the_default_service_scans_without_being_handed_one(self, app, monkeypatch):
        """A test that injects its own hazard service passes when the default
        construction is broken (codex review, 2026-08-20). This one builds the
        service the way production does and only replaces the network seam.
        """
        from services.property_enrichment_service import PropertyEnrichmentService

        monkeypatch.setattr(
            hazard_service,
            "fetch_elements",
            lambda service, lat, lon: (
                {
                    "elements": [
                        {
                            "type": "way",
                            "id": 91_001,
                            "lat": XIVARES[0] + 0.001,
                            "lon": XIVARES[1],
                            "tags": {"landuse": "landfill", "name": "Vertedero"},
                        }
                    ],
                    "returned": 1,
                    "unreadable": 0,
                },
                None,
            ),
        )
        prop = _prop(title="DefaultScans")
        service = PropertyEnrichmentService()
        # The other two free steps reach Overpass and OpenTopoData through
        # their own transports, which this test is not about; stubbing them
        # keeps `tests/network_guard.py` out of it without touching the wiring
        # under test.
        monkeypatch.setattr(
            service.enrichment_service,
            "_overpass_elements",
            lambda query: (None, SimpleNamespace(reason="stubbed")),
        )
        monkeypatch.setattr(service, "sea_view_calculator", lambda **kwargs: None)
        service.enrich_free_sources(prop, commit=True, use_ai=False)
        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_OK
        assert prop.enrichment["hazards"]["items"][0]["name"] == "Vertedero"

    def test_the_default_construction_wires_the_shared_client(self, app):
        """The test above injects its own service, so a broken default would
        pass it (codex review, 2026-08-20)."""
        from services.property_enrichment_service import PropertyEnrichmentService

        service = PropertyEnrichmentService()
        assert isinstance(service.hazard_service, HazardService)
        # One EnrichmentService means one Overpass client and one 5 s gate
        # across the amenity, quality-of-life, pool and hazard lookups.
        assert service.hazard_service.enrichment_service is service.enrichment_service


class TestTheBackfill:
    """`utils/backfill_hazards.py` had no test at all, so both of its rules
    could be deleted and the whole suite stayed green (found in review,
    2026-08-20). `utils/backfill_pool.py`'s CLI is exercised the same way in
    `tests/test_surfaces_name_their_population.py`."""

    def _run(self, app, monkeypatch, argv):
        import logging
        from contextlib import nullcontext

        from utils import backfill_hazards as tool

        class _CurrentApp:
            def app_context(self):
                return nullcontext()

        monkeypatch.setattr(tool, "create_app", lambda: _CurrentApp())
        monkeypatch.setattr("sys.argv", argv)
        return tool, logging

    def test_a_dry_run_names_the_scope_and_writes_nothing(
        self, app, real_fetch, monkeypatch, caplog
    ):
        scanned = _prop(title="BackfillScanned")
        HazardService(
            enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
        ).enrich(scanned, commit=True)
        _prop(title="BackfillPending")

        tool, logging = self._run(app, monkeypatch, ["backfill", "--all", "--dry-run"])
        with caplog.at_level(logging.INFO):
            tool.main()

        # The scope is the rows without a complete current answer, and the
        # disclosure says which window it used.
        assert "hazard_backfill_queue" in caplog.text
        assert "--all" in caplog.text
        assert "last 30 days" not in caplog.text
        assert "BackfillPending" not in scanned.enrichment["hazards"]

    def test_it_writes_under_a_lock_and_scores_what_it_wrote(
        self, app, real_fetch, monkeypatch
    ):
        prop = _prop(title="BackfillRow", property_category="housing")
        scored = []
        locked = []
        real_refresh = db.session.refresh
        monkeypatch.setattr(
            db.session,
            "refresh",
            lambda obj, **kwargs: (
                locked.append(kwargs.get("with_for_update")),
                real_refresh(obj, **kwargs),
            )[1],
        )

        tool, logging = self._run(app, monkeypatch, ["backfill", "--all"])
        monkeypatch.setattr(
            tool,
            "HazardService",
            lambda: HazardService(
                enrichment_service=_FakeEnrichment(elements=FIXTURE["elements"])
            ),
        )
        monkeypatch.setattr(
            tool,
            "PropertyScoringService",
            lambda: SimpleNamespace(
                calculate_for_property=lambda prop, commit: scored.append(prop.id)
            ),
        )
        tool.main()

        assert prop.enrichment["hazards"]["status"] == hazard_service.STATUS_OK
        assert True in locked, "the write must take the row under FOR UPDATE"
        # A no-op at the shipped weight of 0, and the difference between a
        # fresh measurement and a stale score the day it is not.
        assert scored == [prop.id]
