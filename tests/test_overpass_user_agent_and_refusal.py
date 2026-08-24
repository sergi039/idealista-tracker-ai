"""Regression tests: a refused Overpass call is not "no amenities nearby".

`EnrichmentService._enrich_with_osm_data()` posted to overpass-api.de without a
User-Agent, so `requests` sent its default `python-requests/x.y.z`. Measured
against the live instance on 2026-08-09, that is answered with `406 Not
Acceptable` -- and so is any User-Agent carrying a parenthetical comment. Only
a bare product token is served. Every OSM enrichment call had therefore been
failing, and because the only check was `if response.status_code == 200`, the
refusal was silently skipped and the property kept an absent amenity list that
read exactly like "Overpass looked and found nothing".

That is the #98 defect in a second place: a refused API written out as a
computed negative.

The behaviour pinned down here:

* the request carries `User-Agent: IdealistaRank/1.0`, a bare product token;
* a non-200 answer is stored as `osm_amenities_status.state == "unavailable"`
  with a reason code, and never as an empty `osm_amenities`;
* a refusal is never written to the enrichment cache;
* an answered run with nothing nearby is still a success -- empty amenities
  with `state == "ok"`;
* the 504 that Overpass returns while both of its two per-IP slots are busy is
  retried with a backoff measured in tens of seconds, not the half-second
  default;
* the marker survives `commit()`, and counts left from an earlier run are
  carried with the age that lets the page label them stale;
* a 200 whose body carries a `remark` -- how Overpass reports its own query
  timeouts and out-of-memory failures -- is a refusal, not an empty answer,
  and never reaches the seven-day cache.

The last three come from the independent review of PR #144.

It caught that the first version of this fix asserted only against the
in-memory object: `Land.infrastructure_extended` is a plain `db.Column(JSON)`
with no `MutableDict`, so mutating the loaded dict and assigning it back handed
SQLAlchemy the same object twice, no UPDATE was emitted, and every marker was
lost on commit. A test that never reloads cannot see that.

It then caught that the fix still had a hole of exactly the kind it was written
to close: Overpass signals a server-side failure *inside* a 200, and reading
`elements` straight off such a body turns a timed-out query into "no amenities
nearby" -- cached for a week.
"""

import logging
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests

from app import create_app, db
from models import Land, Property, SearchProfile
from services.enrichment_service import (
    OSM_REASON_QUERY_ERROR,
    OSM_STATE_OK,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
    EnrichmentService,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import (
    REASON_HTTP_ERROR,
    REASON_MALFORMED_RESPONSE,
    REASON_NETWORK_ERROR,
)
from utils.http import HTTP_USER_AGENT


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
def service():
    return EnrichmentService()


@pytest.fixture
def land(app):
    with app.app_context():
        land = Land(
            source_email_id="test_overpass_ua_1",
            title="Test Land for Overpass",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("39.4699"),
            location_lon=Decimal("-0.3763"),
        )
        db.session.add(land)
        db.session.commit()
        return land.id


def _listing_with_enrichment(app, source_id, infrastructure_extended):
    """A Property whose enrichment blob carries the OSM section, for the page."""
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        prop = Property(
            source_email_id=source_id,
            title="OverpassStaleFixture",
            search_profile_id=profile.id,
            listing_status="active",
            municipality="Cudillero",
            location_lat=43.56,
            location_lon=-6.15,
            enrichment={"infrastructure_extended": infrastructure_extended},
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


def _response(status_code=200, payload=None, raises=None):
    response = Mock()
    response.status_code = status_code
    if raises is not None:
        response.json.side_effect = raises
    else:
        response.json.return_value = payload if payload is not None else {}
    return response


class TestOutgoingRequest:
    """The header that decides whether overpass-api.de answers at all."""

    @patch("services.enrichment_service.request_with_retries")
    def test_sends_the_bare_product_token_user_agent(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})
            service._enrich_with_osm_data(db.session.get(Land, land))

        headers = mock_request.call_args.kwargs["headers"]
        assert headers["User-Agent"] == HTTP_USER_AGENT

    def test_user_agent_is_a_bare_token(self):
        """A parenthetical comment is refused just like the default UA."""
        assert "(" not in HTTP_USER_AGENT and ")" not in HTTP_USER_AGENT
        assert not HTTP_USER_AGENT.lower().startswith("python-requests")
        assert HTTP_USER_AGENT == "IdealistaRank/1.0"

    @patch("services.enrichment_service.request_with_retries")
    def test_retry_backoff_out_waits_a_busy_query_slot(
        self, mock_request, app, service, land
    ):
        """Overpass answers 504 while both per-IP slots are busy.

        A slot frees up in roughly a minute, so the half-second default backoff
        gives up long before the server would have answered.
        """
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})
            service._enrich_with_osm_data(db.session.get(Land, land))

        kwargs = mock_request.call_args.kwargs
        assert kwargs["backoff_base"] >= 8.0
        assert kwargs["max_attempts"] >= 3
        # Total wait across retries has to be tens of seconds, not sub-second.
        waits = sum(
            kwargs["backoff_base"] * (2**attempt)
            for attempt in range(kwargs["max_attempts"] - 1)
        )
        assert waits >= 30.0


class TestRefusalIsNotAnEmptyResult:
    """The #98 line: refused and empty must not look the same."""

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_406_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land, caplog
    ):
        with app.app_context():
            mock_request.return_value = _response(status_code=406)

            row = db.session.get(Land, land)
            with caplog.at_level(logging.ERROR):
                failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == REASON_HTTP_ERROR
            assert failure.http_status == 406

            infrastructure = row.infrastructure_extended or {}
            # The whole point: no amenity list is invented for a refusal.
            assert "osm_amenities" not in infrastructure
            status = infrastructure[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_UNAVAILABLE
            assert status["reason"] == REASON_HTTP_ERROR
            assert status["http_status"] == 406

        # A refusal is loud, not swallowed.
        assert any("OSM amenities unavailable" in r.message for r in caplog.records)
        # And it is never cached, or the next run would reuse the refusal.
        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_504_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(status_code=504)

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.http_status == 504
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_network_error_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.side_effect = requests.ConnectionError("no route to host")

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == REASON_NETWORK_ERROR
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_a_200_carrying_a_remark_is_not_an_empty_answer(
        self, mock_request, mock_cache, app, service, land
    ):
        """Overpass reports its own failures inside a 200.

        A query that times out or runs out of memory comes back as HTTP 200
        with a `remark` and an empty `elements`. Counting that as "nothing
        within 2km" is exactly the defect this module exists to remove -- and
        the seven-day cache would then keep serving it.
        """
        with app.app_context():
            mock_request.return_value = _response(
                payload={
                    "version": 0.6,
                    "elements": [],
                    "remark": (
                        'runtime error: Query timed out in "query" at line 3 '
                        "after 25 seconds."
                    ),
                }
            )

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == OSM_REASON_QUERY_ERROR
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
            assert infrastructure[OSM_STATUS_KEY]["reason"] == OSM_REASON_QUERY_ERROR

        # Above all: a timed-out query must never reach the week-long cache.
        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_a_200_without_an_elements_list_is_not_an_empty_answer(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(payload={"version": 0.6})

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == REASON_MALFORMED_RESPONSE
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_non_json_body_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        """A 200 carrying an HTML error page is still not an answer."""
        with app.app_context():
            mock_request.return_value = _response(raises=ValueError("not json"))

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()


class TestTheMarkerActuallyPersists:
    """A marker that only exists in memory records nothing.

    `Land.infrastructure_extended` is a plain `db.Column(JSON)` — no
    `MutableDict` — so SQLAlchemy decides whether to emit an UPDATE by
    comparing the old value against the new one. Mutating the loaded dict and
    assigning it straight back gives it the same object twice, the attribute
    never goes dirty, and the write is dropped at flush. Every assertion here
    reloads from the database rather than trusting the instance.
    """

    @patch("services.enrichment_service.request_with_retries")
    def test_refusal_survives_a_commit_and_reload(self, mock_request, app, service):
        with app.app_context():
            row = Land(
                source_email_id="test_overpass_persist_1",
                title="Persisted refusal",
                municipality="Valencia",
                land_type="developed",
                price=Decimal("150000.00"),
                area=Decimal("1500.00"),
                location_lat=Decimal("39.4699"),
                location_lon=Decimal("-0.3763"),
                # A column that already holds something, as a real row does.
                infrastructure_extended={"existing": True},
            )
            db.session.add(row)
            db.session.commit()
            land_id = row.id

            mock_request.return_value = _response(status_code=406)
            service._enrich_with_osm_data(db.session.get(Land, land_id))
            db.session.commit()
            db.session.expire_all()

            reloaded = db.session.get(Land, land_id).infrastructure_extended or {}
            assert reloaded[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
            assert reloaded[OSM_STATUS_KEY]["http_status"] == 406
            # The merge must not drop what was already in the column.
            assert reloaded["existing"] is True

    @patch("services.enrichment_service.request_with_retries")
    def test_counts_survive_a_commit_and_reload(self, mock_request, app, service):
        with app.app_context():
            row = Land(
                source_email_id="test_overpass_persist_2",
                title="Persisted counts",
                municipality="Valencia",
                land_type="developed",
                price=Decimal("150000.00"),
                area=Decimal("1500.00"),
                location_lat=Decimal("39.4699"),
                location_lon=Decimal("-0.3763"),
                infrastructure_extended={"existing": True},
            )
            db.session.add(row)
            db.session.commit()
            land_id = row.id

            mock_request.return_value = _response(
                payload={"elements": [{"tags": {"amenity": "school"}}]}
            )
            service._enrich_with_osm_data(db.session.get(Land, land_id))
            db.session.commit()
            db.session.expire_all()

            reloaded = db.session.get(Land, land_id).infrastructure_extended or {}
            assert reloaded["osm_amenities"] == {"school": 1}
            assert reloaded[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
            assert reloaded["existing"] is True


class TestStaleCountsAreLabelled:
    """Counts from an earlier run are kept, but never presented as current."""

    @patch("services.enrichment_service.request_with_retries")
    def test_a_refusal_carries_the_age_of_the_counts_it_left_behind(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            row = db.session.get(Land, land)

            # A good run measures the counts...
            mock_request.return_value = _response(
                payload={"elements": [{"tags": {"amenity": "school"}}]}
            )
            service._enrich_with_osm_data(row)
            measured_at = row.infrastructure_extended[OSM_STATUS_KEY]["measured_at"]
            assert measured_at

            # ...then the cache expires and Overpass refuses the next one.
            # Without clearing it the second call is a cache hit and never
            # reaches the network - which is the whole point of the cache, and
            # also why the stale-counts case only shows up once it lapses.
            cache.clear()
            mock_request.return_value = _response(status_code=406)
            service._enrich_with_osm_data(row)

            infrastructure = row.infrastructure_extended
            status = infrastructure[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_UNAVAILABLE
            # The real counts are not deleted -- they were true once...
            assert infrastructure["osm_amenities"] == {"school": 1}
            # ...but they keep the timestamp of the run that measured them, so
            # the page can say how old they are instead of showing them as
            # though this run had just produced them.
            assert status["measured_at"] == measured_at
            assert status["checked_at"] > status["measured_at"]

    def test_the_detail_page_marks_stale_counts(self, app):
        """The real page, rendered: counts under a refusal are not current."""
        listing = _listing_with_enrichment(
            app,
            "overpass_stale_counts",
            {
                "osm_amenities": {"school": 2},
                "osm_amenities_status": {
                    "state": "unavailable",
                    "reason": "http_error",
                    "http_status": 406,
                    "measured_at": "2026-08-01T10:00:00+00:00",
                    "checked_at": "2026-08-09T10:00:00+00:00",
                },
            },
        )

        body = app.test_client().get(f"/properties/{listing}").get_data(as_text=True)
        assert "Nearby Amenities (last known)" in body
        assert "Overpass was unavailable" in body
        assert "2026-08-01" in body
        # The count is still shown -- it was true once -- but never as current.
        assert "Nearby Amenities:" not in body

    def test_the_detail_page_shows_answered_counts_as_current(self, app):
        listing = _listing_with_enrichment(
            app,
            "overpass_current_counts",
            {
                "osm_amenities": {"school": 2},
                "osm_amenities_status": {
                    "state": "ok",
                    "measured_at": "2026-08-09T10:00:00+00:00",
                    "checked_at": "2026-08-09T10:00:00+00:00",
                },
            },
        )

        body = app.test_client().get(f"/properties/{listing}").get_data(as_text=True)
        assert "Nearby Amenities:" in body
        assert "last known" not in body
        assert "Overpass was unavailable" not in body


class TestAnAnsweredCallIsNotRelabelled:
    """Nothing after the answer may turn a success into a refusal."""

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_a_failing_cache_write_does_not_make_it_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        """The cache is an optimisation, not the source of the verdict."""
        with app.app_context():
            mock_request.return_value = _response(
                payload={"elements": [{"tags": {"amenity": "school"}}]}
            )
            mock_cache.side_effect = RuntimeError("redis is down")

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            infrastructure = row.infrastructure_extended or {}
            assert infrastructure["osm_amenities"] == {"school": 1}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
            assert "reason" not in infrastructure[OSM_STATUS_KEY]

    @patch("services.enrichment_service.get_cached_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_a_cache_hit_keeps_the_age_of_the_cached_counts(
        self, mock_request, mock_cached, app, service, land
    ):
        """A week-old cache entry must not be reported as measured today.

        The staleness label on the property page is built from `measured_at`,
        so refreshing it on every cache read would make six-day-old counts
        claim to be current.
        """
        with app.app_context():
            mock_cached.return_value = {
                "counts": {"school": 1},
                # The named items ride in the same entry since the v3 bump;
                # an entry without them is a miss, not a hit.
                "items": {"school": []},
                "measured_at": "2026-08-01T10:00:00+00:00",
            }

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            mock_request.assert_not_called()
            status = row.infrastructure_extended[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_OK
            assert status["measured_at"] == "2026-08-01T10:00:00+00:00"
            # checked_at is now; measured_at is when the data was produced.
            assert status["checked_at"] > status["measured_at"]

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_a_fresh_answer_caches_its_measurement_time(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(
                payload={"elements": [{"tags": {"amenity": "school"}}]}
            )

            row = db.session.get(Land, land)
            service._enrich_with_osm_data(row)

            payload = mock_cache.call_args.args[3]
            assert payload["counts"] == {"school": 1}
            assert (
                payload["measured_at"]
                == row.infrastructure_extended[OSM_STATUS_KEY]["measured_at"]
            )


class TestAnsweredRuns:
    """An answer stays an answer, including an empty one."""

    @patch("services.enrichment_service.request_with_retries")
    def test_nothing_nearby_is_a_success_not_a_refusal(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            infrastructure = row.infrastructure_extended or {}
            # Empty *and* answered - distinguishable from the refusal above
            # only because of the status marker.
            assert infrastructure["osm_amenities"] == {}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
            assert "reason" not in infrastructure[OSM_STATUS_KEY]

    @patch("services.enrichment_service.request_with_retries")
    def test_amenities_are_counted_and_marked_ok(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(
                payload={
                    "elements": [
                        {"tags": {"amenity": "supermarket"}},
                        {"tags": {"amenity": "supermarket"}},
                        {"tags": {"amenity": "school"}},
                    ]
                }
            )

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            infrastructure = row.infrastructure_extended or {}
            assert infrastructure["osm_amenities"] == {"supermarket": 2, "school": 1}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
