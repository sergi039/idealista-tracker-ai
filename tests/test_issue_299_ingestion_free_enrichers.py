"""Issue #299: ingestion runs the free enrichers, not only the paid ones.

`services/property_imap_service.py` enriched a new listing with geocoding,
paid travel and sea distance, but never ran the free pass that
`PropertyEnrichmentService.enrich_property` already contains -- OSM amenities
(#152), quality of life (#275) -- and nothing outside
`utils/backfill_sea_view.py` computed the sea-view verdict at all. Every row
ingested 13-14 Aug therefore arrived with no Extended Infrastructure card, no
QoL block and no sea-view verdict; the amenity absence renders exactly like
"nothing nearby", which is the #152 defect reintroduced for new rows.

Pinned here:

* ingesting a listing writes amenity counts, the QoL block and the sea-view
  verdict onto the new row -- with Overpass, the coastline and the reference
  files mocked, never live;
* an Overpass refusal is recorded as a refusal (`unavailable`, no invented
  counts) and never fails ingestion -- the row still lands and the run still
  reports it processed;
* a hand-set sea-view verdict survives the free pass untouched;
* the interactive Enrich flow (`enrich_property`) now computes the same
  sea-view verdict;
* without coordinates nothing reaches the network, and every part records an
  honest gap instead of silently not existing;
* `FREE_ENRICHMENT_ENABLED = False` keeps the pass off entirely.

Idioms follow tests/test_overpass_user_agent_and_refusal.py (transport-level
mock, reload-before-assert) and tests/test_quality_of_life.py (tmp reference
files).
"""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app import create_app, db
from config import Config
from models import Property
from services import quality_of_life_service as qol_module
from services import sea_view_service
from services.enrichment_service import (
    OSM_REASON_NO_COORDINATES,
    OSM_STATE_OK,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
)
from services.property_enrichment_service import PropertyEnrichmentService
from services.property_imap_service import PropertyIMAPService
from services.property_travel_service import PropertyTravelService
from tests import setup_test_environment
from utils.cache import cache

INTERNAL_DATE = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)

# Near Navia (Asturias): inside the CNH fixture's hospital coverage.
COORD_LAT = 43.5400
COORD_LON = -6.7200

INE_FIXTURE = {
    "generated_at": "2026-08-14T00:00:00+00:00",
    "source": {"renta": "INE ADRH (fixture)", "codes": "diccionario26.xlsx"},
    "municipalities": {
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
    ],
}

# One payload both Overpass parsers read their own elements from: the amenity
# counter takes `tags.amenity`, the supermarket parser takes `tags.shop` plus
# coordinates. Neither counts the other's element.
OVERPASS_ANSWER = {
    "elements": [
        {"tags": {"amenity": "school"}},
        {
            "tags": {"shop": "supermarket", "name": "Alimerka"},
            "lat": 43.541,
            "lon": -6.721,
        },
    ]
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        cache.clear()
        db.create_all()
        yield app
        db.drop_all()
        cache.clear()


@pytest.fixture
def flags(monkeypatch):
    """The ingestion path under test: paid steps off, the free pass on.

    Travel is replaced below by a stub that only geocodes -- in production
    that step is what puts coordinates on the row, and the free pass runs
    after it for exactly that reason.
    """
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", True)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", True)


@pytest.fixture
def geocoding_travel_stub(monkeypatch):
    """Stand-in for the paid travel step: it geocodes the row and nothing else.

    The real `calculate_for_property` starts with `ensure_coordinates` (paid
    Google geocoding) before its Places/Distance Matrix calls; those are the
    calls the free pass must never re-fire, so the stub is the whole paid
    boundary here.
    """

    def fake_travel(self, prop, commit=False):
        prop.location_lat = COORD_LAT
        prop.location_lon = COORD_LON
        prop.location_accuracy = "precise"
        if commit:
            db.session.commit()
        return True

    monkeypatch.setattr(PropertyTravelService, "calculate_for_property", fake_travel)


@pytest.fixture
def reference_files(tmp_path, monkeypatch):
    ine = tmp_path / "ine_municipal.json"
    cnh = tmp_path / "hospitals_cnh.json"
    ine.write_text(json.dumps(INE_FIXTURE), encoding="utf-8")
    cnh.write_text(json.dumps(CNH_FIXTURE), encoding="utf-8")
    monkeypatch.setattr(qol_module, "INE_DATA_PATH", str(ine))
    monkeypatch.setattr(qol_module, "CNH_DATA_PATH", str(cnh))
    return tmp_path


def _overpass_response(status_code=200, payload=None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


def _mock_overpass_answering(monkeypatch):
    transport = Mock(return_value=_overpass_response(payload=OVERPASS_ANSWER))
    monkeypatch.setattr("services.enrichment_service.request_with_retries", transport)
    return transport


def _mock_overpass_refusing(monkeypatch):
    transport = Mock(return_value=_overpass_response(status_code=504))
    monkeypatch.setattr("services.enrichment_service.request_with_retries", transport)
    return transport


def _mock_coastline_empty(monkeypatch):
    """Overpass answered: no coastline in range -- an earned negative."""
    monkeypatch.setattr(
        sea_view_service,
        "fetch_coastline_points",
        lambda lat, lon, session=None: [],
    )


def _mock_coastline_refusing(monkeypatch):
    def _refuse(lat, lon, session=None):
        raise sea_view_service.SeaViewSourceError("Overpass returned HTTP 504")

    monkeypatch.setattr(sea_view_service, "fetch_coastline_points", _refuse)


def _listing_email(idealista_id=990299):
    # The dict shape `get_idealista_emails()` produces for a listing email,
    # as in tests/test_issue_25_ingestion_integrity.py. No sea words in the
    # text, so the verdict below is geometry's alone.
    return {
        "type": "listing",
        "source_email_id": f"imap_free_{idealista_id}",
        "email_received_at": INTERNAL_DATE,
        "email_subject": "New home in your search: Navia",
        "email_sender": "Idealista <noresponder@idealista.com>",
        "title": "Casa rural en Navia",
        "url": f"https://www.idealista.com/inmueble/{idealista_id}/",
        "deal_type": "sale",
        "price": 250000.0,
        "area": 120,
        "area_type": "built",
        "municipality": "Navia",
        "search_profile_id": None,
        "property_category": "house",
        "property_subtype": None,
        "description": "Casa con finca en Navia",
        "attributes": None,
        "idealista_property_id": idealista_id,
    }


def _run_ingestion(monkeypatch, emails):
    service = PropertyIMAPService()
    monkeypatch.setattr(
        service, "get_idealista_emails", lambda max_results=None: list(emails)
    )
    return service.run_ingestion(sync_type="test")


def _reload(prop_id):
    db.session.expire_all()
    return db.session.get(Property, prop_id)


class TestIngestionRunsTheFreePass:
    """A new row leaves ingestion with amenities, QoL and a sea-view verdict."""

    def test_ingesting_a_listing_writes_all_three_blocks(
        self, app, flags, geocoding_travel_stub, reference_files, monkeypatch
    ):
        with app.app_context():
            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)

            created = _run_ingestion(monkeypatch, [_listing_email()])
            assert created == 1

            prop = _reload(Property.query.one().id)

            # Amenities: measured counts, marked as an answer (#152).
            infrastructure = prop.infrastructure_extended or {}
            assert infrastructure["osm_amenities"] == {"school": 1}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK

            # Quality of life: every part answered, none absent (#275).
            qol = prop.enrichment["quality_of_life"]
            assert qol["municipality"]["status"] == "ok"
            assert qol["municipality"]["ine_code"] == "33041"
            assert qol["supermarkets"]["status"] == "ok"
            assert qol["supermarkets"]["items"][0]["name"] == "Alimerka"
            assert qol["hospitals"]["status"] == "ok"

            # Sea view: a computed verdict, not an absent key. The coastline
            # cell answered empty, so this negative is earned.
            environment = prop.enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.NO
            detail = environment["sea_view_detail"]
            assert detail["source"] == "geometry"
            assert detail["geometry"]["reason"] == "no_coastline_in_range"

    def test_the_flag_keeps_the_pass_off(
        self, app, flags, geocoding_travel_stub, monkeypatch
    ):
        with app.app_context():
            monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", False)
            monkeypatch.setattr(
                "services.enrichment_service.request_with_retries",
                Mock(side_effect=AssertionError("the free pass must not run")),
            )

            created = _run_ingestion(monkeypatch, [_listing_email()])
            assert created == 1

            prop = _reload(Property.query.one().id)
            enrichment = prop.enrichment or {}
            assert "quality_of_life" not in enrichment
            assert "environment" not in enrichment
            assert not (prop.infrastructure_extended or {})


class TestARefusalIsRecordedAndNeverFailsIngestion:
    """The #98 line, at ingestion: refused is not empty, and not fatal."""

    def test_overpass_refusal_records_status_and_the_row_still_lands(
        self, app, flags, geocoding_travel_stub, reference_files, monkeypatch
    ):
        with app.app_context():
            _mock_overpass_refusing(monkeypatch)
            _mock_coastline_refusing(monkeypatch)

            created = _run_ingestion(monkeypatch, [_listing_email()])

            # The refusals must not fail the run or hold the row back.
            assert created == 1
            prop = _reload(Property.query.one().id)

            # Amenities: no invented counts, an honest refusal marker.
            infrastructure = prop.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            status = infrastructure[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_UNAVAILABLE
            assert status["http_status"] == 504

            # QoL: the Overpass part refused; the file-backed parts answered.
            qol = prop.enrichment["quality_of_life"]
            assert qol["supermarkets"]["status"] == "unavailable"
            assert qol["municipality"]["status"] == "ok"
            assert qol["hospitals"]["status"] == "ok"

            # Sea view: a refusal is `unknown`, never a computed `no`.
            environment = prop.enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.UNKNOWN
            geometry = environment["sea_view_detail"]["geometry"]
            assert geometry["reason"] == "coastline_source_unavailable"


class TestAHandSetVerdictSurvives:
    """The free pass computes beside a manual verdict, never over it."""

    def _stored_property(self, enrichment=None):
        prop = Property(
            source_email_id="free_pass_manual",
            title="Casa rural en Navia",
            municipality="Navia",
            location_lat=COORD_LAT,
            location_lon=COORD_LON,
            location_accuracy="precise",
            enrichment=enrichment,
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id

    def test_the_free_pass_leaves_a_manual_sea_view_alone(
        self, app, reference_files, monkeypatch
    ):
        with app.app_context():
            prop_id = self._stored_property(
                enrichment={
                    "environment": {
                        "sea_view": "yes",
                        "sea_view_detail": {
                            "source": "manual",
                            "reason": "set by hand",
                        },
                    }
                }
            )
            _mock_overpass_answering(monkeypatch)
            # Geometry says `no`; the owner said `yes`. The owner wins.
            _mock_coastline_empty(monkeypatch)

            prop = db.session.get(Property, prop_id)
            PropertyEnrichmentService().enrich_free_sources(prop, commit=True)

            prop = _reload(prop_id)
            environment = prop.enrichment["environment"]
            assert environment["sea_view"] == "yes"
            assert environment["sea_view_detail"]["source"] == "manual"
            # The rest of the pass still ran: the verdict lock is not a veto
            # over amenities or QoL.
            assert (prop.infrastructure_extended or {})["osm_amenities"] == {
                "school": 1
            }
            assert prop.enrichment["quality_of_life"]["municipality"]["status"] == "ok"


class TestTheEnrichFlowComputesSeaView:
    """`enrich_property` (the Enrich button) now writes the same verdict."""

    def test_enrich_property_stores_a_sea_view_verdict(
        self, app, reference_files, monkeypatch
    ):
        with app.app_context():
            prop = Property(
                source_email_id="enrich_button_sea_view",
                title="Casa rural en Navia",
                municipality="Navia",
                location_lat=COORD_LAT,
                location_lon=COORD_LON,
                location_accuracy="precise",
            )
            db.session.add(prop)
            db.session.commit()

            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)

            # The paid halves are not under test: location answers "already
            # placed", travel/sea-distance/pool do nothing. The free pass and
            # the final shared commit are real.
            service = PropertyEnrichmentService(
                location_service=Mock(),
                travel_service=Mock(),
                sea_distance_service=Mock(),
                pool_service=Mock(),
            )
            service.enrich_property(prop, recalc_scoring=False)

            reloaded = _reload(prop.id)
            environment = reloaded.enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.NO
            assert environment["sea_view_detail"]["source"] == "geometry"


class TestNoCoordinatesIsAnHonestGapNotSilence:
    """Without the paid geocode nothing reaches the network, and every part
    says it was never asked instead of silently not existing."""

    def test_ingestion_without_coordinates_records_the_gaps(
        self, app, flags, reference_files, monkeypatch
    ):
        with app.app_context():
            # No travel step, so the row keeps no coordinates.
            monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
            monkeypatch.setattr(
                "services.enrichment_service.request_with_retries",
                Mock(side_effect=AssertionError("no coordinates, no Overpass")),
            )
            monkeypatch.setattr(
                sea_view_service,
                "fetch_coastline_points",
                Mock(side_effect=AssertionError("no coordinates, no coastline")),
            )

            created = _run_ingestion(monkeypatch, [_listing_email()])
            assert created == 1
            prop = _reload(Property.query.one().id)

            status = (prop.infrastructure_extended or {})[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_UNAVAILABLE
            assert status["reason"] == OSM_REASON_NO_COORDINATES

            qol = prop.enrichment["quality_of_life"]
            assert qol["supermarkets"]["status"] == "no_coordinates"
            # The INE context needs no coordinates and still answers.
            assert qol["municipality"]["status"] == "ok"

            environment = prop.enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.UNKNOWN
            geometry = environment["sea_view_detail"]["geometry"]
            assert geometry["reason"] == "no_coordinates"
