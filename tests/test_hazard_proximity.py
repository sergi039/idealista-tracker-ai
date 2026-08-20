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

    def test_a_facility_beyond_the_radius_is_not_reported(self, app, real_fetch):
        """The block claims to have searched `SEARCH_RADIUS_M` and no further."""
        measurement = _measure(app, real_fetch)
        for item in measurement["items"]:
            assert item["origin_distance_m"] <= hazard_rules.SEARCH_RADIUS_M


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

    def test_a_regeocode_restates_the_stored_block_without_a_rescan(
        self, app, real_fetch
    ):
        """The row's *current* accuracy decides, not the one it was measured at."""
        prop = _prop(title="Regeocoded", location_accuracy="approximate")
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


class TestOneAnswerInTwoLanguages:
    """`read_verdict` and `measured_expression` must agree, row for row."""

    @pytest.mark.parametrize(
        "stored,measured",
        [
            (None, False),
            ({"status": hazard_service.STATUS_OK, "items": []}, True),
            ({"status": hazard_service.STATUS_NONE, "items": []}, True),
            ({"status": hazard_service.STATUS_UNAVAILABLE}, False),
            ({"status": hazard_service.STATUS_NO_COORDINATES}, False),
            ({"nonsense": 1}, False),
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
