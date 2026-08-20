"""One Enrich press is bounded, and the paid half of it does not wait (#434).

Measured on the mini 2026-08-20, property 793. The owner pressed **Enrich**,
saw nothing, pressed three more times, and sixteen minutes later the travel
block filled in. The AI analysis never ran at all.

Four defects produced that, and the class is worth naming before the examples:
**an advisory, score-neutral step is allowed to hold a paid, decisive one
hostage, and nothing bounds how long.**

* One Overpass lookup cost **888 s of pure waiting** -- three instances, four
  attempts each at a scalar 60 s timeout, 8+16+32 s of backoff per host, and
  not one request completing. The step spending it was `PoolService.enrich`,
  whose criterion ships at weight 0.
* The run's only commit was its last line, so a Distance Matrix request
  **billed at 12:59** sat in an uncommitted session until 13:12:55, behind
  that step.
* `property_enrich` was the one enqueue site with no `dedupe_key`, so four
  presses queued four identical runs and two of them still held two of the
  four `BACKGROUND_WORKERS` half an hour later.
* The AI step lived only in a browser promise chain, so every re-press
  reloaded the page and killed the previous chain before it reached it.

What the arithmetic tests below do *not* do is measure wall-clock time. They
run a virtual clock: every sleep and every timeout advances it, so the
assertion is the arithmetic itself -- 888 s against a stated ceiling -- and
not "it felt faster on this machine".
"""

import types

import pytest
import requests

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from models import Property  # noqa: E402
from services.enrichment_service import EnrichmentService  # noqa: E402
from utils.google_api import REASON_BUDGET_EXHAUSTED, REASON_NETWORK_ERROR  # noqa: E402
from utils.http import (  # noqa: E402
    OVERPASS_GATE,
    LookupBudgetExceeded,
    lookup_budget,
    request_with_retries,
)


class Clock:
    """A monotonic clock that only moves when something waits on it.

    Handed to `utils.http` and `services.enrichment_service` in place of the
    `time` module, so a retry policy's sleeps, a gate's pacing and a transport
    that sits on a dead socket all advance the same counter. What the tests
    then assert is the sum -- the thing the ticket is about.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.start = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    @property
    def elapsed(self) -> float:
        return self.now - self.start

    def as_module(self):
        return types.SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep)


@pytest.fixture
def clock(monkeypatch):
    import services.enrichment_service as enrichment_module
    import utils.http as http_module

    c = Clock()
    monkeypatch.setattr(http_module, "time", c.as_module())
    monkeypatch.setattr(enrichment_module, "time", c.as_module())
    # The suite neutralises the shared gate at import (tests/__init__.py) so
    # nothing pays 5 s per lookup. These tests want the deployment's own
    # pacing in the total, because it is part of what an outage costs.
    previous_interval = OVERPASS_GATE.min_interval_s
    previous_slot = OVERPASS_GATE._next_slot_at
    OVERPASS_GATE.min_interval_s = 5.0
    OVERPASS_GATE._next_slot_at = 0.0
    yield c
    OVERPASS_GATE.min_interval_s = previous_interval
    OVERPASS_GATE._next_slot_at = previous_slot


def _blackholed_connect(clock):
    """A host that swallows the SYN: the connect leg runs out in full."""

    def _call(*args, **kwargs):
        connect, _read = kwargs["timeout"]
        clock.sleep(connect)
        raise requests.ConnectTimeout("connect timed out")

    return _call


def _hung_after_handshake(clock):
    """kumi.systems on the day: connect in 0.109 s, then nothing for 30 s."""

    def _call(*args, **kwargs):
        _connect, read = kwargs["timeout"]
        clock.sleep(read)
        raise requests.ReadTimeout("read timed out")

    return _call


def _busy(clock, status=504):
    """An instance that *answers*: both per-IP slots are busy (#144)."""

    def _call(*args, **kwargs):
        clock.sleep(0.2)
        return types.SimpleNamespace(
            status_code=status, close=lambda: None, json=lambda: {}
        )

    return _call


class TestSilenceIsNotBusy:
    """The retry policy splits by what the failure *means*."""

    def test_a_silent_host_gets_one_attempt_when_the_caller_says_so(self, clock):
        calls = []

        def _call(*args, **kwargs):
            calls.append(kwargs["timeout"])
            clock.sleep(kwargs["timeout"][0])
            raise requests.ConnectTimeout("connect timed out")

        with pytest.raises(requests.ConnectTimeout):
            request_with_retries(
                _call,
                "https://overpass.example/api",
                max_attempts=4,
                backoff_base=8.0,
                timeout=(5.0, 60.0),
                silence_max_attempts=1,
            )

        assert len(calls) == 1, "a host that says nothing was retried anyway"
        # One attempt at the connect leg, and not a second of backoff: 8+16+32
        # was measured against a server that spoke.
        assert clock.elapsed == pytest.approx(5.0)

    def test_a_read_that_never_arrives_is_silence_too(self, clock):
        """The instance that cost the most was not a connect failure.

        Probed from the mini on 2026-08-20: overpass-api.de refused the
        connection outright, but kumi.systems *connected* in 0.109 s and then
        said nothing for 30 s. A connect-only rule would have walked straight
        past the expensive one.
        """
        calls = []

        def _call(*args, **kwargs):
            calls.append(1)
            clock.sleep(kwargs["timeout"][1])
            raise requests.ReadTimeout("read timed out")

        with pytest.raises(requests.ReadTimeout):
            request_with_retries(
                _call,
                "https://overpass.example/api",
                max_attempts=4,
                backoff_base=8.0,
                timeout=(5.0, 60.0),
                silence_max_attempts=1,
            )

        assert len(calls) == 1
        assert clock.elapsed == pytest.approx(60.0)

    def test_a_server_that_answers_504_keeps_its_patient_budget(self, clock):
        """The #144 control. A busy instance frees a slot in about a minute,
        and abandoning it after one attempt is the defect this fix must not
        introduce."""
        calls = []

        def _call(*args, **kwargs):
            calls.append(1)
            clock.sleep(0.2)
            return types.SimpleNamespace(
                status_code=504, close=lambda: None, json=lambda: {}
            )

        response = request_with_retries(
            _call,
            "https://overpass.example/api",
            max_attempts=4,
            backoff_base=8.0,
            backoff_max=90.0,
            timeout=(5.0, 60.0),
            silence_max_attempts=1,
        )

        assert response.status_code == 504
        assert len(calls) == 4, "a busy instance was abandoned early"
        # 8 + 16 + 32 of backoff, plus the jitter each one carries.
        assert 56.0 <= clock.elapsed <= 57.5

    def test_silence_left_unset_keeps_every_attempt(self, clock):
        """A caller with no second instance to go to still wants them."""
        calls = []

        def _call(*args, **kwargs):
            calls.append(1)
            clock.sleep(1.0)
            raise requests.ConnectionError("refused")

        with pytest.raises(requests.ConnectionError):
            request_with_retries(
                _call,
                "https://example.invalid",
                max_attempts=3,
                backoff_base=0.5,
                timeout=(5.0, 60.0),
            )
        assert len(calls) == 3


class TestTheBudgetIsAWallClockCeiling:
    def test_a_spent_budget_costs_no_socket_at_all(self, clock):
        calls = []

        def _call(*args, **kwargs):
            calls.append(1)
            return types.SimpleNamespace(status_code=200, close=lambda: None)

        with pytest.raises(LookupBudgetExceeded):
            request_with_retries(
                _call,
                "https://overpass.example/api",
                timeout=(5.0, 60.0),
                deadline=clock.monotonic() - 1.0,
            )
        assert calls == [], "a request went out after the budget was gone"

    def test_the_read_leg_is_clamped_to_what_is_left(self, clock):
        seen = []

        def _call(*args, **kwargs):
            seen.append(kwargs["timeout"])
            clock.sleep(kwargs["timeout"][1])
            raise requests.ReadTimeout("read timed out")

        with pytest.raises(requests.ReadTimeout):
            request_with_retries(
                _call,
                "https://overpass.example/api",
                max_attempts=1,
                timeout=(5.0, 60.0),
                deadline=clock.monotonic() + 12.0,
            )
        assert seen == [(5.0, 12.0)]
        assert clock.elapsed == pytest.approx(12.0)

    def test_it_bounds_the_next_attempt_and_not_a_dripping_body(self):
        """The boundary of the guarantee, measured rather than claimed.

        `requests` applies its read timeout *between* reads, so a server that
        sends a byte just often enough holds one attempt open past the
        deadline. This runs a real loopback server to record that -- and to
        record what the deadline does still guarantee: the *next* attempt does
        not start. If someone later reads the docstring as a total-time bound,
        this is the line that disagrees.
        """
        import http.server
        import socketserver
        import threading
        import time as real_time

        class _Drip(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = b'{"elements": [' + b" " * 6 + b"]}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                # A byte at a time, slowly enough that the whole response
                # outlives the deadline and quickly enough that no single read
                # ever hits the inactivity timeout.
                for index in range(len(body)):
                    real_time.sleep(0.03)
                    self.wfile.write(body[index : index + 1])
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), _Drip)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            started = real_time.monotonic()
            response = request_with_retries(
                requests.post,
                url,
                data="x",
                max_attempts=1,
                timeout=(3.0, 60.0),
                silence_max_attempts=1,
                deadline=real_time.monotonic() + 0.1,
            )
            elapsed = real_time.monotonic() - started
            assert response.status_code == 200
            assert elapsed > 0.1, "the drip did not outlive the deadline"

            # What it does guarantee: nothing else starts.
            with pytest.raises(LookupBudgetExceeded):
                request_with_retries(
                    requests.post,
                    url,
                    data="x",
                    max_attempts=1,
                    timeout=(3.0, 60.0),
                    deadline=real_time.monotonic() - 0.001,
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_a_nested_budget_may_shorten_a_run_but_never_extend_it(self, clock):
        with lookup_budget(30.0) as outer:
            with lookup_budget(300.0) as inner:
                assert inner == outer
            with lookup_budget(10.0) as shorter:
                assert shorter < outer


class TestTheWalkAcrossInstancesHasACeiling:
    """The headline arithmetic: 888 s, against a stated ceiling."""

    @pytest.fixture
    def service(self, monkeypatch):
        monkeypatch.setattr(
            Config,
            "OSM_OVERPASS_FALLBACK_URLS",
            [
                "https://overpass.kumi.systems/api/interpreter",
                "https://overpass.private.coffee/api/interpreter",
            ],
        )
        svc = EnrichmentService.__new__(EnrichmentService)
        svc.osm_overpass_url = "https://overpass-api.de/api/interpreter"
        return svc

    def test_three_blackholed_instances_cost_seconds(self, service, clock, monkeypatch):
        """What #434 measured as 888 s.

        Before: three instances x four attempts x a scalar 60 s timeout, plus
        8+16+32 s of backoff on each = 888 s, none of it a completed request.
        After: one attempt per instance at the connect leg, plus the shared
        5 s gate = 30 s.
        """
        import services.enrichment_service as enrichment_module

        tried = []

        def _call(*args, **kwargs):
            tried.append(args[0] if args else kwargs.get("url"))
            connect, _read = kwargs["timeout"]
            clock.sleep(connect)
            raise requests.ConnectTimeout("connect timed out")

        monkeypatch.setattr(
            enrichment_module,
            "request_with_retries",
            lambda fn, *a, **kw: request_with_retries(_call, *a, **kw),
        )

        elements, failure = service._overpass_elements("[out:json];out;")

        assert elements is None
        assert len(tried) == 3, "the walk stopped short of an instance"
        # 3 x (5 s gate + 5 s connect). The `<= 40` leaves room for the gate's
        # own arithmetic without letting a minute back in.
        assert clock.elapsed <= 40.0, f"the walk took {clock.elapsed:.0f}s"
        assert failure.reason == REASON_NETWORK_ERROR

    def test_three_hung_instances_do_not_exceed_the_stated_ceiling(
        self, service, clock, monkeypatch
    ):
        """The expensive shape: each host completes its handshake and then
        says nothing, so there is no connect timeout to save the walk. The
        ceiling is what stops it."""
        import services.enrichment_service as enrichment_module

        monkeypatch.setattr(Config, "OSM_OVERPASS_WALK_BUDGET_S", 120.0)
        call = _hung_after_handshake(clock)
        monkeypatch.setattr(
            enrichment_module,
            "request_with_retries",
            lambda fn, *a, **kw: request_with_retries(call, *a, **kw),
        )

        elements, failure = service._overpass_elements("[out:json];out;")

        assert elements is None
        assert clock.elapsed <= 121.0, f"the walk took {clock.elapsed:.0f}s"
        # The failure named is the *first* one, not the budget error the last
        # instance produced: it describes the instance this deployment is
        # configured against (#415).
        assert failure.reason == REASON_NETWORK_ERROR

    def test_the_first_instance_may_still_spend_the_patient_budget(
        self, service, clock, monkeypatch
    ):
        """A busy primary must not be abandoned for a fallback it does not
        need. The ceiling is sized so #144's 8+16+32 still fits."""
        import services.enrichment_service as enrichment_module

        monkeypatch.setattr(Config, "OSM_OVERPASS_WALK_BUDGET_S", 120.0)
        calls = []
        busy = _busy(clock)

        def _call(*args, **kwargs):
            calls.append(1)
            return busy(*args, **kwargs)

        monkeypatch.setattr(
            enrichment_module,
            "request_with_retries",
            lambda fn, *a, **kw: request_with_retries(_call, *a, **kw),
        )

        service._overpass_elements("[out:json];out;")
        assert calls[:4] == [1, 1, 1, 1], "the primary was abandoned after one try"

    def test_the_whole_press_is_bounded_by_the_run_budget(
        self, service, clock, monkeypatch
    ):
        """The number the owner actually feels.

        One walk is bounded by `OSM_OVERPASS_WALK_BUDGET_S`, but an enrichment
        run makes up to eleven of them -- so what a total outage costs a press
        is this, not that. The first lookup learns the instances are down, the
        second spends what is left, and every one after it refuses before it
        opens a socket.
        """
        import services.enrichment_service as enrichment_module

        monkeypatch.setattr(Config, "OSM_OVERPASS_WALK_BUDGET_S", 210.0)
        call = _hung_after_handshake(clock)
        sockets = []

        def _call(*args, **kwargs):
            sockets.append(1)
            return call(*args, **kwargs)

        monkeypatch.setattr(
            enrichment_module,
            "request_with_retries",
            lambda fn, *a, **kw: request_with_retries(_call, *a, **kw),
        )

        with lookup_budget(240.0):
            for _ in range(11):
                elements, failure = service._overpass_elements("[out:json];out;")
                assert elements is None

        assert clock.elapsed <= 241.0, f"the press waited {clock.elapsed:.0f}s"
        # Before #434 this shape cost 888 s for *one* of those eleven lookups.
        assert clock.elapsed < 888.0
        # And the tail of the run stopped opening sockets rather than paying a
        # gate wait per instance to be told the same thing.
        assert len(sockets) <= 6, sockets

    def test_a_spent_run_budget_stops_the_walk_without_a_request(
        self, service, clock, monkeypatch
    ):
        import services.enrichment_service as enrichment_module

        sockets = []

        def _call(*args, **kwargs):
            sockets.append(1)
            return types.SimpleNamespace(status_code=200, close=lambda: None)

        monkeypatch.setattr(
            enrichment_module,
            "request_with_retries",
            lambda fn, *a, **kw: request_with_retries(_call, *a, **kw),
        )

        with lookup_budget(0.0):
            elements, failure = service._overpass_elements("[out:json];out;")

        assert elements is None
        assert sockets == [], "a request went out with no budget left"
        assert failure.reason == REASON_BUDGET_EXHAUSTED
        # And it stopped at the first instance rather than paying a gate wait
        # per remaining one to ask the same question.
        assert clock.elapsed == 0.0


class TestAnAdvisoryStepRecordsUnavailableAndTheRunGoesOn:
    def test_the_pool_step_answers_unavailable_on_a_spent_budget(self, clock):
        """`PoolService.enrich` is the step that spent the 888 s. With no
        budget left it must record a refusal -- never an absence -- and hand
        control back."""
        from services.pool_service import PoolService, STATUS_UNAVAILABLE

        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            try:
                prop = Property(
                    source_email_id="pool-budget",
                    title="Plot",
                    location_lat=43.5,
                    location_lon=-6.0,
                )
                db.session.add(prop)
                db.session.commit()

                with lookup_budget(0.0):
                    part = PoolService().enrich(prop, commit=True)

                assert part["status"] == STATUS_UNAVAILABLE
                assert part["reason"] == REASON_BUDGET_EXHAUSTED
                assert "candidates" not in part, "a refusal invented an absence"
            finally:
                db.drop_all()


class TestThePaidMeasurementIsCommittedFirst:
    """Fix 2: the run's only commit used to be its last line.

    The Distance Matrix request for property 793 was billed at 12:59 and sat
    in an uncommitted session until 13:12:55, behind a step whose criterion
    ships at weight 0. A container recreated in that window (#283) would have
    taken it.
    """

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    def test_travel_lands_before_any_advisory_step_runs(self, app, monkeypatch):
        from sqlalchemy import event

        from services import property_enrichment_service as module

        order = []

        class _Location:
            def ensure_coordinates(self, prop, refresh=False, *, commit=False):
                order.append("coordinates")
                return True

        class _Sea:
            def update_property(self, prop, *, commit=False):
                order.append("sea")
                return None

        class _Travel:
            def calculate_for_property(self, prop, commit=False):
                order.append("travel")
                return True

        class _Pool:
            def enrich(self, prop, commit=False):
                order.append("pool")
                return {}

        class _Scoring:
            def calculate_for_property(self, prop, commit=False):
                order.append("scoring")
                return True

        service = module.PropertyEnrichmentService(
            location_service=_Location(),
            travel_service=_Travel(),
            sea_distance_service=_Sea(),
            pool_service=_Pool(),
            scoring_service=_Scoring(),
        )
        monkeypatch.setattr(
            service, "enrich_free_sources", lambda p, **kw: order.append("free_sources")
        )
        monkeypatch.setattr(
            module.advertiser,
            "enrich",
            lambda prop, *, commit=False: order.append("advertiser") or {},
        )

        prop = Property(
            source_email_id="commit-order",
            title="Plot",
            location_lat=43.5,
            location_lon=-6.0,
        )
        db.session.add(prop)
        db.session.commit()

        listener = lambda session: order.append("commit")  # noqa: E731
        event.listen(db.session, "after_commit", listener)
        try:
            service.enrich_property(prop, recalc_scoring=True)
        finally:
            event.remove(db.session, "after_commit", listener)

        assert "commit" in order, order
        first_commit = order.index("commit")
        assert order.index("travel") < first_commit, order
        for advisory in ("advertiser", "free_sources", "pool"):
            assert order.index(advisory) > first_commit, (advisory, order)


class TestTheDecisiveStepsGetTheClockFirst:
    """The ordering is a budget decision as much as a commit one.

    An independent review of this branch put it exactly right: the starvation
    to worry about is not the paid call itself -- it never sees the deadline --
    but the *free* lookup it depends on. `services/osm_places.py` resolves the
    travel presets from Overpass, and no destinations means no Distance Matrix
    request. So the steps that feed a score, and the one that spends money,
    must be the ones holding the budget when it is short.
    """

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    def test_travel_holds_the_budget_and_the_advisory_steps_get_what_is_left(
        self, app, monkeypatch
    ):
        from services import property_enrichment_service as module
        from utils.http import lookup_deadline

        seen = []

        class _Location:
            def ensure_coordinates(self, prop, refresh=False, *, commit=False):
                return True

        class _Sea:
            def update_property(self, prop, *, commit=False):
                seen.append(("sea", lookup_deadline() is not None))
                return None

        class _Travel:
            def calculate_for_property(self, prop, commit=False):
                remaining = lookup_deadline()
                seen.append(("travel", remaining))
                return True

        class _Pool:
            def enrich(self, prop, commit=False):
                seen.append(("pool", lookup_deadline()))
                return {}

        service = module.PropertyEnrichmentService(
            location_service=_Location(),
            travel_service=_Travel(),
            sea_distance_service=_Sea(),
            pool_service=_Pool(),
        )
        monkeypatch.setattr(service, "enrich_free_sources", lambda p, **kw: None)
        monkeypatch.setattr(
            module.advertiser, "enrich", lambda prop, *, commit=False: {}
        )

        prop = Property(
            source_email_id="budget-order",
            title="Plot",
            location_lat=43.5,
            location_lon=-6.0,
        )
        db.session.add(prop)
        db.session.commit()

        service.enrich_property(prop, recalc_scoring=False)

        order = [name for name, _ in seen]
        assert order.index("travel") < order.index("pool"), order
        # Every step runs inside the run's budget -- and the paid one reaches
        # it before the advisory one has had a chance to spend any of it.
        assert all(value is not None for _name, value in seen), seen


class TestASecondPressJoinsTheFirst:
    """Fix 3: the one enqueue site with no `dedupe_key`."""

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            row = Property(source_email_id="press", title="Plot")
            db.session.add(row)
            db.session.commit()
            yield app
            db.drop_all()

    @pytest.fixture
    def prop_id(self, app):
        return db.session.query(Property.id).scalar()

    def test_the_async_enqueue_carries_the_key(self, app, prop_id, monkeypatch):
        seen = {}
        monkeypatch.setattr("routes.api_routes._should_run_sync", lambda *a, **k: False)
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: seen.update(kw) or "job-1",
        )
        response = app.test_client().post(f"/api/property/{prop_id}/enrich")
        assert response.status_code == 202
        assert seen["dedupe_key"] == f"property_enrich:{prop_id}"

    def test_the_key_ignores_refresh_coords(self, app, prop_id, monkeypatch):
        """Keyed on the property alone. A `refresh=True` press racing an
        ordinary one is two concurrent writers of `enrichment`, which is the
        #339 incident."""
        seen = {}
        monkeypatch.setattr("routes.api_routes._should_run_sync", lambda *a, **k: False)
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: seen.update(kw) or "job-1",
        )
        response = app.test_client().post(
            f"/api/property/{prop_id}/enrich?refresh_coords=1"
        )
        assert response.status_code == 202
        assert seen["dedupe_key"] == f"property_enrich:{prop_id}"

    def test_the_inline_path_claims_the_same_slot(self, app, prop_id, monkeypatch):
        """`?sync=1` used to call the closure directly, bypassing the registry
        and therefore the key -- so it could run alongside a live async job for
        the same property (#190 review round 3, finding 4, in a second place)."""
        seen = {}
        monkeypatch.setattr(
            "routes.api_routes._run_sync",
            lambda fn, **kw: (
                seen.update(kw) or {"result": {"success": True, "message": "ok"}}
            ),
        )
        response = app.test_client().post(f"/api/property/{prop_id}/enrich?sync=1")
        assert response.status_code == 200
        assert seen["dedupe_key"] == f"property_enrich:{prop_id}"
        assert seen["job_type"] == "property_enrich"

    def test_a_live_job_is_answered_with_its_own_id(self, app, prop_id, monkeypatch):
        from services.background_jobs import JobAlreadyActive

        def _raise(fn, **kw):
            raise JobAlreadyActive("job-live")

        monkeypatch.setattr("routes.api_routes._run_sync", _raise)
        response = app.test_client().post(f"/api/property/{prop_id}/enrich?sync=1")
        assert response.status_code == 409
        assert response.get_json()["job_id"] == "job-live"


class TestTheAnalysisDoesNotDependOnATabStayingOpen:
    """Fix 4: the sequel lived only in a browser promise chain."""

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            row = Property(source_email_id="sequel", title="Plot")
            db.session.add(row)
            db.session.commit()
            yield app
            db.drop_all()

    @pytest.fixture
    def prop_id(self, app):
        return db.session.query(Property.id).scalar()

    @pytest.fixture
    def queued(self, monkeypatch):
        seen = []

        class _Enrichment:
            def enrich_property(self, prop, **kw):
                return True

        monkeypatch.setattr(
            "services.property_enrichment_service.PropertyEnrichmentService",
            _Enrichment,
        )
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: seen.append(kw) or "job-x",
        )
        return seen

    def test_the_enrichment_job_queues_the_analyses_itself(
        self, app, prop_id, queued, monkeypatch
    ):
        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "present")
        response = app.test_client().post(
            f"/api/property/{prop_id}/enrich",
            json={"analyze": True},
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        keys = [kw.get("dedupe_key") for kw in queued]
        assert keys == [
            f"property_ai_analysis:{prop_id}:claude",
            f"property_ai_analysis:{prop_id}:openai",
        ], keys
        # The key the analysis endpoint already uses, so the page's own POST
        # attaches to these rather than paying for a second run.
        assert all(kw["job_type"] == "property_ai_analysis" for kw in queued)

    def test_chatgpt_is_skipped_when_the_bridge_is_not_configured(
        self, app, prop_id, queued, monkeypatch
    ):
        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", None)
        app.test_client().post(
            f"/api/property/{prop_id}/enrich", json={"analyze": True}
        )
        assert [kw["meta"]["provider"] for kw in queued] == ["claude"]

    def test_a_press_that_did_not_ask_for_it_queues_nothing(
        self, app, prop_id, queued, monkeypatch
    ):
        """The endpoint keeps meaning what it says for anything that only
        wants the enrichment."""
        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "present")
        app.test_client().post(f"/api/property/{prop_id}/enrich")
        assert queued == []

    def test_a_query_string_cannot_ask_for_the_spend(
        self, app, prop_id, queued, monkeypatch
    ):
        """`?analyze=1` would be reachable by a simple cross-origin form POST.

        These blueprints are CSRF-exempt and unauthenticated (owner decision
        2026-08-08); what keeps another origin out is that a form POST cannot
        set `Content-Type: application/json`. Reading the flag from the body
        only keeps the AI spend behind that, and costs the one real caller
        nothing.
        """
        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "present")
        app.test_client().post(f"/api/property/{prop_id}/enrich?analyze=1")
        assert queued == []

        # A form-encoded body is not JSON either, so it cannot carry it.
        app.test_client().post(
            f"/api/property/{prop_id}/enrich", data={"analyze": "true"}
        )
        assert queued == []

    def test_a_failed_enqueue_does_not_fail_the_enrichment(
        self, app, prop_id, monkeypatch
    ):
        """The enrichment has already run. Reporting it as failed because a
        follow-up could not be queued sends the owner to press it -- and pay
        for it -- again (#178)."""

        class _Enrichment:
            def enrich_property(self, prop, **kw):
                return True

        monkeypatch.setattr(
            "services.property_enrichment_service.PropertyEnrichmentService",
            _Enrichment,
        )
        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "present")

        def _explode(fn, **kw):
            raise RuntimeError("the queue is down")

        monkeypatch.setattr("routes.api_routes._enqueue", _explode)
        response = app.test_client().post(
            f"/api/property/{prop_id}/enrich", json={"analyze": True}
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True


class TestTheClientIsToldTheCeilingRatherThanGuessingIt:
    """Fix 4a: `JOB_POLL_TIMEOUTS.enrichment` was a client-side guess that
    predated the Overpass fallback list, which is #178's defect."""

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            row = Property(source_email_id="ceiling", title="Plot")
            db.session.add(row)
            db.session.commit()
            yield app
            db.drop_all()

    def test_the_queued_response_states_it(self, app, monkeypatch):
        from services.enrich_budget import poll_timeout_ms

        prop_id = db.session.query(Property.id).scalar()
        monkeypatch.setattr("routes.api_routes._should_run_sync", lambda *a, **k: False)
        monkeypatch.setattr("routes.api_routes._enqueue", lambda fn, **kw: "job-1")
        body = app.test_client().post(f"/api/property/{prop_id}/enrich").get_json()
        assert body["poll_timeout_ms"] == poll_timeout_ms()

    def test_it_is_derived_and_not_a_constant(self, monkeypatch):
        """Move a budget the server enforces and the client's ceiling moves
        with it. That is the whole point: the next fallback instance must not
        silently re-open #178."""
        from services import enrich_budget

        monkeypatch.setattr(Config, "ENRICH_LOOKUP_BUDGET_S", 240.0)
        before = enrich_budget.worst_case_seconds()
        monkeypatch.setattr(Config, "ENRICH_LOOKUP_BUDGET_S", 400.0)
        assert enrich_budget.worst_case_seconds() == before + 160.0

        monkeypatch.setattr(Config, "AI_ANALYSIS_TIMEOUT_SECONDS", 300)
        assert enrich_budget.worst_case_seconds() > before + 160.0

    def test_it_is_never_shorter_than_the_run_it_describes(self):
        """The harmful direction is *short*.

        A client that stops polling while the server is still working reports
        a running job as failed, and the obvious next move pays for it again
        (#178). So the sum has to cover every server-side wait a legitimate
        run can contain, not the typical one.
        """
        from services import enrich_budget

        lookups = float(Config.ENRICH_LOOKUP_BUDGET_S)
        ai = float(Config.AI_ANALYSIS_TIMEOUT_SECONDS) + float(
            Config.AI_BRIDGE_SOCKET_MARGIN_SECONDS
        )
        paid = float(Config.ENRICH_PAID_ALLOWANCE_S)
        assert enrich_budget.worst_case_seconds() >= lookups + ai + paid

    def test_it_covers_the_budget_the_server_actually_enforces(self):
        from services import enrich_budget

        assert (
            enrich_budget.worst_case_seconds() > enrich_budget.lookup_budget_seconds()
        )

    def test_both_clients_prefer_the_server_number(self):
        """A text assertion, and a weak one on its own -- but the alternative
        is a constant nobody reads until it is stale again. Both surfaces are
        checked because the enrich response is polled from two places."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        main_js = (root / "static" / "js" / "main.js").read_text()
        detail = (root / "templates" / "property_detail.html").read_text()

        assert "response.poll_timeout_ms" in main_js
        assert "data.poll_timeout_ms" in detail
        # And the sequel is declared once, up front, rather than driven from
        # the tab step by step.
        assert "analyze: true" in detail


class TestARowWithNoCoordinateKeepsWhatWasMeasured:
    """The re-ordering must not hand the pool step a row it never used to see.

    `PoolService._compute` answers `no_coordinates` without a network call --
    but that status is not one of the two its "a refusal never overwrites an
    answer" guard defends against, so running it on the coordinate-less path
    would write it over a measurement taken when the row still had one.
    """

    @pytest.fixture
    def app(self):
        setup_test_environment()
        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    def test_the_pool_step_is_not_run_without_a_coordinate(self, app, monkeypatch):
        from services import property_enrichment_service as module

        ran = []

        class _Location:
            def ensure_coordinates(self, prop, refresh=False, *, commit=False):
                return False

        class _Pool:
            def enrich(self, prop, commit=False):
                ran.append("pool")
                return {"status": "no_coordinates"}

        service = module.PropertyEnrichmentService(
            location_service=_Location(), pool_service=_Pool()
        )
        monkeypatch.setattr(service, "enrich_free_sources", lambda p, **kw: None)
        monkeypatch.setattr(
            module.advertiser, "enrich", lambda prop, *, commit=False: {}
        )

        prop = Property(source_email_id="no-coords", title="Plot")
        db.session.add(prop)
        db.session.commit()

        assert service.enrich_property(prop) is False
        assert ran == [], "the pool step ran on a row with no coordinate"

    def test_it_is_run_when_there_is_one(self, app, monkeypatch):
        """The control: without it the assertion above passes on a pool step
        that was simply never wired at all."""
        from services import property_enrichment_service as module

        ran = []

        class _Location:
            def ensure_coordinates(self, prop, refresh=False, *, commit=False):
                return True

        class _Pool:
            def enrich(self, prop, commit=False):
                ran.append("pool")
                return {}

        class _Travel:
            def calculate_for_property(self, prop, commit=False):
                return True

        class _Sea:
            def update_property(self, prop, *, commit=False):
                return None

        service = module.PropertyEnrichmentService(
            location_service=_Location(),
            pool_service=_Pool(),
            travel_service=_Travel(),
            sea_distance_service=_Sea(),
        )
        monkeypatch.setattr(service, "enrich_free_sources", lambda p, **kw: None)
        monkeypatch.setattr(
            module.advertiser, "enrich", lambda prop, *, commit=False: {}
        )

        prop = Property(
            source_email_id="with-coords",
            title="Plot",
            location_lat=43.5,
            location_lon=-6.0,
        )
        db.session.add(prop)
        db.session.commit()

        service.enrich_property(prop, recalc_scoring=False)
        assert ran == ["pool"]


class TestASpentClockIsNotTheHostSayingNo:
    """The seam between this ticket's deadline and #438's breaker.

    `OVERPASS_BREAKERS` opens after three refusals and then answers from what
    is already known for five minutes. A budget refusal says nothing about
    whether the instance would have answered -- so counting it would arm those
    five minutes against a healthy host on the strength of somebody else's
    slow run, and the *next* press, with a full budget, would find every
    instance pre-refused.
    """

    def test_the_amenity_walk_does_not_arm_the_breaker(self, monkeypatch):
        from utils.http import OVERPASS_BREAKERS

        monkeypatch.setattr(
            Config,
            "OSM_OVERPASS_FALLBACK_URLS",
            ["https://overpass.kumi.systems/api/interpreter"],
        )
        service = EnrichmentService.__new__(EnrichmentService)
        service.osm_overpass_url = "https://overpass-api.de/api/interpreter"

        for _ in range(OVERPASS_BREAKERS.threshold + 1):
            with lookup_budget(0.0):
                _elements, failure = service._overpass_elements("[out:json];out;")
            assert failure.reason == REASON_BUDGET_EXHAUSTED

        for url in (service.osm_overpass_url, *Config.OSM_OVERPASS_FALLBACK_URLS):
            breaker = OVERPASS_BREAKERS.for_url(url)
            assert breaker.state()["consecutive_refusals"] == 0, url
            assert breaker.should_skip() is False, url

    def test_the_coastline_client_does_not_arm_it_either(self, monkeypatch):
        import services.sea_view_service as sea_view
        from utils.http import OVERPASS_BREAKERS

        monkeypatch.setattr(sea_view, "_cache_get", lambda *a, **kw: None)
        monkeypatch.setattr(sea_view, "_cache_set", lambda *a, **kw: None)

        for _ in range(OVERPASS_BREAKERS.threshold + 1):
            with lookup_budget(0.0):
                with pytest.raises(sea_view.SeaViewBudgetExceeded):
                    sea_view.fetch_coastline_points(43.5, -6.0)

        breaker = OVERPASS_BREAKERS.for_url(Config.OSM_OVERPASS_URL)
        assert breaker.state()["consecutive_refusals"] == 0
        assert breaker.should_skip() is False

    def test_a_real_refusal_still_arms_it(self, monkeypatch):
        """The control. Without it both assertions above would pass on a
        breaker that had simply stopped counting anything."""
        import services.enrichment_service as enrichment_module
        from utils.http import OVERPASS_BREAKERS

        monkeypatch.setattr(Config, "OSM_OVERPASS_FALLBACK_URLS", [])
        service = EnrichmentService.__new__(EnrichmentService)
        service.osm_overpass_url = "https://overpass-api.de/api/interpreter"

        def _refuse(fn, *a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(enrichment_module, "request_with_retries", _refuse)
        service._overpass_elements("[out:json];out;")
        assert (
            OVERPASS_BREAKERS.for_url(service.osm_overpass_url).state()[
                "consecutive_refusals"
            ]
            == 1
        )
