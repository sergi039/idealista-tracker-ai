"""The quality-of-life block (Phase 2 slice, agreed proposal D15-D21).

Contracts pinned here, all #98-shaped:
* an Overpass refusal is `unavailable` and never renders or caches as "no
  shops"; an empty measured answer is `osm_empty` and says coverage may lag;
* a municipality the join cannot match is `not_matched`, never guessed;
* missing reference files read `no_reference_data`, not an empty landscape;
* one part failing never takes the others down, and no score reads any of it;
* every distance is straight-line and labeled so.
"""

import json

import pytest

from app import create_app, db
from models import Property
from services.enrichment_service import (
    EnrichmentService,
    GoogleApiFailure,
    OsmSupermarketReading,
)
from services import quality_of_life_service as qol_module
from services.quality_of_life_service import QualityOfLifeService
from tests import setup_test_environment

INE_FIXTURE = {
    "generated_at": "2026-08-14T00:00:00+00:00",
    "source": {"renta": "INE ADRH (fixture)", "codes": "diccionario26.xlsx"},
    "municipalities": {
        "33023": {
            "name": "Franco, El",
            "province": "33",
            "renta_media_persona": 13100,
            "renta_year": 2023,
            "population": 3800,
            "population_5y_change_pct": -2.4,
            "population_year": 2026,
        },
        "33041": {
            "name": "Navia",
            "province": "33",
            "renta_media_persona": 14200,
            "renta_year": 2023,
            "population": 8400,
            "population_5y_change_pct": -1.1,
            "population_year": 2026,
        },
    },
    "province_medians": {"33": {"renta_media_persona": 12800}},
}

CNH_FIXTURE = {
    "generated_at": "2026-08-14T00:00:00+00:00",
    "source": "CNH 2025 (fixture)",
    "hospitals": [
        {
            "name": "Hospital de Jarrio",
            "municipality": "Coaña",
            "province": "Asturias",
            "beds": 110,
            "teaching": False,
            "high_tech_count": 1,
            "grouping": "general_acute",
            "lat": 43.53,
            "lon": -6.73,
        },
        {
            "name": "HUCA",
            "municipality": "Oviedo",
            "province": "Asturias",
            "beds": 1039,
            "teaching": True,
            "high_tech_count": 8,
            "grouping": "teaching_high_tech",
            "lat": 43.36,
            "lon": -5.85,
        },
        {
            "name": "Farther General",
            "municipality": "Lugo",
            "province": "Lugo",
            "beds": 120,
            "teaching": False,
            "high_tech_count": 0,
            "grouping": "general_acute",
            "lat": 43.0,
            "lon": -7.55,
        },
    ],
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


@pytest.fixture
def reference_files(tmp_path, monkeypatch):
    ine = tmp_path / "ine_municipal.json"
    cnh = tmp_path / "hospitals_cnh.json"
    ine.write_text(json.dumps(INE_FIXTURE), encoding="utf-8")
    cnh.write_text(json.dumps(CNH_FIXTURE), encoding="utf-8")
    monkeypatch.setattr(qol_module, "INE_DATA_PATH", str(ine))
    monkeypatch.setattr(qol_module, "CNH_DATA_PATH", str(cnh))
    return tmp_path


class _FakeEnrichment:
    def __init__(self, reading):
        self.reading = reading
        self.calls = []

    def fetch_osm_supermarket_reach(self, lat, lon):
        self.calls.append((lat, lon))
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


def _service(reading=None):
    reading = reading if reading is not None else OsmSupermarketReading(items=[])
    return QualityOfLifeService(enrichment_service=_FakeEnrichment(reading))


class TestMunicipalityContext:
    def test_matches_through_normalization_and_articles(self, reference_files):
        # Idealista says "El Franco"; INE stores "Franco, El".
        context = _service().municipality_context("El Franco")
        assert context["status"] == "ok"
        assert context["ine_code"] == "33023"
        assert context["renta_media_persona"] == 13100
        assert context["renta_province_median"] == 12800

    def test_unmatched_is_honest_not_guessed(self, reference_files):
        context = _service().municipality_context("Corredoria-La Carisa-Prado de L...")
        assert context["status"] == "not_matched"
        assert "renta_media_persona" not in context

    def test_missing_reference_file_is_no_reference_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qol_module, "INE_DATA_PATH", str(tmp_path / "absent.json"))
        assert _service().municipality_context("Navia")["status"] == "no_reference_data"


class TestSupermarketReach:
    def test_refusal_is_unavailable_never_empty(self, reference_files):
        reading = OsmSupermarketReading(
            failure=GoogleApiFailure(reason="overpass_query_error")
        )
        part = _service(reading).supermarket_reach(43.55, -6.83)
        assert part["status"] == "unavailable"
        assert "items" not in part

    def test_measured_empty_is_osm_empty(self, reference_files):
        part = _service(OsmSupermarketReading(items=[])).supermarket_reach(43.55, -6.83)
        assert part["status"] == "osm_empty"
        assert part["items"] == []

    def test_measured_items_are_ok_and_labeled_straight_line(self, reference_files):
        items = [
            {
                "name": "Alimerka",
                "shop": "supermarket",
                "lat": 1,
                "lon": 1,
                "distance_km": 2.5,
            }
        ]
        part = _service(OsmSupermarketReading(items=items)).supermarket_reach(
            43.5, -6.8
        )
        assert part["status"] == "ok"
        assert part["distance_basis"] == "straight_line"


class TestHospitals:
    def test_nearest_per_grouping_by_straight_line(self, reference_files):
        # El Franco: Jarrio is ~9 km away, the Lugo general ~60 km.
        part = _service().hospitals(43.55, -6.83)
        assert part["status"] == "ok"
        assert part["nearest"]["general_acute"]["name"] == "Hospital de Jarrio"
        assert part["nearest"]["teaching_high_tech"]["name"] == "HUCA"
        assert part["distance_basis"] == "straight_line"

    def test_missing_file_reads_no_reference_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qol_module, "CNH_DATA_PATH", str(tmp_path / "absent.json"))
        assert _service().hospitals(43.55, -6.83)["status"] == "no_reference_data"


class TestEnrichOrchestration:
    def _property(self):
        prop = Property(
            source_email_id="qol-fixture",
            title="QolFixture",
            municipality="Navia",
            location_lat=43.54,
            location_lon=-6.72,
        )
        db.session.add(prop)
        db.session.commit()
        return prop

    def test_one_part_failing_never_takes_the_others_down(self, app, reference_files):
        service = QualityOfLifeService(
            enrichment_service=_FakeEnrichment(RuntimeError("boom"))
        )
        prop = self._property()
        payload = service.enrich(prop, commit=False)
        assert payload["supermarkets"]["status"] == "unavailable"
        assert payload["municipality"]["status"] == "ok"
        assert payload["hospitals"]["status"] == "ok"

    def test_a_refusal_never_overwrites_a_measured_part(self, app, reference_files):
        """Sea-distance precedent, applied per part (diff review 2026-08-14):
        the measured supermarkets stay through a later Overpass refusal, with
        the failed attempt stamped beside them."""
        prop = self._property()
        items = [
            {
                "name": "Alimerka",
                "shop": "supermarket",
                "lat": 43.5,
                "lon": -6.7,
                "distance_km": 1.0,
            }
        ]
        _service(OsmSupermarketReading(items=items)).enrich(prop, commit=False)
        refusing = _service(
            OsmSupermarketReading(
                failure=GoogleApiFailure(reason="overpass_query_error")
            )
        )
        payload = refusing.enrich(prop, commit=False)

        shops = payload["supermarkets"]
        assert shops["status"] == "ok", "the measured answer must survive"
        assert shops["items"] == items
        assert shops["last_attempt_status"] == "unavailable"
        assert "last_attempt_at" in shops

    def test_a_reference_collision_reads_unavailable_not_a_coin_flip(
        self, app, tmp_path, monkeypatch
    ):
        """Two in-scope names normalizing to one key must raise through
        build_index and surface as `unavailable`, never a silent wrong join."""
        colliding = json.loads(json.dumps(INE_FIXTURE))
        colliding["municipalities"]["27901"] = dict(
            colliding["municipalities"]["33041"], province="27"
        )
        path = tmp_path / "ine_colliding.json"
        path.write_text(json.dumps(colliding), encoding="utf-8")
        monkeypatch.setattr(qol_module, "INE_DATA_PATH", str(path))
        monkeypatch.setattr(qol_module, "CNH_DATA_PATH", str(tmp_path / "absent"))

        prop = self._property()
        payload = _service().enrich(prop, commit=False)
        assert payload["municipality"]["status"] == "unavailable"

    def test_coordinate_less_property_still_gets_the_ine_context(
        self, app, reference_files
    ):
        """The INE part needs no coordinates; the coordinate parts say
        `no_coordinates` instead of silently never existing."""
        prop = Property(
            source_email_id="qol-no-coords",
            title="QolNoCoords",
            municipality="Navia",
        )
        db.session.add(prop)
        db.session.commit()
        payload = _service().enrich(prop, commit=False)
        assert payload["municipality"]["status"] == "ok"
        assert payload["supermarkets"]["status"] == "no_coordinates"
        assert payload["hospitals"]["status"] == "no_coordinates"

    def test_block_is_written_and_score_neutral(self, app, reference_files):
        prop = self._property()
        before = (prop.score_total, prop.score_investment, prop.score_lifestyle)
        _service().enrich(prop, commit=True)
        stored = db.session.get(Property, prop.id)
        block = stored.enrichment["quality_of_life"]
        assert block["municipality"]["ine_code"] == "33041"
        assert "updated_at" in block
        after = (stored.score_total, stored.score_investment, stored.score_lifestyle)
        assert after == before, "the QoL block must never move a score"


class TestSupermarketReachClient:
    """The new Overpass query rides the shared transport: refusals surface,
    elements parse, unnamed shops stay, nearest-first with a top-N cap."""

    def _client(self, monkeypatch, elements=None, failure=None):
        service = EnrichmentService()
        monkeypatch.setattr(
            service, "_overpass_elements", lambda query: (elements, failure)
        )
        # The cache would otherwise return a previous test's answer.
        monkeypatch.setattr(
            "services.enrichment_service.get_cached_enrichment_data",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "services.enrichment_service.cache_enrichment_data",
            lambda *a, **k: None,
        )
        return service

    def test_refusal_passes_through(self, monkeypatch):
        service = self._client(
            monkeypatch, failure=GoogleApiFailure(reason="overpass_query_error")
        )
        reading = service.fetch_osm_supermarket_reach(43.5, -6.8)
        assert reading.failure is not None
        assert reading.items is None

    def test_elements_parse_nearest_first_with_center_fallback(self, monkeypatch):
        elements = [
            {
                "type": "way",
                "center": {"lat": 43.6, "lon": -6.8},
                "tags": {
                    "shop": "supermarket",
                    "name": "Alimerka",
                    "brand": "Alimerka",
                },
            },
            {
                "type": "node",
                "lat": 43.51,
                "lon": -6.8,
                "tags": {"shop": "convenience"},
            },  # unnamed village shop stays
        ]
        service = self._client(monkeypatch, elements=elements)
        reading = service.fetch_osm_supermarket_reach(43.5, -6.8)
        assert reading.failure is None
        names = [item["name"] for item in reading.items]
        assert names == [None, "Alimerka"], "nearest first, unnamed kept"
        assert all("distance_km" in item for item in reading.items)
