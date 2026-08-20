"""Drive times from a routing engine on this machine, or an honest refusal.

Step 3 of the cost plan: with the places on OpenStreetMap, the Distance Matrix
leg is the only billed call an enrichment still makes. OSRM answers the same
question from the same map for nothing -- but its answers are not identical,
and the tests exist to keep that from being discovered later.

Measured against 30 target pairs already in the database, precise origins, all
previously answered by Google: the **distances agree** (49.0 vs 49.3 km, 75.4
vs 77.2), the median duration difference is **-1.3%**, and the outliers have
structure -- under five minutes Google rounds to whole minutes and OSRM does
not, and on motorway runs of 30-75 km **OSRM is 26-34% slower**.

So the feature is opt-in, and what these tests pin is the three things that
must hold whether or not it is switched on: it is off unless configured, a
routing engine that cannot be reached refuses rather than falling back to the
paid API, and a mode the extract has no profile for is refused rather than
answered by the car.
"""

import pytest
import requests

from config import Config
from services import osrm_routing
from services.osrm_routing import REASON_MODE_UNSUPPORTED, RouteLeg

ORIGIN = (43.3561, -5.8763)
AIRPORT = (43.5636, -6.0348)
STATION = (43.3571, -5.8771)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _ok_payload(rows_duration, rows_distance):
    return {
        "code": "Ok",
        "durations": [rows_duration],
        "distances": [rows_distance],
    }


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(Config, "OSRM_URL", "http://osrm.test:5000")
    yield


class TestItIsOffUnlessConfigured:
    def test_no_url_means_disabled(self, monkeypatch):
        monkeypatch.setattr(Config, "OSRM_URL", "")
        assert osrm_routing.is_enabled() is False

    def test_the_shipped_default_is_off(self):
        """Turning it on decides what the stored numbers mean, so it is a
        deployment's decision and never a default that arrives with a deploy.

        Read from a clean interpreter, and **not** `importlib.reload(config)`:
        reloading rebinds `config.Config` to a new class while every
        `from config import Config` already executed in this session keeps the
        old one, so a later test's `monkeypatch.setattr(Config, ...)` patches
        an object the services no longer read. The repository documents that
        in `tests/test_paid_google_is_on_request.py`, and the first version of
        this test walked into it anyway -- seven unrelated tests in three other
        files went red, none of them mentioning OSRM.
        """
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items()}
        env.pop("OSRM_URL", None)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config import Config;print(repr(Config.OSRM_URL))",
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "''"

    def test_a_configured_url_enables_it(self, configured):
        assert osrm_routing.is_enabled() is True


class TestItAnswersTheBatch:
    def test_one_request_serves_every_destination(self, configured, monkeypatch):
        calls = []

        def _get(fn, url, **kwargs):
            calls.append(url)
            return _Response(_ok_payload([0, 2880, 300], [0, 51700, 800]))

        monkeypatch.setattr(osrm_routing, "request_with_retries", _get)

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT, STATION])

        assert failure is None
        assert len(calls) == 1, "the whole batch is one round trip"
        assert legs == [
            RouteLeg(distance_m=51700, duration_s=2880),
            RouteLeg(distance_m=800, duration_s=300),
        ]

    def test_the_origin_is_not_returned_as_a_destination(self, configured, monkeypatch):
        """Index 0 of the row is the origin to itself; the answers follow it."""
        monkeypatch.setattr(
            osrm_routing,
            "request_with_retries",
            lambda fn, url, **kw: _Response(_ok_payload([0, 2880], [0, 51700])),
        )

        legs, _ = osrm_routing.table(ORIGIN, [AIRPORT])

        assert len(legs) == 1
        assert legs[0].duration_s == 2880

    def test_coordinates_go_out_as_lon_lat(self, configured, monkeypatch):
        """OSRM's order is the opposite of everything else in this repository,
        and swapping them silently answers about a different place."""
        seen = {}

        def _get(fn, url, **kwargs):
            seen["url"] = url
            return _Response(_ok_payload([0, 60], [0, 100]))

        monkeypatch.setattr(osrm_routing, "request_with_retries", _get)
        osrm_routing.table(ORIGIN, [AIRPORT])

        assert "-5.8763,43.3561;-6.0348,43.5636" in seen["url"]

    def test_no_route_is_a_measurement_not_a_failure(self, configured, monkeypatch):
        """An island or a pedestrian-only address: the engine answered."""
        monkeypatch.setattr(
            osrm_routing,
            "request_with_retries",
            lambda fn, url, **kw: _Response(_ok_payload([0, None], [0, None])),
        )

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT])

        assert failure is None
        assert legs == [RouteLeg()]


class TestARefusalIsARefusal:
    def test_an_unreachable_engine_refuses(self, configured, monkeypatch):
        def _boom(fn, url, **kwargs):
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(osrm_routing, "request_with_retries", _boom)

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT])

        assert legs is None
        assert failure is not None

    def test_a_bad_status_refuses(self, configured, monkeypatch):
        monkeypatch.setattr(
            osrm_routing,
            "request_with_retries",
            lambda fn, url, **kw: _Response({}, status_code=503),
        )

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT])

        assert legs is None
        assert failure is not None

    def test_a_non_ok_body_refuses(self, configured, monkeypatch):
        monkeypatch.setattr(
            osrm_routing,
            "request_with_retries",
            lambda fn, url, **kw: _Response({"code": "NoSegment"}),
        )

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT])

        assert legs is None
        assert failure is not None


class TestAModeItWasNotBuiltForIsRefused:
    @pytest.mark.parametrize("mode", ["walking", "bicycling", "transit"])
    def test_only_driving_is_answered(self, configured, monkeypatch, mode):
        """The extract carries `car.lua` alone. Answering a walk with a drive
        is a wrong number wearing a right number's clothes."""

        def _forbidden(fn, url, **kwargs):
            raise AssertionError("a non-driving mode reached the car profile")

        monkeypatch.setattr(osrm_routing, "request_with_retries", _forbidden)

        legs, failure = osrm_routing.table(ORIGIN, [AIRPORT], mode=mode)

        assert legs is None
        assert failure.reason == REASON_MODE_UNSUPPORTED


class TestTheTravelServiceUsesIt:
    def test_it_replaces_the_billed_call_and_never_falls_back_to_it(
        self, configured, monkeypatch
    ):
        from services.property_travel_service import PropertyTravelService

        import services.property_travel_service as travel_module

        # Recorded, not raised. `_distance_matrix_batch` wraps its request in
        # `except Exception` and turns anything thrown into a tidy failure, so
        # a stub that raises is answered by the very code it is meant to catch
        # -- the assertion has to happen in the test (#307's lesson, and the
        # third time it has come up tonight).
        billed_calls = []

        def _record_google(fn, url, **kwargs):
            billed_calls.append(url)
            raise AssertionError("Distance Matrix was called while OSRM was on")

        monkeypatch.setattr(travel_module, "request_with_retries", _record_google)
        monkeypatch.setattr(
            osrm_routing,
            "table",
            lambda origin, destinations, mode="driving": (
                None,
                __import__(
                    "utils.google_api", fromlist=["GoogleApiFailure"]
                ).GoogleApiFailure(reason="network_error"),
            ),
        )

        service = PropertyTravelService(
            google_maps_key="key-that-must-not-be-used",
            google_places_key="key-that-must-not-be-used",
        )
        results = service._distance_matrix_batch(
            ORIGIN[0], ORIGIN[1], ["43.5636,-6.0348"], mode="driving"
        )

        assert billed_calls == [], "a refusal must not fall through to the paid API"
        assert len(results) == 1
        assert results[0].failure is not None
        assert results[0].duration_s is None

    def test_a_measured_leg_reaches_the_caller(self, configured, monkeypatch):
        from services.property_travel_service import PropertyTravelService

        monkeypatch.setattr(
            osrm_routing,
            "table",
            lambda origin, destinations, mode="driving": (
                [RouteLeg(distance_m=51700, duration_s=2880)],
                None,
            ),
        )

        service = PropertyTravelService()
        results = service._distance_matrix_batch(
            ORIGIN[0], ORIGIN[1], ["43.5636,-6.0348"], mode="driving"
        )

        assert results[0].duration_s == 2880
        assert results[0].distance_m == 51700
