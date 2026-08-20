"""One public Overpass instance is a single point of failure. Now it is not.

Measured on the night of 2026-08-19, while the travel presets were being moved
onto Overpass: **overpass-api.de refused every connection from the Mac mini**
-- `curl` from the host itself timed out at 25 s, four times in a row -- while
answering this laptop in 0.27 s. It was an IP-level block or throttle, brought
on by that evening's own free backfills, and it lasted longer than an hour.
`overpass.kumi.systems` answered the mini with 200 in 3.5 s throughout, and
`overpass.private.coffee` in 2.1 s.

That matters more than it used to. Until 2026-08-18 an Overpass refusal cost
an amenity count nobody scored; since the presets moved off Places there is no
paid path behind them at all, so an instance that will not talk to this machine
means a listing measures nothing.

What these tests pin is the three ways a fallback list can be worse than none:
asking a second host about a refusal every host would repeat, hiding which
instance the deployment is actually configured against, and quietly becoming a
way to double the traffic when the first host is merely slow.
"""

import pytest
import requests

from config import Config
from services.enrichment_service import OSM_REASON_QUERY_ERROR, EnrichmentService
from tests import setup_test_environment
from utils.google_api import GoogleApiFailure, REASON_HTTP_ERROR

PRIMARY = "https://overpass-api.de/api/interpreter"
KUMI = "https://overpass.kumi.systems/api/interpreter"
COFFEE = "https://overpass.private.coffee/api/interpreter"


@pytest.fixture(autouse=True)
def _environment():
    setup_test_environment()
    yield


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(Config, "OSM_OVERPASS_URL", PRIMARY)
    monkeypatch.setattr(Config, "OSM_OVERPASS_FALLBACK_URLS", [KUMI, COFFEE])
    svc = EnrichmentService()
    svc.osm_overpass_url = PRIMARY
    return svc


def _answers(service, monkeypatch, outcomes):
    """`outcomes` maps a URL to (elements, failure). Records the order tried."""
    tried = []

    def _one(url, query):
        tried.append(url)
        return outcomes.get(url, ([], None))

    monkeypatch.setattr(service, "_overpass_elements_from", _one)
    return tried


class TestItMovesOnWhenTheInstanceWillNotTalk:
    def test_a_network_refusal_is_asked_of_the_next_instance(
        self, service, monkeypatch
    ):
        """The measured case: the primary times out, kumi answers."""
        tried = _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (None, GoogleApiFailure(reason="network_error")),
                KUMI: ([{"id": 1}], None),
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert failure is None
        assert elements == [{"id": 1}]
        assert tried == [PRIMARY, KUMI], "the primary is still tried first"

    def test_a_loaded_instance_is_worth_a_second_opinion(self, service, monkeypatch):
        """A `remark` inside a 200 is that instance saying it is overloaded."""
        tried = _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (None, GoogleApiFailure(reason=OSM_REASON_QUERY_ERROR)),
                KUMI: ([{"id": 2}], None),
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert failure is None
        assert elements == [{"id": 2}]
        assert tried == [PRIMARY, KUMI]

    def test_it_walks_the_whole_list(self, service, monkeypatch):
        tried = _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (None, GoogleApiFailure(reason=REASON_HTTP_ERROR)),
                KUMI: (None, GoogleApiFailure(reason=REASON_HTTP_ERROR)),
                COFFEE: ([{"id": 3}], None),
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert failure is None
        assert elements == [{"id": 3}]
        assert tried == [PRIMARY, KUMI, COFFEE]


class TestItDoesNotAskTwiceForNothing:
    def test_a_406_stops_at_the_first_instance(self, service, monkeypatch):
        """That refusal is this client's User-Agent, and every instance runs
        the same software: moving hosts repeats it and doubles the traffic.

        The failure is built the way **production** builds it. It used to be
        `reason="not_acceptable"`, which nothing emits -- every non-200 comes
        back as `REASON_HTTP_ERROR`, which is fallback-eligible -- so the rule
        this file states in words was not the rule the code followed, and a
        real 406 fanned out to all three hosts (codex review, 2026-08-20).
        """
        tried = _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (
                    None,
                    GoogleApiFailure(reason=REASON_HTTP_ERROR, http_status=406),
                )
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert elements is None
        assert failure is not None
        assert tried == [PRIMARY], "a 406 is the same answer everywhere"

    def test_a_502_still_tries_the_next_instance(self, service, monkeypatch):
        """...and the narrowing must not turn every HTTP error into a stop:
        a 502 is one instance being down, which is what the list is for."""
        tried = _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (
                    None,
                    GoogleApiFailure(reason=REASON_HTTP_ERROR, http_status=502),
                ),
                KUMI: ([{"id": 5}], None),
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert failure is None and elements == [{"id": 5}]
        assert tried == [PRIMARY, KUMI]

    def test_an_answer_costs_exactly_one_instance(self, service, monkeypatch):
        tried = _answers(service, monkeypatch, {PRIMARY: ([{"id": 4}], None)})

        service._overpass_elements("[out:json];node;out;")

        assert tried == [PRIMARY], "a fallback list must not become a fan-out"


class TestTheFailureNamesTheConfiguredInstance:
    def test_the_first_failure_is_what_comes_back(self, service, monkeypatch):
        """An operator reading "kumi.systems timed out" looks in the wrong
        place: the deployment is configured against the primary."""
        _answers(
            service,
            monkeypatch,
            {
                PRIMARY: (
                    None,
                    GoogleApiFailure(reason="network_error", message="primary"),
                ),
                KUMI: (
                    None,
                    GoogleApiFailure(reason=REASON_HTTP_ERROR, message="kumi"),
                ),
                COFFEE: (
                    None,
                    GoogleApiFailure(reason=REASON_HTTP_ERROR, message="coffee"),
                ),
            },
        )

        elements, failure = service._overpass_elements("[out:json];node;out;")

        assert elements is None
        assert failure.message == "primary"


class TestTheShippedConfiguration:
    def test_the_primary_is_unchanged(self):
        assert Config.OSM_OVERPASS_URL == PRIMARY

    def test_fallbacks_ship_non_empty_and_exclude_the_primary(self):
        fallbacks = Config.OSM_OVERPASS_FALLBACK_URLS
        assert fallbacks, "one instance is the single point of failure this removes"
        assert PRIMARY not in fallbacks

    def test_the_transport_still_carries_the_product_token(self, monkeypatch):
        """The 406 above is what happens without it (#144). Moving hosts must
        not have quietly dropped the header the primary taught us to send."""
        seen = {}

        def _capture(fn, url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            raise requests.RequestException("stop here")

        monkeypatch.setattr(
            "services.enrichment_service.request_with_retries", _capture
        )
        svc = EnrichmentService()
        svc._overpass_elements_from(KUMI, "[out:json];node;out;")

        assert seen.get("User-Agent") == "IdealistaRank/1.0"
