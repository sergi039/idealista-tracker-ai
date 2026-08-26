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


# Ordinary listing prose on this coast, and the reason the AI branch matters:
# `evaluate_text` only consults the bridge when the text mentions the sea, so a
# fixture without these words would make every "the bridge was not called"
# assertion below pass without proving anything.
SEA_TITLE = "Casa con vistas al mar en Navia"
SEA_DESCRIPTION = "Vivienda con vistas al mar, finca y garaje. Cerca de la playa."

# No sea words at all: the text signal short-circuits to `none`, so the verdict
# is geometry's alone. Used where a *geometry* outcome is what is asserted.
PLAIN_TITLE = "Casa rural en Navia"
PLAIN_DESCRIPTION = "Casa con finca y huerta en Navia"


def _listing_email(idealista_id=990299, title=PLAIN_TITLE, description=None):
    # The dict shape `get_idealista_emails()` produces for a listing email,
    # as in tests/test_issue_25_ingestion_integrity.py.
    return {
        "type": "listing",
        "source_email_id": f"imap_free_{idealista_id}",
        "email_received_at": INTERNAL_DATE,
        "email_subject": "New home in your search: Navia",
        "email_sender": "Idealista <noresponder@idealista.com>",
        "title": title,
        "url": f"https://www.idealista.com/inmueble/{idealista_id}/",
        "deal_type": "sale",
        "price": 250000.0,
        "area": 120,
        "area_type": "built",
        "municipality": "Navia",
        "search_profile_id": None,
        "property_category": "house",
        "property_subtype": None,
        "description": PLAIN_DESCRIPTION if description is None else description,
        "attributes": None,
        "idealista_property_id": idealista_id,
    }


def _sea_listing_email(idealista_id=990300):
    return _listing_email(
        idealista_id=idealista_id, title=SEA_TITLE, description=SEA_DESCRIPTION
    )


def _mock_bridge(monkeypatch, claim="view"):
    """The AI bridge, recording every call. Returns the call list.

    `subscription_transport.complete` runs a cold Claude CLI on the owner's
    subscription with a 600 s timeout (#201). An unattended ingest loop must
    never reach it -- that is the blocker this suite exists for.

    It **records** rather than raising, and that is deliberate: a mock that
    raised `AssertionError` proved nothing, because `classify_text_with_ai`
    catches `Exception` and falls back to the keyword path, so the run looked
    identical whether or not the bridge had been called. Caught here by
    mutating `use_ai=False` back to `True` and watching the suite stay green.
    An empty call list is the only honest evidence.
    """
    calls = []

    def _complete(prompt, provider=None, system=None, **kwargs):
        calls.append({"provider": provider, "prompt": prompt})
        return {"text": json.dumps({"claim": claim, "quote": "vistas al mar"})}

    monkeypatch.setattr("services.subscription_transport.complete", _complete)
    return calls


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
            # The coordinates it describes, so a later refused run can tell
            # whether this verdict still belongs to the row.
            assert detail["origin"] == {"lat": COORD_LAT, "lon": COORD_LON}

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


class TestTheAiBridgeIsNeverReachedUnattended:
    """The blocker: one press, one subscription call — and ingestion is no press.

    `sea_view_service.evaluate_text` asks the owner's Claude subscription what
    a mention of the sea means, through `tools/ai_bridge.py` — a cold CLI run
    with a 600 s timeout (#201). Ingestion runs unattended over a whole batch
    of alert emails, so it must take the keyword path; the Enrich button, where
    an owner pressed something, may still ask.
    """

    def test_the_fixture_text_really_reaches_the_ai_branch(self):
        """Without this, every assertion below would pass vacuously.

        `evaluate_text` returns `none` before consulting the bridge when the
        text does not mention the sea at all, so a fixture with no sea words
        proves nothing about who calls the bridge.
        """
        view_hits, proximity_hits = sea_view_service._matched_keywords(
            f"{SEA_DESCRIPTION} {SEA_TITLE}"
        )
        assert view_hits, "the sea fixture must match VIEW_KEYWORDS"
        assert proximity_hits, "and the proximity phrases too"
        # And the plain fixture must stay plain, or the geometry-only
        # assertions elsewhere in this file stop meaning what they say.
        assert sea_view_service._matched_keywords(
            f"{PLAIN_DESCRIPTION} {PLAIN_TITLE}"
        ) == ([], [])

    def test_ingestion_never_calls_the_bridge_even_for_a_sea_listing(
        self, app, flags, geocoding_travel_stub, reference_files, monkeypatch
    ):
        with app.app_context():
            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)
            calls = _mock_bridge(monkeypatch)

            created = _run_ingestion(monkeypatch, [_sea_listing_email()])
            assert created == 1

            # The evidence that matters: the CLI was never spawned.
            assert calls == []

            prop = _reload(Property.query.one().id)
            detail = prop.enrichment["environment"]["sea_view_detail"]
            # The keyword path ran and says so; it is not silently "ai".
            assert detail["text"]["source"] == "keywords_only"
            # And it took that path by choice, not because a call failed:
            # a swallowed bridge error would leave `ai_error` behind.
            assert "ai_error" not in detail["text"]
            assert detail["text"]["claim"] == sea_view_service.TEXT_VIEW
            # And the verdict is still a real one: the listing claims a view,
            # the terrain disagrees, so one unopposed source is `likely`.
            assert prop.enrichment["environment"]["sea_view"] == (
                sea_view_service.LIKELY
            )

    def test_ingestion_without_coordinates_still_never_calls_the_bridge(
        self, app, flags, reference_files, monkeypatch
    ):
        """The text signal runs *before* the coordinate check, so a row that
        never geocoded is the one path where the AI branch is all there is."""
        with app.app_context():
            monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
            calls = _mock_bridge(monkeypatch)
            monkeypatch.setattr(
                "services.enrichment_service.request_with_retries",
                Mock(side_effect=AssertionError("no coordinates, no Overpass")),
            )

            created = _run_ingestion(monkeypatch, [_sea_listing_email()])
            assert created == 1

            assert calls == []
            prop = _reload(Property.query.one().id)
            detail = prop.enrichment["environment"]["sea_view_detail"]
            assert detail["text"]["source"] == "keywords_only"
            assert "ai_error" not in detail["text"]
            assert detail["geometry"]["reason"] == "no_coordinates"

    def test_the_enrich_button_does_use_the_bridge(
        self, app, reference_files, monkeypatch
    ):
        """The other half of the contract: a press may spend a CLI run."""
        with app.app_context():
            prop = Property(
                source_email_id="enrich_button_ai",
                title=SEA_TITLE,
                description=SEA_DESCRIPTION,
                municipality="Navia",
                location_lat=COORD_LAT,
                location_lon=COORD_LON,
                location_accuracy="precise",
            )
            db.session.add(prop)
            db.session.commit()

            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)
            calls = _mock_bridge(monkeypatch, claim="proximity")

            PropertyEnrichmentService(
                location_service=Mock(),
                travel_service=Mock(),
                sea_distance_service=Mock(),
                pool_service=Mock(),
            ).enrich_property(prop, recalc_scoring=False)

            assert len(calls) == 1
            assert calls[0]["provider"] == "claude"
            detail = _reload(prop.id).enrichment["environment"]["sea_view_detail"]
            # The AI's reading is what was stored, not the keyword guess:
            # "vistas al mar" matched, but the bridge called it proximity.
            assert detail["text"]["source"] == "ai"
            assert detail["text"]["claim"] == sea_view_service.TEXT_PROXIMITY


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
            PropertyEnrichmentService().enrich_free_sources(
                prop, commit=True, use_ai=False
            )

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


class TestARefusalNeverErasesAMeasuredVerdict:
    """A source that refused knows nothing about this property.

    The dangerous path is the *second* press of Enrich, not the first ingest:
    a new row has no verdict to lose, but `enrich_property` now recomputes the
    sea view on every press, so a row already carrying a computed `yes` would
    have had it replaced with `unknown` the first time Overpass was busy.
    `SeaDistanceService` has applied this rule to `enrichment["sea"]` since
    #98; it lives in `apply_to_property`, the one writer, so the backfill and
    every future caller inherit it.
    """

    def _stored(
        self,
        source_id,
        environment,
        lat=COORD_LAT,
        lon=COORD_LON,
        title=SEA_TITLE,
        description=SEA_DESCRIPTION,
    ):
        """A row that already carries a verdict, re-evaluated below.

        The text defaults to the *sea* fixture on purpose. An earlier version
        of this class used the plain one, reasoning that "a view claim in the
        text would answer `likely` on its own" — true, and exactly why it had
        to be tested: `combine()` turns a refused geometry plus a view claim
        into `likely`, not `unknown`, so a guard keyed on the verdict being
        `unknown` silently downgraded every measured `yes` on this coast. The
        plain fixture stepped around the defect instead of at it.
        """
        prop = Property(
            source_email_id=source_id,
            title=title,
            description=description,
            municipality="Navia",
            location_lat=lat,
            location_lon=lon,
            location_accuracy="precise",
            enrichment={"environment": environment},
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id

    @staticmethod
    def _measured_verdict(
        state="yes",
        lat=COORD_LAT,
        lon=COORD_LON,
        geometry_state=sea_view_service.LIKELY,
        geometry_reason="clear_line_of_sight",
        source="text+geometry",
        reason="listing claims a view and terrain allows it",
    ):
        """The shape `evaluate_property` really writes, geometry block and all.

        The geometry half is what the repair reuses, so a fixture without it
        would test a row that cannot occur outside a hand-set verdict.
        """
        return {
            "sea_view": state,
            "sea_view_detail": {
                "source": source,
                "reason": reason,
                "text": {"claim": "view", "source": "keywords_only"},
                "geometry": {
                    "state": geometry_state,
                    "reason": geometry_reason,
                    "distance_m": 1200.0,
                    "coordinate_accuracy": "precise",
                },
                "origin": {"lat": lat, "lon": lon},
                "computed_at": "2026-08-10T09:00:00+00:00",
            },
        }

    def _recompute(self, prop_id, monkeypatch):
        _mock_overpass_answering(monkeypatch)
        _mock_coastline_refusing(monkeypatch)
        prop = db.session.get(Property, prop_id)
        PropertyEnrichmentService().enrich_free_sources(prop, commit=True, use_ai=False)
        return _reload(prop_id)

    def test_a_measured_yes_survives_a_refusal_on_a_sea_view_listing(
        self, app, reference_files, monkeypatch
    ):
        """The review finding: `yes` must not decay to `likely` on a 504.

        Text claims a view (as most listings here do) and the coastline
        lookup refuses. Recombining the fresh text with the terrain measured
        earlier gives back `yes`; trusting this run's refused geometry would
        give `likely` — a quiet downgrade of a two-source verdict.
        """
        with app.app_context():
            prop_id = self._stored("sea_view_kept_yes", self._measured_verdict("yes"))

            environment = self._recompute(prop_id, monkeypatch).enrichment[
                "environment"
            ]

            assert environment["sea_view"] == sea_view_service.YES
            detail = environment["sea_view_detail"]
            assert detail["source"] == "text+geometry"
            # The terrain is the earlier measurement, labelled as reused.
            assert detail["geometry"]["reused_measurement"] is True
            assert detail["geometry"]["state"] == sea_view_service.LIKELY
            # And the refusal is stamped: this run would have said `likely`.
            assert detail["last_attempt_state"] == sea_view_service.LIKELY
            assert detail["last_attempt_reason"] == "coastline_source_unavailable"
            assert detail["last_attempt_at"]
            assert "origin_unverified" not in detail

    def test_the_specific_terrain_reason_is_not_replaced_by_a_generic_one(
        self, app, reference_files, monkeypatch
    ):
        """A stored `likely` whose terrain disagreed keeps *why* it disagreed.

        Trusting the refusal would rewrite "terrain disagrees
        (no_coastline_in_range)" as "terrain not computable" — the same state,
        a strictly worse record of how it was reached.
        """
        with app.app_context():
            prop_id = self._stored(
                "sea_view_kept_reason",
                self._measured_verdict(
                    "likely",
                    geometry_state=sea_view_service.NO,
                    geometry_reason="no_coastline_in_range",
                    source="text",
                    reason="listing claims a view, terrain disagrees "
                    "(no_coastline_in_range)",
                ),
            )

            detail = self._recompute(prop_id, monkeypatch).enrichment["environment"][
                "sea_view_detail"
            ]

            assert detail["reason"] == (
                "listing claims a view, terrain disagrees (no_coastline_in_range)"
            )
            assert "not computable" not in detail["reason"]
            assert detail["last_attempt_reason"] == "coastline_source_unavailable"

    def test_a_measurement_from_other_coordinates_is_not_reused(
        self, app, reference_files, monkeypatch
    ):
        """Provenance is the point: the rule preserves *this* row's terrain."""
        with app.app_context():
            prop_id = self._stored(
                "sea_view_moved",
                self._measured_verdict("yes", lat=43.36, lon=-5.85),
            )

            environment = self._recompute(prop_id, monkeypatch).enrichment[
                "environment"
            ]

            # Nothing reusable, so this run's own refusal stands: the text
            # still claims a view, and the terrain is honestly not computable.
            detail = environment["sea_view_detail"]
            assert environment["sea_view"] == sea_view_service.LIKELY
            assert detail["reason"] == "listing claims a view, terrain not computable"
            assert detail["geometry"]["reason"] == "coastline_source_unavailable"
            assert "reused_measurement" not in detail["geometry"]

    def test_a_fresh_text_signal_is_honoured_over_the_reused_terrain(
        self, app, reference_files, monkeypatch
    ):
        """Preserving the geometry half is not freezing the verdict.

        The description no longer claims a view; the terrain measured earlier
        still allows one. That is `likely` from geometry alone — a
        whole-verdict rule would have re-asserted the stale `yes`.
        """
        with app.app_context():
            prop_id = self._stored(
                "sea_view_text_changed",
                self._measured_verdict("yes"),
                title=PLAIN_TITLE,
                description=PLAIN_DESCRIPTION,
            )

            environment = self._recompute(prop_id, monkeypatch).enrichment[
                "environment"
            ]

            assert environment["sea_view"] == sea_view_service.LIKELY
            detail = environment["sea_view_detail"]
            assert detail["source"] == "geometry"
            assert detail["text"]["claim"] == sea_view_service.TEXT_NONE
            assert detail["geometry"]["reused_measurement"] is True

    def test_a_verdict_from_before_origins_were_recorded_is_reused_but_labelled(
        self, app, reference_files, monkeypatch
    ):
        """Every verdict `utils/backfill_sea_view.py` wrote before this change
        carries no origin. Erasing those is the failure the rule exists to
        prevent, so the terrain is reused — with the unverified provenance
        stamped rather than assumed away."""
        with app.app_context():
            legacy = self._measured_verdict("yes")
            legacy["sea_view_detail"].pop("origin")
            prop_id = self._stored("sea_view_legacy", legacy)

            detail = self._recompute(prop_id, monkeypatch).enrichment["environment"][
                "sea_view_detail"
            ]

            assert detail["geometry"]["reused_measurement"] is True
            # The label lives on the terrain: it is a fact about where that
            # measurement was taken, not about this run.
            assert detail["geometry"]["origin_unverified"] is True
            assert detail["last_attempt_reason"] == "coastline_source_unavailable"

    def test_the_unverified_label_survives_a_second_outage(
        self, app, reference_files, monkeypatch
    ):
        """The label must be sticky, not re-derived from the origin each run.

        Repair #1 stamps the verdict with *today's* `origin` — correct, since
        that is the coordinate the verdict now describes, and it gives later
        runs real move-detection. But it also means repair #2 compares that
        synthetic origin against the same coordinates, finds them equal, and
        would drop the label while reusing the very same unverified terrain.
        The row would then read as better-provenanced than it is — #98's shape
        — so the label rides with the geometry until it is really re-measured.
        """
        with app.app_context():
            legacy = self._measured_verdict("yes")
            legacy["sea_view_detail"].pop("origin")
            prop_id = self._stored("sea_view_legacy_twice", legacy)

            first = self._recompute(prop_id, monkeypatch).enrichment["environment"]
            assert first["sea_view_detail"]["geometry"]["origin_unverified"] is True
            # Repair #1 recorded today's coordinates, which is what makes the
            # second comparison agree — the precondition for the defect.
            assert first["sea_view_detail"]["origin"] == {
                "lat": COORD_LAT,
                "lon": COORD_LON,
            }

            second = self._recompute(prop_id, monkeypatch).enrichment["environment"]

            assert second["sea_view_detail"]["geometry"]["origin_unverified"] is True
            assert second["sea_view_detail"]["geometry"]["reused_measurement"] is True
            assert second["sea_view"] == sea_view_service.YES

    def test_the_label_is_read_from_its_previous_home_too(
        self, app, reference_files, monkeypatch
    ):
        """A row repaired by the first version of this guard keeps its label.

        That version stamped `origin_unverified` on the top-level detail and
        wrote today's `origin` beside it, so such a row now compares as
        verified. Reading only the geometry would drop the label on its next
        outage — the same defect, one shape further back.
        """
        with app.app_context():
            previous = self._measured_verdict("yes")
            previous["sea_view_detail"]["origin_unverified"] = True
            prop_id = self._stored("sea_view_old_flag_home", previous)

            geometry = self._recompute(prop_id, monkeypatch).enrichment["environment"][
                "sea_view_detail"
            ]["geometry"]

            assert geometry["origin_unverified"] is True

    def test_a_successful_re_measurement_clears_the_unverified_label(
        self, app, reference_files, monkeypatch
    ):
        """Sticky is not permanent: real terrain replaces the borrowed one."""
        with app.app_context():
            legacy = self._measured_verdict("yes")
            legacy["sea_view_detail"].pop("origin")
            prop_id = self._stored("sea_view_legacy_remeasured", legacy)

            repaired = self._recompute(prop_id, monkeypatch).enrichment["environment"]
            assert repaired["sea_view_detail"]["geometry"]["origin_unverified"] is True

            # Overpass answers this time: the terrain is measured afresh.
            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)
            PropertyEnrichmentService().enrich_free_sources(
                db.session.get(Property, prop_id), commit=True, use_ai=False
            )

            geometry = _reload(prop_id).enrichment["environment"]["sea_view_detail"][
                "geometry"
            ]
            assert "origin_unverified" not in geometry
            assert "reused_measurement" not in geometry
            assert geometry["reason"] == "no_coastline_in_range"

    def test_two_outages_in_a_row_do_not_age_the_terrain_forward(
        self, app, reference_files, monkeypatch
    ):
        """The reused terrain keeps the time it was actually measured.

        On the second repair the stored `computed_at` is the *previous
        repair*, so reading it as the measurement time would let a stale
        terrain claim to be current — a slow-motion version of the staleness
        the amenity card labels (#144).
        """
        with app.app_context():
            prop_id = self._stored("sea_view_twice", self._measured_verdict("yes"))

            first = self._recompute(prop_id, monkeypatch).enrichment["environment"]
            second = self._recompute(prop_id, monkeypatch).enrichment["environment"]

            measured_at = first["sea_view_detail"]["geometry"]["measured_at"]
            assert measured_at == "2026-08-10T09:00:00+00:00"
            assert second["sea_view_detail"]["geometry"]["measured_at"] == measured_at
            assert second["sea_view"] == sea_view_service.YES

    def test_a_stored_unknown_geometry_has_nothing_to_lend(
        self, app, reference_files, monkeypatch
    ):
        """`unknown` terrain is the absence of a measurement, not one."""
        with app.app_context():
            previous = self._measured_verdict(
                "unknown",
                geometry_state=sea_view_service.UNKNOWN,
                geometry_reason="elevation_source_unavailable",
                source="none",
                reason="elevation_source_unavailable",
            )
            prop_id = self._stored(
                "sea_view_unknown",
                previous,
                title=PLAIN_TITLE,
                description=PLAIN_DESCRIPTION,
            )

            environment = self._recompute(prop_id, monkeypatch).enrichment[
                "environment"
            ]

            assert environment["sea_view"] == sea_view_service.UNKNOWN
            detail = environment["sea_view_detail"]
            assert detail["geometry"]["reason"] == "coastline_source_unavailable"
            assert "reused_measurement" not in detail["geometry"]
            assert "last_attempt_reason" not in detail

    def test_an_answering_source_still_overwrites(
        self, app, reference_files, monkeypatch
    ):
        """The rule must not freeze a verdict: a source that answered wins."""
        with app.app_context():
            prop_id = self._stored(
                "sea_view_recomputed",
                self._measured_verdict("yes"),
                title=PLAIN_TITLE,
                description=PLAIN_DESCRIPTION,
            )

            _mock_overpass_answering(monkeypatch)
            _mock_coastline_empty(monkeypatch)
            prop = db.session.get(Property, prop_id)
            PropertyEnrichmentService().enrich_free_sources(
                prop, commit=True, use_ai=False
            )

            environment = _reload(prop_id).enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.NO
            detail = environment["sea_view_detail"]
            assert detail["source"] == "geometry"
            assert detail["geometry"]["reason"] == "no_coastline_in_range"
            assert "reused_measurement" not in detail["geometry"]

    def test_a_computed_unknown_is_not_a_refusal(
        self, app, reference_files, monkeypatch
    ):
        """An approximate coordinate is an answer about this row, so it lands.

        Only the two source-refusal reasons bar an overwrite; a verdict we
        genuinely cannot compute must not hide behind an old measurement.
        """
        with app.app_context():
            prop_id = self._stored(
                "sea_view_approximate",
                self._measured_verdict("yes"),
                title=PLAIN_TITLE,
                description=PLAIN_DESCRIPTION,
            )
            prop = db.session.get(Property, prop_id)
            prop.location_accuracy = "approximate"
            db.session.commit()

            _mock_overpass_answering(monkeypatch)
            # Sea within reach of the centroid: geometry cannot decide, and
            # says so with `approximate_coordinates` rather than a refusal.
            monkeypatch.setattr(
                sea_view_service,
                "fetch_coastline_points",
                lambda lat, lon, session=None: [(COORD_LAT + 0.005, COORD_LON)],
            )
            PropertyEnrichmentService().enrich_free_sources(
                db.session.get(Property, prop_id), commit=True, use_ai=False
            )

            environment = _reload(prop_id).enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.UNKNOWN
            detail = environment["sea_view_detail"]
            assert detail["geometry"]["reason"] == "approximate_coordinates"
            assert "reused_measurement" not in detail["geometry"]

    def test_a_row_that_loses_its_coordinate_loses_its_verdict(
        self, app, reference_files, monkeypatch
    ):
        """The subject going away is not a refusal either (2026-08-26).

        The reproduction is six production rows a full backfill moved from a
        measured `no` to `unknown`. The pre-run snapshot says five of them were
        measured at 40.463667,-3.74922 -- the centre of Spain, which is what
        geocoding a #298 truncated title fragment returns -- and their
        coordinates have since been cleared. Keeping those would keep a claim
        about Madrid on an Asturian listing; row 132 is Carreno, on the coast,
        and its stored `no_coastline_in_range` was a false negative that
        survived only because it was measured 400 km inland.

        Nothing else stops this. A row with no coordinate has no origin, and
        `origins_agree` answers "cannot tell" rather than "moved", which
        `repaired_with_stored_geometry` reuses on -- so the *only* thing
        keeping the stored terrain out is that `no_coordinates` is not in
        `SOURCE_REFUSAL_REASONS`. `services/hazard_service.py` applied the
        opposite rule to this exact shape until the same day.
        """
        with app.app_context():
            prop_id = self._stored(
                "sea_view_lost_its_coordinate",
                self._measured_verdict(
                    "no",
                    geometry_state=sea_view_service.NO,
                    geometry_reason="no_coastline_in_range",
                    source="geometry",
                    reason="terrain disagrees (no_coastline_in_range)",
                ),
                title=PLAIN_TITLE,
                description=PLAIN_DESCRIPTION,
            )
            prop = db.session.get(Property, prop_id)
            prop.location_lat = None
            prop.location_lon = None
            prop.location_accuracy = "unknown"
            db.session.commit()

            _mock_overpass_answering(monkeypatch)
            # Deliberately *available*: the coastline source answering makes
            # this a test of the coordinate rule and not of a refusal that
            # happens to arrive alongside it.
            _mock_coastline_empty(monkeypatch)
            PropertyEnrichmentService().enrich_free_sources(
                db.session.get(Property, prop_id), commit=True, use_ai=False
            )

            environment = _reload(prop_id).enrichment["environment"]
            assert environment["sea_view"] == sea_view_service.UNKNOWN
            detail = environment["sea_view_detail"]
            geometry = detail["geometry"]
            assert geometry["reason"] == "no_coordinates"
            assert geometry["state"] == sea_view_service.UNKNOWN
            # By value, not by state alone: a repair would hand back the stored
            # terrain under a fresh top-level verdict, so the assertions that
            # matter are that the measurement itself is gone.
            assert "reused_measurement" not in geometry
            assert "distance_m" not in geometry
            assert "last_attempt_reason" not in detail


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
