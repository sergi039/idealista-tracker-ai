"""Money is spent only inside an authorization somebody opened on purpose.

The owner's rule, after the billing history this repository already records:
EUR 190 for 1-18 August 2026 on a project ingesting ~7 listings a day, and one
morning in which 306 listings were enriched into a throwaway dev database for
roughly $110 of credit nobody read. The response at the time was to default
`AUTO_TRAVEL_ENRICHMENT` to false. That closed the path that had just been
walked and left the others open: three HTTP endpoints, seven CLI tools, the
background-job executor and the legacy `Land` ingest all reached a billed
Google API with no flag in front of them, and `POST /api/lands/enrich-all` did
it in an unbounded loop over the whole table, unauthenticated and CSRF-exempt.

`utils/google_spend` is the one door now. This file is what makes that a claim
worth believing rather than an intention, and it is deliberately split into
two kinds of assertion, because they fail for different reasons:

* **Structural** -- there is no second door. A tree-wide grep, so a twelfth
  call site added next month cannot spend by simply not knowing about this.
  It is the shape `tests/test_scheduler_flag_fails_closed.py` and
  `tests/test_deploy_page_check_shared.py` already use for a contract that
  lives in more than one file.
* **Behavioural** -- the door is shut by default, the cap binds, a refusal is
  a refusal and never a measurement, and the surfaces that legitimately spend
  really do open an authorization.

What this file does **not** claim, stated here because a guard presented as
complete is worse than one known to be partial: it cannot see a process that
never imports the module -- a `curl` to Google, or a script building its own
`requests.get`. The boundary is this transport, not the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

import utils.google_spend as google_spend
from app import create_app, db
from models import Land, Property
from tests import setup_test_environment
from utils.google_api import (
    REASON_SPEND_CAP_EXCEEDED,
    REASON_SPEND_NOT_AUTHORIZED,
    REASON_SPEND_OFF_ON_THIS_MACHINE,
    failure_from_exception,
)
from utils.google_spend import (
    API_DISTANCE_MATRIX,
    API_GEOCODING,
    API_PLACES_NEARBY,
    PaidCallRefused,
    SpendNotRequested,
    authorized_spend,
    billed_get,
    cli_authorization,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The one module allowed to name a billed Google endpoint.
THE_ONE_DOOR = "utils/google_spend.py"


@pytest.fixture
def app(tmp_path, monkeypatch):
    setup_test_environment()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


@pytest.fixture
def no_authorization():
    """Close the suite-wide authorization `tests/conftest.py` opens.

    The suite grants one so that forty modules mocking `requests.get` test the
    service they name rather than this gate's refusal branch. Every test in
    this file that is *about* the gate has to take it away again, and put it
    back afterwards -- conftest resets it per test either way, but a test that
    relied on that would be relying on teardown to make its own setup true.
    """
    token = google_spend._AUTHORIZATION.set(None)
    try:
        yield
    finally:
        google_spend._AUTHORIZATION.reset(token)


def _ok_response(payload=None):
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload if payload is not None else {"status": "OK"}
    return response


# ---------------------------------------------------------------------------
# Structural: there is no second door
# ---------------------------------------------------------------------------


class TestThereIsExactlyOneDoor:
    def test_no_other_module_names_a_billed_google_endpoint(self):
        """A twelfth call site cannot appear anywhere else and spend quietly.

        This is the assertion the whole design rests on. Every behavioural
        test below is about a call that goes *through* `billed_get`; none of
        them can see a call that does not. Only a grep can.
        """
        offenders = []
        for directory in ("services", "utils", "routes", "tools"):
            root = REPO_ROOT / directory
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                relative = path.relative_to(REPO_ROOT).as_posix()
                if relative == THE_ONE_DOOR:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if "maps.googleapis.com" in text:
                    offenders.append(relative)

        assert offenders == [], (
            "These modules name a billed Google endpoint directly, so they can "
            "spend without passing the authorization check in "
            f"{THE_ONE_DOOR}: {offenders}. Route the call through "
            "`utils.google_spend.billed_get` instead -- it takes an `api` "
            "constant, never a URL."
        )

    def test_the_one_door_knows_every_billed_api(self):
        """The URL table and the set of billed APIs cannot drift apart.

        `billed_get` refuses an unknown `api`, so an API added to one and not
        the other is either an unusable constant or a KeyError at the moment
        somebody presses Enrich.
        """
        assert set(google_spend._URLS) == set(google_spend.BILLED_APIS)
        for api, url in google_spend._URLS.items():
            assert url.startswith("https://maps.googleapis.com/"), (api, url)


# ---------------------------------------------------------------------------
# Behavioural: the door is shut by default
# ---------------------------------------------------------------------------


class TestTheDefaultIsNo:
    def test_a_billed_call_with_no_authorization_is_refused(self, no_authorization):
        with patch.object(google_spend, "requests") as transport:
            with pytest.raises(PaidCallRefused) as excinfo:
                billed_get(API_PLACES_NEARBY, params={"key": "k"}, units=1)

        assert excinfo.value.reason == REASON_SPEND_NOT_AUTHORIZED
        transport.get.assert_not_called()

    def test_nothing_leaves_the_machine_when_it_is_refused(self, no_authorization):
        """The refusal happens *before* the request, not after it.

        A gate that refuses on the way back has already been billed. Asserted
        against the real transport rather than a stub of `billed_get`'s own
        helper: `request_with_retries` is what would issue the call.
        """
        with patch.object(google_spend, "request_with_retries") as issue:
            with pytest.raises(PaidCallRefused):
                billed_get(API_DISTANCE_MATRIX, params={"key": "k"}, units=26)
        issue.assert_not_called()

    def test_an_authorization_lets_exactly_that_call_through(self):
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("owner asked", actor="test", cap_units=10):
                response = billed_get(API_GEOCODING, params={"key": "k"}, units=1)
        assert response.status_code == 200
        assert transport.get.call_count == 1

    def test_the_authorization_does_not_outlive_its_block(self, no_authorization):
        """The property the routes depend on when they open theirs inside a job."""
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("owner asked", actor="test", cap_units=10):
                billed_get(API_GEOCODING, params={"key": "k"}, units=1)

            with pytest.raises(PaidCallRefused):
                billed_get(API_GEOCODING, params={"key": "k"}, units=1)

    def test_it_does_not_leak_into_a_thread_the_executor_starts(self):
        """A `contextvars` value does not cross into a new thread.

        This is *relied upon*, not merely tolerated: it is why
        `routes/api_routes.py` opens its authorization inside the job closure
        rather than around `_enqueue`. If this ever became false, an
        authorization granted for one request would cover every job queued
        during it.
        """
        import threading

        seen = {}

        def worker():
            seen["verdict"] = google_spend.spend_verdict(1)

        with authorized_spend("owner asked", actor="test", cap_units=10):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        assert seen["verdict"].allowed is False
        assert seen["verdict"].reason == REASON_SPEND_NOT_AUTHORIZED


# ---------------------------------------------------------------------------
# Behavioural: a refusal is a refusal, never a measurement
# ---------------------------------------------------------------------------


class TestARefusalIsNotAnAnswer:
    def test_it_is_a_request_exception_so_callers_already_degrade(self):
        """Every one of the eleven call sites catches `Exception` and hands it
        to `failure_from_exception`. That is the whole reason refusing costs
        no new branch anywhere -- and it only holds while this is true."""
        assert issubclass(PaidCallRefused, requests.RequestException)

    @pytest.mark.parametrize(
        "reason",
        [
            REASON_SPEND_NOT_AUTHORIZED,
            REASON_SPEND_CAP_EXCEEDED,
            REASON_SPEND_OFF_ON_THIS_MACHINE,
        ],
    )
    def test_it_classifies_as_itself_and_not_as_a_network_error(self, reason):
        """A decision about our own wallet must not read as Google being down.

        `PaidCallRefused` subclasses `RequestException`, so without the
        explicit branch in `failure_from_exception` it would be reported as
        `network_error` -- sending an operator to look at Google's status page
        for a refusal this application chose.
        """
        failure = failure_from_exception(PaidCallRefused(reason, "refused"))
        assert failure.reason == reason

    def test_a_refused_geocode_falls_back_to_the_free_source(self, no_authorization):
        """The behaviour that keeps a cost control from costing measurements.

        `AUTO_GEOCODING` exists because a row with no coordinate loses four
        *free* downstream measurements. A refused billed geocode must
        therefore reach Nominatim, not return None.
        """
        from utils.geocoding import GeocodingService

        service = GeocodingService()
        service.google_maps_key = "a-key-so-the-billed-branch-is-taken"

        with patch("utils.geocoding.request_with_retries") as nominatim:
            nominatim.return_value = _ok_response(
                [
                    {
                        "lat": "43.54",
                        "lon": "-6.72",
                        "display_name": "Navia, Asturias, Spain",
                        "addresstype": "town",
                        "place_rank": 16,
                        "address": {"town": "Navia", "postcode": "33710"},
                    }
                ]
            )
            result = service.geocode_address("Navia, Asturias")

        assert result is not None
        assert nominatim.call_count == 1, (
            "a refused billed geocode must fall through to the free source, "
            "not give up: the coordinate is what four free measurements need"
        )


# ---------------------------------------------------------------------------
# Behavioural: the cap is a ceiling, not a report
# ---------------------------------------------------------------------------


class TestTheCapBinds:
    def test_units_are_charged_before_the_request(self):
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("owner", actor="test", cap_units=30) as grant:
                billed_get(API_DISTANCE_MATRIX, params={"key": "k"}, units=26)
                assert grant.spent == 26
                assert grant.remaining() == 4

    def test_a_call_that_would_exceed_the_cap_is_refused_whole(self):
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("owner", actor="test", cap_units=10):
                with pytest.raises(PaidCallRefused) as excinfo:
                    billed_get(API_DISTANCE_MATRIX, params={"key": "k"}, units=26)
            assert excinfo.value.reason == REASON_SPEND_CAP_EXCEEDED
            transport.get.assert_not_called()

    def test_retries_are_charged_because_google_saw_them(self):
        """`request_with_retries` may issue the same request three times.

        Each attempt is a request Google may bill for, so charging the nominal
        figure once would make the ledger disagree with the invoice in exactly
        the situation where somebody is reading both -- an API that is
        throttling.
        """
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                response = Mock()
                response.status_code = 429
                response.json.return_value = {}
                return response
            return _ok_response()

        # The backoff lives in `utils.http`, which is where the retry loop is;
        # patching it here would silently do nothing and add ~24 s of real
        # sleeping to the suite.
        import utils.http as http_module

        with patch.object(google_spend.requests, "get", side_effect=flaky):
            with patch.object(http_module, "_compute_backoff", return_value=0):
                with authorized_spend("owner", actor="test", cap_units=50) as grant:
                    billed_get(API_PLACES_NEARBY, params={"key": "k"}, units=1)

        assert calls["n"] == 3
        assert grant.spent == 3, (
            "three requests were issued and only one was charged: the ledger "
            "would under-report exactly when Google is throttling"
        )

    def test_a_nested_block_cannot_raise_the_ceiling(self):
        with authorized_spend("outer", actor="test", cap_units=10) as outer:
            with authorized_spend("inner", actor="test", cap_units=1000) as inner:
                assert inner.cap_units == 10
        assert outer.cap_units == 10

    def test_what_a_nested_block_spends_is_charged_to_its_parent(self):
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("outer", actor="test", cap_units=10) as outer:
                with authorized_spend("inner", actor="test", cap_units=10):
                    billed_get(API_GEOCODING, params={"key": "k"}, units=4)
                assert outer.spent == 4, (
                    "an inner block whose spend vanished on return would make "
                    "the outer cap bound nothing at all"
                )


# ---------------------------------------------------------------------------
# Behavioural: the machine-level switch
# ---------------------------------------------------------------------------


class TestTheMachineSwitch:
    def test_a_machine_told_not_to_spend_refuses_even_with_an_authorization(self):
        from config import Config

        with patch.object(Config, "GOOGLE_SPEND_ENABLED", False):
            with patch.object(google_spend, "requests") as transport:
                with authorized_spend("owner asked", actor="test", cap_units=10):
                    with pytest.raises(PaidCallRefused) as excinfo:
                        billed_get(API_GEOCODING, params={"key": "k"}, units=1)
            assert excinfo.value.reason == REASON_SPEND_OFF_ON_THIS_MACHINE
            transport.get.assert_not_called()

    def test_it_defaults_to_on_read_from_a_clean_interpreter(self):
        """Read out of a fresh process, not from this suite's patched `Config`.

        Defaulting it *off* would stop the deployment's own Enrich button on
        the deploy that shipped it -- the mistake already on record for
        `AUTO_START_SCHEDULER`. The authorization above is the gate; this flag
        is the second lock, for a machine that must never spend, and it is set
        false *there*.
        """
        env = dict(os.environ)
        env.pop("GOOGLE_SPEND_ENABLED", None)
        env["DATABASE_URL"] = "sqlite:///:memory:"
        env.setdefault("SECRET_KEY", "x")
        env.setdefault("SESSION_SECRET", "x")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json;from config import Config;"
                "print(json.dumps({'spend': Config.GOOGLE_SPEND_ENABLED}))",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip())["spend"] is True


# ---------------------------------------------------------------------------
# Behavioural: the ledger
# ---------------------------------------------------------------------------


class TestTheLedger:
    def _entries(self, tmp_path):
        path = Path(google_spend.ledger_path())
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_a_billed_call_is_recorded_with_who_asked_for_it(
        self, app, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            google_spend, "ledger_path", lambda: str(tmp_path / "l.jsonl")
        )
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend(
                "Enrich pressed for property 793", actor="api:test", cap_units=50
            ):
                billed_get(
                    API_DISTANCE_MATRIX,
                    params={"key": "k"},
                    units=26,
                    subject="43.5,-6.4",
                )

        entries = [
            json.loads(line)
            for line in (tmp_path / "l.jsonl").read_text().splitlines()
            if line
        ]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["api"] == API_DISTANCE_MATRIX
        assert entry["units"] == 26
        assert entry["outcome"] == "answered"
        assert entry["reason"] == "Enrich pressed for property 793"
        assert entry["actor"] == "api:test"
        assert entry["subject"] == "43.5,-6.4"

    def test_a_refusal_is_recorded_too_and_charged_nothing(
        self, tmp_path, monkeypatch, no_authorization
    ):
        monkeypatch.setattr(
            google_spend, "ledger_path", lambda: str(tmp_path / "l.jsonl")
        )
        with pytest.raises(PaidCallRefused):
            billed_get(API_PLACES_NEARBY, params={"key": "k"}, units=1)

        entries = [
            json.loads(line)
            for line in (tmp_path / "l.jsonl").read_text().splitlines()
            if line
        ]
        assert len(entries) == 1
        assert entries[0]["outcome"] == "refused"
        assert entries[0]["units"] == 0
        assert entries[0]["refusal"] == REASON_SPEND_NOT_AUTHORIZED

    def test_an_unwritable_ledger_does_not_lose_a_paid_measurement(
        self, tmp_path, monkeypatch
    ):
        """The owner has already been billed by the time this runs.

        Failing the call here would throw away a measurement that has been
        paid for, to protect a record of it.
        """
        monkeypatch.setattr(
            google_spend, "ledger_path", lambda: "/proc/nonexistent/l.jsonl"
        )
        with patch.object(google_spend, "requests") as transport:
            transport.get.return_value = _ok_response()
            with authorized_spend("owner", actor="test", cap_units=10):
                response = billed_get(API_GEOCODING, params={"key": "k"}, units=1)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Behavioural: the surfaces
# ---------------------------------------------------------------------------


class TestTheEntryPoints:
    def test_bulk_enrich_all_refuses_instead_of_looping_over_the_table(self, app):
        """The single widest uncontrolled spender in the tree.

        Unauthenticated, CSRF-exempt, rate-limited only at 2 per 5 minutes,
        and it called `enrich_land` on every row with an empty enrichment
        column. Measured 2026-08-26: no template and no static asset calls it.
        """
        client = app.test_client()
        response = client.post("/api/lands/enrich-all")
        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert body["reason"] == "spend_not_authorized"

    def test_bulk_enrich_all_makes_no_request_at_all(self, app):
        with patch.object(google_spend, "request_with_retries") as issue:
            app.test_client().post("/api/lands/enrich-all")
        issue.assert_not_called()

    def test_the_property_enrich_route_opens_one(self, app, no_authorization):
        """An owner pressing Enrich *is* the owner asking.

        Asserted through the real route rather than by reading it: the whole
        failure mode this file exists for is a rule that is described and not
        enforced.
        """
        prop = Property(
            title="A plot",
            url="https://www.idealista.com/inmueble/1/",
            source_email_id="test:1",
        )
        db.session.add(prop)
        db.session.commit()

        seen = {}

        def _capture(*args, **kwargs):
            grant = google_spend.current_authorization()
            seen["actor"] = grant.actor if grant else None
            seen["reason"] = grant.reason if grant else None
            return True

        with patch(
            "services.property_enrichment_service.PropertyEnrichmentService."
            "enrich_property",
            side_effect=_capture,
        ):
            response = app.test_client().post(f"/api/property/{prop.id}/enrich")

        assert response.status_code == 200
        assert seen["actor"] == "api:manual_property_enrichment", (
            "the Enrich route must open an authorization around the work, or "
            "every billed call it makes is refused"
        )
        assert str(prop.id) in seen["reason"]

    def test_the_land_enrich_route_opens_one(self, app, no_authorization):
        land = Land(
            title="A land",
            url="https://www.idealista.com/inmueble/2/",
            source_email_id="test:2",
        )
        db.session.add(land)
        db.session.commit()

        seen = {}

        def _capture(*args, **kwargs):
            grant = google_spend.current_authorization()
            seen["actor"] = grant.actor if grant else None
            return True

        with patch(
            "services.enrichment_service.EnrichmentService.enrich_land",
            side_effect=_capture,
        ):
            response = app.test_client().post(f"/api/land/{land.id}/enrich")

        assert response.status_code == 200
        assert seen["actor"] == "api:manual_enrichment"


class TestTheCliContract:
    def test_a_billed_tool_refuses_to_start_without_a_reason(self):
        with pytest.raises(SpendNotRequested) as excinfo:
            with cli_authorization(None, actor="utils.example", rows=40):
                pass
        assert "--reason" in str(excinfo.value)

    def test_a_blank_reason_is_not_a_reason(self):
        with pytest.raises(SpendNotRequested):
            with cli_authorization("   ", actor="utils.example", rows=1):
                pass

    def test_the_cap_is_arithmetic_on_the_scope(self):
        with cli_authorization(
            "the owner asked", actor="utils.example", rows=3, per_row=50
        ) as grant:
            assert grant.cap_units == 150

    @pytest.mark.parametrize(
        "module",
        [
            "utils.recalc_property_travel",
            "utils.recalc_travel_times",
            "utils.backfill_beach_travel",
            "utils.backfill_pool",
            "utils.refresh_property_accuracy",
            "utils.refresh_coordinates",
            "utils.import_cnh_hospitals",
        ],
    )
    def test_every_billed_cli_tool_offers_the_flag(self, module):
        """A tool that bills and has no `--reason` cannot be run at all.

        `cli_authorization` refuses without one, so a tool that forgot to add
        the argument would be permanently broken rather than permanently
        free -- which is the safe direction, but a confusing way to find out.
        """
        import ast
        import importlib

        source = Path(importlib.import_module(module).__file__).read_text(
            encoding="utf-8"
        )
        # Parsed, not grepped. A substring search is satisfied by the call
        # appearing in a comment, which is not a hypothetical: the first
        # version of this test stayed green under a mutation that replaced the
        # call with `pass  # add_spend_arguments(parser)`. That is CLAUDE.md's
        # own warning about text substitution, arriving from the other side --
        # the assertion matched, and the code did nothing.
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "add_spend_arguments"
            for node in ast.walk(ast.parse(source))
        )
        assert called, (
            f"{module} spends Google credit and never calls "
            "add_spend_arguments(parser), so it offers no --reason"
        )


# ---------------------------------------------------------------------------
# Behavioural: a free-source outage must not become paid traffic
# ---------------------------------------------------------------------------


class TestAnOutageDoesNotBecomeABill:
    def test_an_overpass_refusal_does_not_reach_the_billed_cross_check(self, app):
        """The expensive direction, and the shape of the EUR 190 incident.

        `PoolService._cross_check` is a billed Places Text Search that runs
        when OpenStreetMap answers and finds no pool. If an Overpass *refusal*
        also reached it, every hour the free source is down would be an hour
        of paying Google per listing -- and this project has measured its own
        IP being refused by overpass-api.de twice, on 2026-08-19 and
        2026-08-20.

        `_compute` returns `unavailable` on a discovery failure, before the
        cross-check. Pinned here because nothing else states it, and the two
        branches are four lines apart.
        """
        from services.pool_service import PoolService

        prop = Property(
            title="A plot",
            url="https://www.idealista.com/inmueble/3/",
            source_email_id="test:3",
            location_lat=43.54,
            location_lon=-6.72,
        )
        db.session.add(prop)
        db.session.commit()

        service = PoolService()
        with patch.object(
            service,
            "discover_candidates",
            return_value={"failure_reason": "overpass_unavailable"},
        ):
            with patch.object(service, "_cross_check") as cross_check:
                result = service._compute(prop)

        (
            cross_check.assert_not_called(),
            (
                "an Overpass outage must not push traffic onto the billed Places "
                "search: that turns a free-source failure into a bill"
            ),
        )
        assert result["status"] == "unavailable"
