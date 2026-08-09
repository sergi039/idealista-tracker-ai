"""Regression tests for #153: an enrichment run says how complete it was.

`EnrichmentService.enrich_land()` calls three sources and used to hand back a
bare boolean decided by Google alone, with the reason Overpass did not count
written in a comment above the `if`. The owner confirmed the asymmetry on
2026-08-09 and chose to state it in the shape of the code instead: the run now
reduces its sources to `ok` / `degraded` / `unavailable`, exactly as
`PropertyTravelService` already does with `TRAVEL_STATE_DEGRADED`, stamps that
verdict on the record and returns `state != unavailable`.

The behaviour pinned down here:

* nothing refused is `ok`, and the run returns True;
* only Overpass refusing is `degraded` - still True, because the score reads
  only what Google Places writes, and a 504 from a busy per-IP slot is routine;
* Google refusing is `unavailable` and returns False, with `decisive` marking
  which refusal made it so;
* a decisive refusal outranks an advisory one in the same run;
* the verdict survives `commit()`. `Land.infrastructure_extended` is a plain
  `db.Column(JSON)` with no `MutableDict`, so a status assembled by mutating the
  loaded dict in place is never flushed - the trap the review of #144 found, and
  a test that never reloads cannot see it;
* stamping the run verdict does not clobber the per-source
  `osm_amenities_status`: one says what the last Overpass call did, the other
  what the run as a whole produced.

`degraded` exists so that "an advisory source refused" stops being reported as
an unqualified success. Collapsing it back into `ok`, or promoting it to
`unavailable`, are both changes to a decision the owner made in #153.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land
from services.enrichment_service import (
    ENRICHMENT_STATE_DEGRADED,
    ENRICHMENT_STATE_OK,
    ENRICHMENT_STATE_UNAVAILABLE,
    ENRICHMENT_STATUS_KEY,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
    EnrichmentService,
    enrichment_run_state,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import (
    REASON_HTTP_ERROR,
    REASON_REQUEST_DENIED,
    GoogleApiFailure,
)

OVERPASS_BUSY = GoogleApiFailure(reason=REASON_HTTP_ERROR, http_status=504)
PLACES_DENIED = GoogleApiFailure(reason=REASON_REQUEST_DENIED, status="REQUEST_DENIED")


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


# A key an earlier, successful run left behind. The fixture seeds it so the
# verdict is always merged into an `infrastructure_extended` that was loaded
# from the database, which is the case that can hit the #144 trap: a dict
# already attached to the instance, mutated in place, never flushed. Starting
# from NULL hides it, because assigning a brand-new dict is a detectable change
# whether the writer copied or not.
PRIOR_RUN_KEY = "supermarket_available"


@pytest.fixture
def land_id(app):
    land = Land(
        source_email_id="issue153_land",
        title="Land in Cudillero",
        municipality="Cudillero",
        location_lat=Decimal("43.6516865"),
        location_lon=Decimal("-7.8400525"),
        infrastructure_extended={PRIOR_RUN_KEY: True},
    )
    db.session.add(land)
    db.session.commit()
    db.session.expire_all()
    return land.id


def _run(land_id, *, places=None, maps=None, osm=None):
    """Run enrich_land with each source's outcome dictated, nothing else real.

    Every source returns either None ("it answered") or the GoogleApiFailure it
    refused with, which is the contract the three `_enrich_with_*` methods
    already have. Scoring and travel times are stubbed: they are local work and
    #153 is only about the verdict.
    """
    service = EnrichmentService()
    with patch.object(
        EnrichmentService, "_enrich_with_google_places", return_value=places
    ):
        with patch.object(
            EnrichmentService, "_enrich_with_google_maps", return_value=maps
        ):
            with patch.object(
                EnrichmentService, "_enrich_with_osm_data", return_value=osm
            ):
                with patch.object(EnrichmentService, "_analyze_environment"):
                    with patch("services.enrichment_service.ScoringService"):
                        with patch(
                            "services.travel_time_service.TravelTimeService"
                            ".calculate_travel_times",
                            return_value=False,
                        ):
                            return service.enrich_land(land_id)


def _reload_infrastructure(land_id):
    """Read the blob back from the database, not from the loaded object."""
    db.session.expire_all()
    return db.session.get(Land, land_id).infrastructure_extended or {}


def _reload_status(land_id):
    """Read the verdict back from the database, not from the loaded object."""
    return _reload_infrastructure(land_id).get(ENRICHMENT_STATUS_KEY)


class TestRunStateReduction:
    """The pure rule, without a database in the way."""

    def test_no_refusal_is_ok(self):
        assert enrichment_run_state([]) == ENRICHMENT_STATE_OK

    def test_advisory_refusal_alone_is_degraded(self):
        assert (
            enrichment_run_state([("OSM Overpass", OVERPASS_BUSY, False)])
            == ENRICHMENT_STATE_DEGRADED
        )

    def test_decisive_refusal_is_unavailable(self):
        assert (
            enrichment_run_state([("Google Places", PLACES_DENIED, True)])
            == ENRICHMENT_STATE_UNAVAILABLE
        )

    def test_decisive_refusal_outranks_advisory(self):
        assert (
            enrichment_run_state(
                [
                    ("Google Places", PLACES_DENIED, True),
                    ("OSM Overpass", OVERPASS_BUSY, False),
                ]
            )
            == ENRICHMENT_STATE_UNAVAILABLE
        )


class TestEnrichLandVerdict:
    def test_complete_run_is_ok_and_true(self, app, land_id):
        assert _run(land_id) is True

        infrastructure = _reload_infrastructure(land_id)
        status = infrastructure[ENRICHMENT_STATUS_KEY]
        assert status["state"] == ENRICHMENT_STATE_OK
        # A persisted timestamp is UTC and parseable, or it is not a timestamp:
        # `assert status["checked_at"]` alone would pass on any truthy string.
        assert datetime.fromisoformat(status["checked_at"]).utcoffset() == timedelta(0)
        # Nothing refused, so there is nothing to list.
        assert "refused" not in status
        # The verdict is merged into the blob, not written over it.
        assert infrastructure[PRIOR_RUN_KEY] is True

    def test_overpass_refusal_degrades_the_run_without_failing_it(self, app, land_id):
        # A 504 means both per-IP Overpass slots were busy. Routine, and the
        # score never read those amenities, so the run still produced what it
        # was asked for - but it is not an unqualified success either.
        assert _run(land_id, osm=OVERPASS_BUSY) is True

        status = _reload_status(land_id)
        assert status["state"] == ENRICHMENT_STATE_DEGRADED
        assert status["refused"] == [
            {
                "source": "OSM Overpass",
                "reason": REASON_HTTP_ERROR,
                "decisive": False,
                "http_status": 504,
            }
        ]

    def test_google_refusal_makes_the_run_unavailable(self, app, land_id):
        assert _run(land_id, places=PLACES_DENIED) is False

        status = _reload_status(land_id)
        assert status["state"] == ENRICHMENT_STATE_UNAVAILABLE
        assert status["refused"] == [
            {
                "source": "Google Places",
                "reason": REASON_REQUEST_DENIED,
                "decisive": True,
            }
        ]

    def test_distance_matrix_refusal_is_decisive_too(self, app, land_id):
        assert _run(land_id, maps=PLACES_DENIED) is False
        assert _reload_status(land_id)["state"] == ENRICHMENT_STATE_UNAVAILABLE

    def test_both_refusing_reports_both_and_stays_unavailable(self, app, land_id):
        assert _run(land_id, places=PLACES_DENIED, osm=OVERPASS_BUSY) is False

        status = _reload_status(land_id)
        assert status["state"] == ENRICHMENT_STATE_UNAVAILABLE
        assert [entry["source"] for entry in status["refused"]] == [
            "Google Places",
            "OSM Overpass",
        ]
        # Which refusal decided the verdict is on the record, not inferred.
        assert [entry["decisive"] for entry in status["refused"]] == [True, False]

    def test_run_verdict_leaves_the_per_source_osm_status_alone(self, app, land_id):
        # The real `_enrich_with_osm_data` stamps `osm_amenities_status` as it
        # refuses; the run verdict is written afterwards and must merge, not
        # replace. Both survive the same commit.
        def refuse(self, land):
            self._record_osm_status(land, OSM_STATE_UNAVAILABLE, OVERPASS_BUSY)
            return OVERPASS_BUSY

        service = EnrichmentService()
        with patch.object(
            EnrichmentService, "_enrich_with_google_places", return_value=None
        ):
            with patch.object(
                EnrichmentService, "_enrich_with_google_maps", return_value=None
            ):
                with patch.object(EnrichmentService, "_enrich_with_osm_data", refuse):
                    with patch.object(EnrichmentService, "_analyze_environment"):
                        with patch("services.enrichment_service.ScoringService"):
                            with patch(
                                "services.travel_time_service.TravelTimeService"
                                ".calculate_travel_times",
                                return_value=False,
                            ):
                                assert service.enrich_land(land_id) is True

        db.session.expire_all()
        infrastructure = db.session.get(Land, land_id).infrastructure_extended or {}
        assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert (
            infrastructure[ENRICHMENT_STATUS_KEY]["state"] == ENRICHMENT_STATE_DEGRADED
        )
