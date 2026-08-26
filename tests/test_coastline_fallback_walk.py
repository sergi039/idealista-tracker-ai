"""The coastline client walks the fallback list (#480), the way #415 taught
the amenity client to.

Measured four times in eight days -- 19.08, 20.08, 24.08 and 26.08.2026 --
overpass-api.de refused every connection from the production machine while
answering another machine within about a second, and while every instance on
the fallback list answered the production machine too. The amenity client
walked and answered; the coastline client, the one transport behind both the
sea-view verdict and the sea-distance score, could only raise. On 26.08 that
cost five freshly enriched rows their sea measurement in one evening.

What these tests pin is the walk's contract, not the round trip's: the round
trip's own refusals -- the streamed size ceiling, the dripping-body clock, the
remark inside a 200 -- keep their tests in test_sea_view_service.py, and the
walk must keep raising exactly one exception type through all of it.
"""

import logging

import pytest
import requests

from config import Config
from services import sea_view_service as svc
from tests import setup_test_environment
from utils.http import OVERPASS_BREAKERS, LookupBudgetExceeded

PRIMARY = "https://overpass-api.de/api/interpreter"
FALLBACK_1 = "https://overpass.openstreetmap.fr/api/interpreter"
FALLBACK_2 = "https://overpass.kumi.systems/api/interpreter"

COAST_LAT, COAST_LON = 43.5, -5.9

GOOD_BODY = (
    b'{"elements": [{"type": "way", '
    b'"geometry": [{"lat": 43.51, "lon": -5.91}, {"lat": 43.52, "lon": -5.92}]}]}'
)
REMARK_BODY = b'{"elements": [], "remark": "runtime error: Query timed out"}'
EMPTY_BODY = b'{"elements": []}'


class _Reply:
    """The least response `_read_bounded_body` will read: streamed, closable."""

    def __init__(self, body: bytes, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    setup_test_environment()
    monkeypatch.setattr(Config, "OSM_OVERPASS_URL", PRIMARY)
    monkeypatch.setattr(Config, "OSM_OVERPASS_FALLBACK_URLS", [FALLBACK_1, FALLBACK_2])
    # The cache would shortcut the transport; these tests are about the
    # transport. Writes are recorded so the success test can assert one.
    monkeypatch.setattr(svc, "_cache_get", lambda *a, **kw: None)
    written = []
    monkeypatch.setattr(
        svc, "_cache_set", lambda *a, **kw: written.append((a, kw)) or None
    )
    yield written


def _transport(monkeypatch, outcomes):
    """`outcomes` maps a URL to a _Reply or an exception. Records the order."""
    dialled = []

    def _one(post, url, **kwargs):
        dialled.append(url)
        outcome = outcomes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(svc, "request_with_retries", _one)
    return dialled


class TestTheMeasuredCase:
    def test_a_silent_primary_costs_the_cell_nothing_any_more(
        self, monkeypatch, caplog, _environment
    ):
        """26.08.2026: overpass-api.de refuses the machine, openstreetmap.fr
        answers it in 0.24 s. Before #480 this raised; now it measures."""
        dialled = _transport(
            monkeypatch,
            {
                PRIMARY: requests.ConnectionError("connect timeout"),
                FALLBACK_1: _Reply(GOOD_BODY),
            },
        )

        with caplog.at_level(logging.INFO, logger="services.sea_view_service"):
            points = svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert points == [(43.51, -5.91), (43.52, -5.92)]
        assert dialled == [PRIMARY, FALLBACK_1], "the primary is still tried first"
        assert _environment, "a fallback's answer was not cached"
        assert any(
            "answered from the fallback" in record.message for record in caplog.records
        ), "an operator reading the log could not tell which instance answered"
        assert OVERPASS_BREAKERS.for_url(PRIMARY).state()["consecutive_refusals"] == 1
        assert (
            OVERPASS_BREAKERS.for_url(FALLBACK_1).state()["consecutive_refusals"] == 0
        )

    def test_an_empty_answer_from_a_fallback_is_still_a_measured_absence(
        self, monkeypatch
    ):
        """The contract `fetch_coastline_points` states: an empty list means
        Overpass answered and there is no coastline in range. Which instance
        answered does not change what an answer means."""
        _transport(
            monkeypatch,
            {
                PRIMARY: requests.ConnectionError("connect timeout"),
                FALLBACK_1: _Reply(EMPTY_BODY),
            },
        )

        assert svc.fetch_coastline_points(COAST_LAT, COAST_LON) == []


class TestWhatEndsTheWalk:
    def test_a_406_is_terminal_across_the_list(self, monkeypatch):
        """A 406 is this client's User-Agent being refused, and every instance
        runs the same software -- asking the rest repeats it for nothing
        (#144). One dial, not three."""
        dialled = _transport(monkeypatch, {PRIMARY: _Reply(b"", status_code=406)})

        with pytest.raises(svc.SeaViewSourceError, match="HTTP 406"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert dialled == [PRIMARY], "a User-Agent refusal was asked of a second host"

    def test_a_remark_inside_a_200_is_worth_a_second_opinion(self, monkeypatch):
        """A remark means that instance is loaded, not that the query is
        wrong -- the amenity walk's rule, kept here."""
        dialled = _transport(
            monkeypatch,
            {
                PRIMARY: _Reply(REMARK_BODY),
                FALLBACK_1: _Reply(GOOD_BODY),
            },
        )

        points = svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert points, "a loaded primary still cost the cell its measurement"
        assert dialled == [PRIMARY, FALLBACK_1]

    def test_the_first_failure_is_the_one_reported(self, monkeypatch):
        """It names the instance this deployment is configured against; an
        operator reading the fallback's error would go looking in the wrong
        place (#415's rule)."""
        _transport(
            monkeypatch,
            {
                PRIMARY: _Reply(b"", status_code=502),
                FALLBACK_1: _Reply(b"", status_code=500),
                FALLBACK_2: _Reply(b"", status_code=500),
            },
        )

        with pytest.raises(svc.SeaViewSourceError, match="HTTP 502"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)


class TestTheBreakerAndTheClock:
    def test_an_open_primary_breaker_is_walked_past_without_a_dial(self, monkeypatch):
        """The skip must try the next instance, not end the walk -- a skip
        that stopped it would reinstate the single point of failure, which is
        the defect this whole file exists to keep out."""
        for _ in range(OVERPASS_BREAKERS.threshold):
            OVERPASS_BREAKERS.for_url(PRIMARY).record_refusal("x")
        dialled = _transport(monkeypatch, {FALLBACK_1: _Reply(GOOD_BODY)})

        points = svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert points
        assert PRIMARY not in dialled, "a host already known down was dialled anyway"
        assert dialled == [FALLBACK_1]

    def test_a_spent_clock_mid_walk_reports_the_real_refusal(self, monkeypatch):
        """A host that already answered outranks the clock (#434): the caller
        hears the primary's refusal, and the un-asked fallback's breaker
        stays untouched -- a budget says nothing about that host."""
        _transport(
            monkeypatch,
            {
                PRIMARY: requests.ConnectionError("connect timeout"),
                FALLBACK_1: LookupBudgetExceeded("lookup budget exhausted"),
            },
        )

        with pytest.raises(svc.SeaViewSourceError, match="request failed") as caught:
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert not isinstance(caught.value, svc.SeaViewBudgetExceeded)
        assert (
            OVERPASS_BREAKERS.for_url(FALLBACK_1).state()["consecutive_refusals"] == 0
        )

    def test_a_spent_clock_with_no_earlier_refusal_stays_a_budget_error(
        self, monkeypatch
    ):
        """With nothing real to report, the budget error keeps its own type,
        so `needs_sea_view` keeps the row in scope and nothing is cached."""
        _transport(
            monkeypatch, {PRIMARY: LookupBudgetExceeded("lookup budget exhausted")}
        )

        with pytest.raises(svc.SeaViewBudgetExceeded):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

        assert OVERPASS_BREAKERS.for_url(PRIMARY).state()["consecutive_refusals"] == 0
