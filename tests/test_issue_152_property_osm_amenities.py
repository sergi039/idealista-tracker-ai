"""Regression tests: the amenity lookup reaches the listings that exist (#152).

`EnrichmentService._enrich_with_osm_data` was reachable only from the legacy
`Land` endpoints. The Enrich button on `/properties/<id>` went to
`PropertyEnrichmentService.enrich_property`, which did coordinates, sea
distance, travel times and scoring -- and never asked Overpass anything. So 213
of 352 listings could not show nearby amenities however often the button was
pressed, and the Extended Infrastructure card was absent on their page, which
reads as "nothing nearby" rather than "never asked".

That is #98 and #144 one level up: not a refusal recorded as a negative, but a
question never asked, presented as an answer. Overpass is free and keyless, so
there was no billing argument for leaving it out either.

What is pinned here:

* `enrich_property` runs the amenity lookup and the counts survive a commit and
  reload, in `Property.enrichment["infrastructure_extended"]`;
* a refused Overpass call is stored as `state == "unavailable"` with a reason,
  never as empty counts, and does not fail the enrichment run -- no score reads
  these counts, and 504 is routine on an endpoint with two per-IP slots;
* the three refusals overpass-api.de actually delivers all reach the property
  path, because it goes through the same client as the `Land` path;
* a property mirrored from `lands` keeps the legacy keys its page already
  showed when the first fresh measurement lands on top of them;
* a property with no usable coordinates is recorded as not asked, and no
  paid geocoding is triggered to fix that;
* both Overpass callers share one pacing gate, so a bulk run over hundreds of
  rows is not hundreds of back-to-back queries;
* the backfill tool goes through the free amenity call only.
"""

import threading
import time
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.enrichment_service import (
    OSM_REASON_NO_COORDINATES,
    OSM_REASON_QUERY_ERROR,
    OSM_STATE_OK,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
    EnrichmentService,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import REASON_HTTP_ERROR
from utils.http import (
    HTTP_USER_AGENT,
    OVERPASS_GATE,
    OVERPASS_MIN_INTERVAL_S,
    RateGate,
)


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


def _response(status_code: int = 200, payload=None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {"elements": []}
    return response


def _amenities(*kinds):
    return _response(payload={"elements": [{"tags": {"amenity": k}} for k in kinds]})


def _property(app, source_id="prop_osm_1", *, lat="39.4699", lon="-0.3763", **kwargs):
    with app.app_context():
        profile = SearchProfile.query.filter_by(name="Subscription").first()
        if profile is None:
            profile = SearchProfile(
                name="Subscription",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(profile)
            db.session.commit()

        prop = Property(
            source_email_id=source_id,
            title="A house with shops nearby",
            municipality="Valencia",
            property_category="housing",
            property_subtype="house",
            search_profile_id=profile.id,
            listing_status="active",
            price=Decimal("250000.00"),
            location_lat=Decimal(lat) if lat is not None else None,
            location_lon=Decimal(lon) if lon is not None else None,
            **kwargs,
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


class _StubLocation:
    def ensure_coordinates(self, prop, refresh=False):
        return True


class _StubTravel:
    def calculate_for_property(self, prop, commit=False):
        return True


class _StubScoring:
    def calculate_for_property(self, prop, commit=False):
        return True


class _StubSea:
    def update_property(self, prop, *, commit=False):
        return None


def _enrichment_service_under_test(**overrides):
    """`PropertyEnrichmentService` with only the amenity half live."""
    from services.property_enrichment_service import PropertyEnrichmentService

    kwargs = {
        "location_service": _StubLocation(),
        "travel_service": _StubTravel(),
        "scoring_service": _StubScoring(),
        "sea_distance_service": _StubSea(),
    }
    kwargs.update(overrides)
    return PropertyEnrichmentService(**kwargs)


class TestTheEnrichButtonAsksOverpass:
    """The wiring the issue is about."""

    @patch("services.enrichment_service.request_with_retries")
    def test_enrichment_measures_amenities_and_they_survive_a_reload(
        self, mock_request, app
    ):
        mock_request.return_value = _amenities("supermarket", "school", "supermarket")
        property_id = _property(app)

        with app.app_context():
            prop = db.session.get(Property, property_id)
            assert _enrichment_service_under_test().enrich_property(prop) is True

            db.session.expire_all()
            reloaded = db.session.get(Property, property_id)
            section = reloaded.enrichment["infrastructure_extended"]

        assert section["osm_amenities"] == {"supermarket": 2, "school": 1}
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
        assert section[OSM_STATUS_KEY]["measured_at"]

    @patch("services.enrichment_service.request_with_retries")
    def test_the_request_carries_the_user_agent_overpass_accepts(
        self, mock_request, app
    ):
        mock_request.return_value = _amenities("cafe")
        property_id = _property(app, "prop_osm_ua")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            _enrichment_service_under_test().enrich_property(prop)

        headers = mock_request.call_args.kwargs["headers"]
        assert headers["User-Agent"] == HTTP_USER_AGENT

    @patch("services.enrichment_service.request_with_retries")
    def test_the_page_shows_what_was_measured(self, mock_request, app):
        """End to end: the card the 213 listings never had."""
        mock_request.return_value = _amenities("hospital", "pharmacy")
        property_id = _property(app, "prop_osm_page")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            _enrichment_service_under_test().enrich_property(prop)

        body = (
            app.test_client().get(f"/properties/{property_id}").get_data(as_text=True)
        )
        assert "Nearby Amenities" in body
        assert "Unavailable" not in body


class TestARefusalIsNotAnEmptyAnswer:
    """The #98 rule, on the property path this time."""

    @patch("services.enrichment_service.request_with_retries")
    def test_a_406_is_recorded_as_unavailable_and_never_as_empty_counts(
        self, mock_request, app
    ):
        mock_request.return_value = _response(status_code=406)
        property_id = _property(app, "prop_osm_406")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            _enrichment_service_under_test().enrich_property(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).enrichment[
                "infrastructure_extended"
            ]

        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert section[OSM_STATUS_KEY]["reason"] == REASON_HTTP_ERROR
        assert section[OSM_STATUS_KEY]["http_status"] == 406
        assert "osm_amenities" not in section

    @patch("services.enrichment_service.request_with_retries")
    def test_a_remark_inside_a_200_is_a_refusal(self, mock_request, app):
        """Overpass reports its own query timeouts inside a 200 body."""
        mock_request.return_value = _response(
            payload={"elements": [], "remark": "runtime error: Query timed out"}
        )
        property_id = _property(app, "prop_osm_remark")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            _enrichment_service_under_test().enrich_property(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).enrichment[
                "infrastructure_extended"
            ]

        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert section[OSM_STATUS_KEY]["reason"] == OSM_REASON_QUERY_ERROR
        assert "osm_amenities" not in section

    @patch("services.enrichment_service.request_with_retries")
    def test_a_refusal_never_reaches_the_cache(self, mock_request, app, service):
        mock_request.return_value = _response(status_code=504)
        property_id = _property(app, "prop_osm_cache")

        with app.app_context():
            with patch(
                "services.enrichment_service.cache_enrichment_data"
            ) as mock_cache:
                prop = db.session.get(Property, property_id)
                service.enrich_osm_amenities(prop)

            mock_cache.assert_not_called()

    @patch("services.enrichment_service.request_with_retries")
    def test_a_refused_lookup_does_not_fail_the_enrichment_run(self, mock_request, app):
        """Overpass answers 504 whenever both per-IP slots are busy.

        No score reads these counts, so failing the whole pass on that would
        report failure for a property whose Google half arrived intact -- the
        same asymmetry the legacy `enrich_land` already applies.
        """
        mock_request.return_value = _response(status_code=504)
        property_id = _property(app, "prop_osm_504")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            assert _enrichment_service_under_test().enrich_property(prop) is True

    @patch("services.enrichment_service.request_with_retries")
    def test_counts_from_an_earlier_run_are_kept_and_dated(
        self, mock_request, app, service
    ):
        property_id = _property(app, "prop_osm_stale")

        with app.app_context():
            prop = db.session.get(Property, property_id)

            mock_request.return_value = _amenities("school")
            service.enrich_osm_amenities(prop)
            measured_at = prop.infrastructure_extended[OSM_STATUS_KEY]["measured_at"]
            assert measured_at

            # The cache is what keeps the second call off the network, so the
            # stale case only appears once it lapses.
            cache.clear()
            mock_request.return_value = _response(status_code=406)
            service.enrich_osm_amenities(prop)

            section = prop.infrastructure_extended

        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        # True once, so not deleted -- but carrying the age of the run that
        # measured them, which is what lets the page label them stale.
        assert section["osm_amenities"] == {"school": 1}
        assert section[OSM_STATUS_KEY]["measured_at"] == measured_at


class TestTheRowsMirroredFromLands:
    """A first measurement must not hide what the page already showed."""

    @patch("services.enrichment_service.request_with_retries")
    def test_legacy_infrastructure_keys_survive_the_first_write(
        self, mock_request, app, service
    ):
        """`Property.infrastructure_extended` falls back to the mirrored
        `legacy_land` section only while there is no top-level one, so a naive
        first write would drop every Google-derived key with it."""
        mock_request.return_value = _amenities("bank")
        property_id = _property(
            app,
            "prop_osm_legacy",
            enrichment={
                "legacy_land": {
                    "infrastructure_extended": {
                        "supermarket_available": True,
                        "supermarket_distance": 800,
                        "osm_amenities": {"school": 4},
                    }
                }
            },
        )

        with app.app_context():
            prop = db.session.get(Property, property_id)
            service.enrich_osm_amenities(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).infrastructure_extended

        assert section["supermarket_available"] is True
        assert section["supermarket_distance"] == 800
        # The fresh measurement replaces the mirrored one.
        assert section["osm_amenities"] == {"bank": 1}
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_OK


class TestNoCoordinatesIsNotNothingNearby:
    @patch("services.enrichment_service.request_with_retries")
    def test_a_property_without_coordinates_is_recorded_as_not_asked(
        self, mock_request, app, service
    ):
        property_id = _property(app, "prop_osm_nocoords", lat=None, lon=None)

        with app.app_context():
            prop = db.session.get(Property, property_id)
            failure = service.enrich_osm_amenities(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).enrichment[
                "infrastructure_extended"
            ]

        # Geocoding is a paid Google call; counting cafés is not a reason to
        # spend one.
        mock_request.assert_not_called()
        assert failure is not None
        assert failure.reason == OSM_REASON_NO_COORDINATES
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert section[OSM_STATUS_KEY]["reason"] == OSM_REASON_NO_COORDINATES
        assert "osm_amenities" not in section

    @pytest.mark.parametrize(
        "lat, lon",
        [
            (float("nan"), -0.3763),
            (91.0, -0.3763),
            (39.4699, 181.0),
        ],
    )
    @patch("services.enrichment_service.request_with_retries")
    def test_a_coordinate_off_the_globe_is_a_gap_not_a_query(
        self, mock_request, app, service, lat, lon
    ):
        """Bad input is not a location.

        Asking Overpass about latitude 91 comes back with nothing nearby, and
        that absence would then be filed as the measured fact -- the shape of
        #98 again. `commit=False` because the column's own constraint would
        reject these values, which is a second net, not this one.
        """
        property_id = _property(app, f"prop_osm_offglobe_{lat}_{lon}")

        with app.app_context():
            prop = db.session.get(Property, property_id)
            prop.location_lat = lat
            prop.location_lon = lon
            failure = service.enrich_osm_amenities(prop, commit=False)

            section = prop.infrastructure_extended
            db.session.rollback()

        mock_request.assert_not_called()
        assert failure is not None
        assert failure.reason == OSM_REASON_NO_COORDINATES
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert "osm_amenities" not in section

    @patch("services.enrichment_service.request_with_retries")
    def test_a_listing_that_cannot_be_geocoded_records_the_gap_too(
        self, mock_request, app
    ):
        """Through the orchestrator, not the helper.

        `enrich_property` gives up as soon as geocoding fails to place the
        listing, and that early return used to leave the amenity section absent
        -- which is the very reading #152 is about. The independent review of
        this change found it there rather than in the helper, so the test lives
        at the same level as the defect.
        """
        property_id = _property(app, "prop_osm_ungeocodable", lat=None, lon=None)

        class _FailedGeocoding:
            def ensure_coordinates(self, prop, refresh=False):
                return False

        with app.app_context():
            prop = db.session.get(Property, property_id)
            ok = _enrichment_service_under_test(
                location_service=_FailedGeocoding()
            ).enrich_property(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).enrichment[
                "infrastructure_extended"
            ]

        assert ok is False
        mock_request.assert_not_called()
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
        assert section[OSM_STATUS_KEY]["reason"] == OSM_REASON_NO_COORDINATES

    @patch("services.enrichment_service.request_with_retries")
    def test_a_listing_at_zero_zero_runs_the_whole_pass(self, mock_request, app):
        """`0` is falsy in Python and a coordinate in the Gulf of Guinea.

        The amenity counts alone do not prove this: under the old truthiness
        check the property took the *gap* branch, which measures amenities too
        (`_osm_coordinate` accepts 0) and then returns, silently skipping sea
        distance, travel and scoring. The second review round caught exactly
        that, so the return value and the later steps are what is asserted.
        """
        mock_request.return_value = _amenities("cafe")
        property_id = _property(app, "prop_osm_zero", lat="0", lon="0")
        scored = []

        class _RecordingScoring:
            def calculate_for_property(self, prop, commit=False):
                scored.append(prop.id)
                return True

        with app.app_context():
            prop = db.session.get(Property, property_id)
            ok = _enrichment_service_under_test(
                scoring_service=_RecordingScoring()
            ).enrich_property(prop)

            db.session.expire_all()
            section = db.session.get(Property, property_id).infrastructure_extended

        assert ok is True
        assert scored == [property_id]
        assert section["osm_amenities"] == {"cafe": 1}
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_OK


class TestPacing:
    """overpass-api.de grants two query slots per IP, for the whole process."""

    def test_a_429_is_waited_out_rather_than_recorded(self, app, service):
        """Measured on 2026-08-09: a paced 20-property dry run drew 15 `429`s
        and 8 `504`s for 20 answers. "Too many requests" is not an answer about
        what is nearby -- the server wants a slower rate, and the real answer
        arrives on retry. The real transport runs here, only its sleep is
        patched: a caller that narrowed `retryable_statuses` to the 504 case
        would file the 429 as a measured absence, which is #98 again.
        """
        property_id = _property(app, "prop_osm_429")

        with app.app_context():
            with (
                patch("utils.http.time.sleep", return_value=None),
                patch(
                    "services.enrichment_service.requests.post",
                    side_effect=[_response(status_code=429), _amenities("bank")],
                ) as mock_post,
            ):
                prop = db.session.get(Property, property_id)
                failure = service.enrich_osm_amenities(prop)

            section = db.session.get(Property, property_id).infrastructure_extended

        assert mock_post.call_count == 2
        assert failure is None
        assert section["osm_amenities"] == {"bank": 1}
        assert section[OSM_STATUS_KEY]["state"] == OSM_STATE_OK

    def test_the_interval_is_the_measured_one(self):
        """A guessed 2 s drew more refusals than answers; do not walk it back
        without measuring again -- see docs/STATE.md. The gate object itself
        carries 0 here, because tests/__init__.py disables pacing for a suite
        that never reaches the live instance."""
        assert OVERPASS_MIN_INTERVAL_S >= 5.0

    def test_both_overpass_callers_share_one_gate(self):
        from services import enrichment_service, sea_view_service

        assert enrichment_service.OVERPASS_GATE is OVERPASS_GATE
        assert sea_view_service.OVERPASS_GATE is OVERPASS_GATE

    def test_the_gate_spaces_consecutive_calls(self):
        clock = {"now": 1000.0}
        slept = []

        def _sleep(seconds):
            slept.append(seconds)
            clock["now"] += seconds

        gate = RateGate(2.0, name="test")
        with (
            patch("utils.http.time.monotonic", lambda: clock["now"]),
            patch("utils.http.time.sleep", _sleep),
        ):
            gate.wait()  # first call: the gate has never fired, so no wait
            gate.mark()
            clock["now"] += 0.25  # a fast request
            gate.wait()

        assert slept == [pytest.approx(1.75)]

    def test_concurrent_callers_are_spaced_rather_than_bunched(self):
        """Two threads arriving together must not both fire immediately.

        This is the case a single-threaded test cannot see, and the reason the
        slot is reserved under the lock: measuring the gap and then acting on
        it lets both callers read the same gap and pass.
        """
        gate = RateGate(0.15, name="threaded")
        gate.wait()  # the first slot is taken; both threads below must queue
        starts = []
        barrier = threading.Barrier(2)

        def _caller():
            barrier.wait()
            gate.wait()
            starts.append(time.monotonic())

        threads = [threading.Thread(target=_caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(starts) == 2
        assert abs(starts[1] - starts[0]) >= 0.15 * 0.8, starts

    def test_a_retry_waits_for_the_slot_the_gate_gives_it(self):
        """The backoff and the gate together, on a fake clock.

        They are not redundant: the backoff is what this server just asked for,
        the gate is what the process allows itself across every caller. A
        backoff already past the next slot must cost nothing extra, and a short
        one must not let the retry jump the queue.
        """
        from utils.http import request_with_retries

        clock = {"now": 100.0}
        starts = []

        def _sleep(seconds):
            clock["now"] += seconds

        def _post(*_args, **_kwargs):
            starts.append(clock["now"])
            return _response(status_code=504 if len(starts) == 1 else 200)

        gate = RateGate(30.0, name="slow")
        with (
            patch("utils.http.time.monotonic", lambda: clock["now"]),
            patch("utils.http.time.sleep", _sleep),
        ):
            request_with_retries(
                _post, "https://example.invalid", max_attempts=2, gate=gate
            )

        # First attempt at once, the retry no sooner than the interval -- the
        # 0.5 s default backoff alone would have fired it almost immediately.
        assert starts[0] == 100.0
        assert starts[1] - starts[0] >= 30.0

    def test_an_unexpected_exception_still_ends_the_attempt(self):
        """`mark()` on every exit, not only on `RequestException`.

        `request_fn` is an arbitrary callable — a session adapter, a hook or a
        test transport can raise anything. Naming one exception type left the
        gate waited-but-never-marked, so the next slot was measured from the
        *start* of a call that had run for ten seconds. Found by the
        independent review of this change.
        """
        from utils.http import request_with_retries

        clock = {"now": 100.0}
        events = []

        class _TracingGate(RateGate):
            def wait(self):
                events.append(("wait", clock["now"]))
                return 0.0

            def mark(self):
                events.append(("mark", clock["now"]))

        def _explode(*_args, **_kwargs):
            clock["now"] += 10.0  # a call that ran, then failed oddly
            raise RuntimeError("adapter blew up")

        gate = _TracingGate(5.0, name="tracing")
        with patch("utils.http.time.monotonic", lambda: clock["now"]):
            with pytest.raises(RuntimeError):
                request_with_retries(_explode, "https://example.invalid", gate=gate)

        assert events == [("wait", 100.0), ("mark", 110.0)]

    def test_a_finished_call_does_not_block_behind_another_callers_wait(self):
        """`mark()` must not wait out somebody else's pacing interval.

        Holding the lock across the sleep made a returning request block for a
        whole interval before it could record that it was done -- found by the
        independent review, with 0.205s measured on a 0.20s interval.
        """
        gate = RateGate(1.0, name="mark-not-blocked")
        gate.wait()

        waiter = threading.Thread(target=gate.wait)
        waiter.start()
        try:
            time.sleep(0.05)  # let the waiter reach its sleep
            started = time.monotonic()
            gate.mark()
            blocked_for = time.monotonic() - started
        finally:
            waiter.join(timeout=5)

        assert blocked_for < 0.2, blocked_for

    @patch("services.enrichment_service.request_with_retries")
    def test_the_amenity_lookup_hands_the_transport_the_gate(
        self, mock_request, app, service
    ):
        """Pacing belongs to the transport, so the retries are paced too.

        The lookup used to take the gate itself and then hand the retry loop a
        free hand -- which paced the lookups and left the bursts unpaced.
        """
        mock_request.return_value = _amenities("cafe")
        property_id = _property(app, "prop_osm_gate")

        with app.app_context():
            service.enrich_osm_amenities(db.session.get(Property, property_id))

        assert mock_request.call_args.kwargs["gate"] is OVERPASS_GATE

    def test_every_attempt_takes_the_gate_not_just_the_first(self, app, service):
        """The real transport, with a refusal in front of the answer.

        Measured during the #152 backfill: at 5 s between lookups the run still
        drew more 429s than 504s, because each refusal was answered by a burst
        the gate never saw. One `wait` per attempt is the fix.
        """
        calls = []

        class _CountingGate(RateGate):
            def wait(self):
                calls.append("wait")
                return 0.0

            def mark(self):
                calls.append("mark")

        gate = _CountingGate(0.0, name="counting")
        property_id = _property(app, "prop_osm_gate_retry")

        with app.app_context():
            with (
                patch("utils.http.time.sleep", return_value=None),
                patch("services.enrichment_service.OVERPASS_GATE", gate),
                patch(
                    "services.enrichment_service.requests.post",
                    side_effect=[_response(status_code=504), _amenities("cafe")],
                ),
            ):
                service.enrich_osm_amenities(db.session.get(Property, property_id))

            section = db.session.get(Property, property_id).infrastructure_extended

        assert calls == ["wait", "mark", "wait", "mark"]
        assert section["osm_amenities"] == {"cafe": 1}

    @patch("services.enrichment_service.request_with_retries")
    def test_a_second_property_at_the_same_point_asks_nothing(
        self, mock_request, app, service
    ):
        """The cache is what keeps a backfill off the network for rows that
        share a location; the gate paces the rest."""
        mock_request.return_value = _amenities("cafe")
        first = _property(app, "prop_osm_cached_1")
        second = _property(app, "prop_osm_cached_2")

        with app.app_context():
            service.enrich_osm_amenities(db.session.get(Property, first))
            service.enrich_osm_amenities(db.session.get(Property, second))

            section = db.session.get(Property, second).infrastructure_extended

        assert mock_request.call_count == 1
        assert section["osm_amenities"] == {"cafe": 1}


class TestTheBackfillStaysFree:
    """Google enrichment is out of scope for #152: it costs money."""

    def test_it_calls_only_the_amenity_lookup(self, app):
        from utils import backfill_osm_amenities

        class _Recorder:
            def __init__(self):
                self.seen = []

            def enrich_osm_amenities(self, prop, *, commit=True):
                self.seen.append((prop.id, commit))
                return None

            def __getattr__(self, name):  # any other call is a bug, loudly
                raise AssertionError(f"backfill reached {name}(), which is not free")

        property_id = _property(app, "prop_osm_backfill")
        recorder = _Recorder()

        with app.app_context():
            outcome = backfill_osm_amenities.backfill(
                [db.session.get(Property, property_id)], recorder
            )

        assert recorder.seen == [(property_id, True)]
        assert outcome["measured"] == 1

    @patch("services.enrichment_service.request_with_retries")
    def test_the_real_service_reaches_overpass_and_nothing_else(
        self, mock_request, app
    ):
        """The recorder above cannot see paid work added *inside* the amenity
        call, so this one runs the real `EnrichmentService` and watches where
        the requests go."""
        from config import Config
        from utils import backfill_osm_amenities

        mock_request.return_value = _amenities("cafe")
        property_id = _property(app, "prop_osm_backfill_real")
        service = EnrichmentService()

        with app.app_context():
            with (
                patch.object(
                    EnrichmentService,
                    "_enrich_with_google_places",
                    side_effect=AssertionError("paid Places call"),
                ),
                patch.object(
                    EnrichmentService,
                    "_enrich_with_google_maps",
                    side_effect=AssertionError("paid Distance Matrix call"),
                ),
                patch.object(
                    service.geocoding_service,
                    "geocode_address",
                    side_effect=AssertionError("paid Geocoding call"),
                ),
            ):
                outcome = backfill_osm_amenities.backfill(
                    [db.session.get(Property, property_id)], service
                )

        assert outcome["measured"] == 1
        urls = [call.args[1] for call in mock_request.call_args_list]
        assert urls == [Config.OSM_OVERPASS_URL]

    def test_only_missing_skips_a_property_that_already_answered(self, app):
        from utils import backfill_osm_amenities

        class _Recorder:
            def __init__(self):
                self.seen = []

            def enrich_osm_amenities(self, prop, *, commit=True):
                self.seen.append(prop.id)
                return None

        answered = _property(
            app,
            "prop_osm_answered",
            enrichment={
                "infrastructure_extended": {
                    "osm_amenities": {"cafe": 1},
                    OSM_STATUS_KEY: {"state": OSM_STATE_OK},
                }
            },
        )
        refused = _property(
            app,
            "prop_osm_refused",
            enrichment={
                "infrastructure_extended": {
                    OSM_STATUS_KEY: {"state": OSM_STATE_UNAVAILABLE}
                }
            },
        )
        recorder = _Recorder()

        with app.app_context():
            rows = [
                db.session.get(Property, answered),
                db.session.get(Property, refused),
            ]
            outcome = backfill_osm_amenities.backfill(rows, recorder, only_missing=True)

        # A refusal is retried, an answer is not.
        assert recorder.seen == [refused]
        assert outcome["skipped"] == 1

    def test_a_dry_run_does_not_ask_the_service_to_commit(self, app):
        """`--dry-run` is a rehearsal of the pacing and the refusals, not of
        the write; a dry run that commits is worse than no dry run."""
        from utils import backfill_osm_amenities

        commits = []

        class _Recorder:
            def enrich_osm_amenities(self, prop, *, commit=True):
                commits.append(commit)
                return None

        property_id = _property(app, "prop_osm_dry_run")

        with app.app_context():
            backfill_osm_amenities.backfill(
                [db.session.get(Property, property_id)], _Recorder(), dry_run=True
            )

        assert commits == [False]

    def test_it_paces_between_properties(self, app):
        from utils import backfill_osm_amenities

        class _Quiet:
            def enrich_osm_amenities(self, prop, *, commit=True):
                return None

        slept = []
        first = _property(app, "prop_osm_pace_1")
        second = _property(app, "prop_osm_pace_2")

        with app.app_context():
            backfill_osm_amenities.backfill(
                [
                    db.session.get(Property, first),
                    db.session.get(Property, second),
                ],
                _Quiet(),
                sleep_s=1.5,
                sleep=slept.append,
            )

        assert slept == [1.5, 1.5]
