"""One Enrich press costs a bounded amount of somebody else's capacity (#399).

Measured on the mini 2026-08-17: Overpass stopped opening sockets at all, and
`enrich_property` — whose chain makes up to eleven Overpass round trips — kept
running long after the pressing session's own client gave up at 300 s.

Two things about that are worth stating precisely, because the ticket's
headline is out of date and the arithmetic behind it was low.

**The button is already a background job.** `POST /api/property/<id>/enrich`
enqueues and answers 202 with a job id; `static/js/main.js` polls it. So a
press does not hold a gunicorn thread — except through `?sync=1`, which is the
one path that still runs the chain inside the request, on an unauthenticated
API. #136 closed exactly that hatch on the two status endpoints and this one
kept it.

**What it holds instead is an executor slot**, and there are four of them
(`BACKGROUND_WORKERS`, default 4) shared by *every* job type. So an outage did
not stop the app answering; it stopped every background job in the app.

What this file pins is the three things that bound the cost:

* the hatch is closed **at the call site**, not merely available to be closed —
  a test that only asks `_should_run_sync(allow_request_override=False)` proves
  the helper works and says nothing about whether the endpoint passes it;
* a second press on the same property joins the run in flight instead of
  taking a second slot;
* the connect timeout is separated from the read timeout on both Overpass
  transports. `urllib3.Timeout.from_float(60)` gives `connect=60 read=60`, so
  sixty seconds were being spent learning that a host does not answer, twelve
  times per call site. A healthy connect to all three instances measures
  0.06–0.08 s (three samples each, 2026-08-20).
"""

import pytest

from tests import setup_test_environment

setup_test_environment()

import requests  # noqa: E402

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402


def _dead(*args, **kwargs):
    """What a host that is not opening sockets actually raises.

    A bare `RuntimeError` escapes both transports -- they classify
    `requests.RequestException` and let anything else through -- so a stub
    raising one tests the escape path, not the refusal path.
    """
    raise requests.ConnectionError("connect timeout")


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
def prop(app):
    row = Property(
        source_email_id="enrich_399",
        title="Land for sale in Avilés",
        municipality="Avilés",
        url="https://www.idealista.com/en/inmueble/1/",
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestTheSyncHatchIsClosedAtTheCallSite:
    def test_the_endpoint_queues_even_when_asked_to_run_inline(
        self, app, client, prop, monkeypatch
    ):
        """The assertion is about the endpoint, not about the helper.

        `_should_run_sync(allow_request_override=False)` returning False proves
        the helper; only a request proves the call site passes the argument.
        `TESTING` is off for the duration because the helper answers True under
        it by design.
        """
        calls = []
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: calls.append(kw) or "job-1",
        )
        app.config["TESTING"] = False
        try:
            response = client.post(f"/api/property/{prop.id}/enrich?sync=1")
        finally:
            app.config["TESTING"] = True

        assert response.status_code == 202, response.get_data(as_text=True)
        assert response.get_json()["status"] == "queued"
        assert calls, "the chain was run inline instead of being queued"

    def test_the_two_endpoints_that_closed_it_first_still_have_it_closed(self, app):
        """The control. If this ever goes red the pattern moved, and the
        assertion above is about a spelling nobody uses any more."""
        from routes.api_routes import _should_run_sync

        with app.test_request_context("/api/property/1/enrich?sync=1"):
            app.config["TESTING"] = False
            try:
                assert _should_run_sync() is True, "the hatch exists elsewhere"
                assert _should_run_sync(allow_request_override=False) is False
            finally:
                app.config["TESTING"] = True


class TestASecondPressJoinsTheFirst:
    def test_the_same_property_gets_the_same_job(self, app, client, prop, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "routes.api_routes._should_run_sync", lambda *a, **kw: False
        )
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: seen.append(kw.get("dedupe_key")) or "job-1",
        )

        client.post(f"/api/property/{prop.id}/enrich")
        client.post(f"/api/property/{prop.id}/enrich")

        assert seen == [
            f"property_enrich:{prop.id}",
            f"property_enrich:{prop.id}",
        ], seen

    def test_a_different_property_gets_its_own(self, app, client, prop, monkeypatch):
        other = Property(source_email_id="enrich_399_b", title="Another")
        db.session.add(other)
        db.session.commit()

        seen = []
        monkeypatch.setattr(
            "routes.api_routes._should_run_sync", lambda *a, **kw: False
        )
        monkeypatch.setattr(
            "routes.api_routes._enqueue",
            lambda fn, **kw: seen.append(kw.get("dedupe_key")) or "job-1",
        )

        client.post(f"/api/property/{prop.id}/enrich")
        client.post(f"/api/property/{other.id}/enrich")

        assert seen[0] != seen[1], seen
        assert None not in seen, "a missing dedupe key lets a re-press take a slot"


class TestTheConnectTimeoutIsSeparateFromTheRead:
    """A scalar timeout is spent twice over: `urllib3.Timeout.from_float(60)`
    is `connect=60 read=60`. The read leg is what a busy-but-alive Overpass
    needs (#144's 504 arrives after a completed handshake, so a connect-only
    bound cannot reclassify it); the connect leg is pure waiting on a host
    that is not there."""

    def test_the_amenity_transport_splits_them(self, app, monkeypatch):
        from services import enrichment_service as module

        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here: the call itself is not the subject")

        monkeypatch.setattr(module, "request_with_retries", spy)
        try:
            module.EnrichmentService()._overpass_elements_from(
                "https://overpass.example/api/interpreter", "[out:json];"
            )
        except Exception:
            pass

        assert seen.get("timeout") == (3.0, 60), seen.get("timeout")

    def test_the_coastline_transport_splits_them(self, app, monkeypatch):
        from services import sea_view_service as module

        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

        monkeypatch.setattr(module, "request_with_retries", spy)
        try:
            module.fetch_coastline_points(43.5, -5.9)
        except Exception:
            pass

        assert seen.get("timeout") == (3.0, 120), seen.get("timeout")


class TestTheBreakerStopsRedialingADeadHost:
    """Three refusals in a row, then five minutes of answering from what is
    already known.

    The connect timeout above cuts what one doomed attempt costs; this cuts how
    many of them happen. Without it, every call site in every press pays the
    whole cascade again to learn the same fact — eleven times per press, on
    four shared executor slots.

    The registry is the one already in the tree (`RefusalBreaker`/`HostBreakers`,
    written for idealista's DataDome wall), moved to `utils/http.py` because it
    needs no model, no session and no table. `services/listing_status_service.py`
    imports it back, so its own tests are untouched.
    """

    def test_a_skip_spends_nothing(self, app, monkeypatch):
        from services import enrichment_service as module

        url = "https://overpass.example/api/interpreter"
        calls = []
        monkeypatch.setattr(
            module,
            "request_with_retries",
            lambda *a, **kw: calls.append(1) or _dead(),
        )
        service = module.EnrichmentService()

        for _ in range(3):
            service._overpass_elements_from(url, "[out:json];")
        spent_before_the_breaker_opened = len(calls)

        elements, failure = service._overpass_elements_from(url, "[out:json];")

        assert spent_before_the_breaker_opened == 3
        assert len(calls) == 3, "the fourth call went out anyway"
        assert elements is None
        assert "not dialled" in (failure.message or "")

    def test_a_skip_still_lets_the_fallback_walk_continue(self, app, monkeypatch):
        """Load bearing, and easy to get wrong.

        `_overpass_elements` stops walking on a reason outside
        `_OVERPASS_TRY_ELSEWHERE`. If a skip reported anything else, the first
        open breaker would end the walk — quietly reinstating the single point
        of failure #415 removed.
        """
        from services import enrichment_service as module
        from utils.google_api import REASON_NETWORK_ERROR

        url = "https://overpass.example/api/interpreter"
        monkeypatch.setattr(
            module,
            "request_with_retries",
            lambda *a, **kw: _dead(),
        )
        service = module.EnrichmentService()
        for _ in range(3):
            service._overpass_elements_from(url, "[out:json];")

        _, failure = service._overpass_elements_from(url, "[out:json];")

        assert failure.reason == REASON_NETWORK_ERROR
        assert failure.reason in module.EnrichmentService._OVERPASS_TRY_ELSEWHERE

    def test_an_answer_closes_it_again(self, app, monkeypatch):
        """It heals on evidence, not on a timer."""
        from services import enrichment_service as module
        from utils.http import OVERPASS_BREAKERS

        url = "https://overpass.example/api/interpreter"
        service = module.EnrichmentService()
        monkeypatch.setattr(
            module,
            "request_with_retries",
            lambda *a, **kw: _dead(),
        )
        service._overpass_elements_from(url, "[out:json];")
        service._overpass_elements_from(url, "[out:json];")
        assert OVERPASS_BREAKERS.for_url(url).state()["consecutive_refusals"] == 2

        class _Ok:
            status_code = 200

            @staticmethod
            def json():
                return {"elements": []}

        monkeypatch.setattr(module, "request_with_retries", lambda *a, **kw: _Ok())
        service._overpass_elements_from(url, "[out:json];")

        assert OVERPASS_BREAKERS.for_url(url).state()["consecutive_refusals"] == 0

    def test_the_coastline_client_shares_the_registry(self, app, monkeypatch):
        """Both transports dial the same instances, so one learning that a host
        is down must spare the other from re-discovering it."""
        from services import sea_view_service as module
        from utils.http import OVERPASS_BREAKERS

        OVERPASS_BREAKERS.for_url(module.Config.OSM_OVERPASS_URL).record_refusal("x")
        OVERPASS_BREAKERS.for_url(module.Config.OSM_OVERPASS_URL).record_refusal("x")
        OVERPASS_BREAKERS.for_url(module.Config.OSM_OVERPASS_URL).record_refusal("x")

        calls = []
        monkeypatch.setattr(
            module,
            "request_with_retries",
            lambda *a, **kw: calls.append(1) or _dead(),
        )

        with pytest.raises(module.SeaViewSourceError) as caught:
            module.fetch_coastline_points(43.5, -5.9)

        assert not calls, "the coastline client dialled a host already known down"
        assert "not dialled" in str(caught.value)

    def test_a_skip_never_reads_as_an_empty_coastline(self, app, monkeypatch):
        """The contract this module was built around: an empty list means
        Overpass answered and there is no coastline in range."""
        from services import sea_view_service as module
        from utils.http import OVERPASS_BREAKERS

        for _ in range(3):
            OVERPASS_BREAKERS.for_url(module.Config.OSM_OVERPASS_URL).record_refusal(
                "x"
            )

        with pytest.raises(module.SeaViewSourceError):
            module.fetch_coastline_points(43.5, -5.9)
