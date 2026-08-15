"""A wall is a standing condition, not 76 unrelated failures.

idealista answers this machine with DataDome bot protection. Measured
2026-08-15 over the 76 land rows of the Asturias coastal corridor, one at a
time with the service's own randomized pause: every single call logged
`Hit captcha protection`, not one reached a listing page. `curl` from the host
with a full set of browser headers gets 403 and the same block body, so it is
not the container.

The service was already fail-closed about it -- a captcha writes nothing, which
is issue #136's contract and stays pinned in its own file. What it did not do
was *remember*: every press of the check button spent another outbound request
to learn the same thing, and reported it as an unexplained failure.

So refusals are counted across calls (`RefusalBreaker`), the service stops
dialling once the host has said no three times running, and each refusal
carries a reason the page can name. Pinned here: the counting, the half-open
probe, and that a refusal still writes nothing.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from models import Property
from services.listing_status_service import (
    ListingStatusService,
    RefusalBreaker,
)
from tests import setup_test_environment

LISTING_ID = "109689073"
LISTING_URL = f"https://www.idealista.com/en/inmueble/{LISTING_ID}/"

CAPTCHA_PAGE = (
    '<html lang="es"><head><title>idealista.com</title></head><body>'
    "<p>Please enable JS and disable any ad blocker</p>"
    '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
)
LIVE_PAGE = "<html><body>Precio 250.000 EUR</body></html>"
HOME_PAGE = "<html><body>Casas y pisos, alquiler y venta</body></html>"


class FakeResponse:
    def __init__(self, status_code=200, text="", url=LISTING_URL):
        self.status_code = status_code
        self.text = text
        self.url = url


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


def _property(key="blocked", **overrides):
    fields = {
        "source_email_id": f"backoff-{key}",
        "title": f"Listing {key}",
        "url": LISTING_URL,
        "listing_status": "active",
        "listing_status_source": "ingest",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheBreakerItself:
    """Pure state machine, driven with an explicit clock so nothing sleeps."""

    def test_it_opens_only_on_consecutive_refusals(self):
        breaker = RefusalBreaker(threshold=3, cooldown_s=1800)
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

        breaker.record_refusal("blocked", now)
        breaker.record_refusal("blocked", now)
        assert breaker.should_skip(now) is False

        breaker.record_refusal("blocked", now)
        assert breaker.should_skip(now) is True
        assert breaker.state()["consecutive_refusals"] == 3
        assert breaker.state()["last_reason"] == "blocked"

    def test_an_answer_in_between_clears_the_count(self):
        """A run of failures is the signal; a scattered one is not."""
        breaker = RefusalBreaker(threshold=3, cooldown_s=1800)
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

        breaker.record_refusal("timeout", now)
        breaker.record_refusal("timeout", now)
        breaker.record_success(now)
        breaker.record_refusal("timeout", now)

        assert breaker.should_skip(now) is False
        assert breaker.state()["consecutive_refusals"] == 1

    def test_the_cooldown_ends_with_one_probe_not_with_trust(self):
        """It heals on evidence: the cooldown buys exactly one request back,
        and a refusal on that request re-arms it. A breaker that reopened the
        floodgates on a timer would resume the sweep into the same wall."""
        breaker = RefusalBreaker(threshold=3, cooldown_s=1800)
        start = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        for _ in range(3):
            breaker.record_refusal("blocked", start)

        during = start + timedelta(seconds=900)
        assert breaker.should_skip(during) is True

        after = start + timedelta(seconds=1801)
        assert breaker.should_skip(after) is False

        breaker.record_refusal("blocked", after)
        assert breaker.should_skip(after) is True
        assert breaker.should_skip(after + timedelta(seconds=900)) is True

    def test_a_probe_that_lands_reopens_it(self):
        breaker = RefusalBreaker(threshold=3, cooldown_s=1800)
        start = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        for _ in range(3):
            breaker.record_refusal("blocked", start)

        after = start + timedelta(seconds=1801)
        breaker.record_success(after)

        assert breaker.should_skip(after) is False
        assert breaker.state()["open"] is False
        assert breaker.state()["consecutive_refusals"] == 0


class TestTheServiceStopsDialling:
    def test_after_the_limit_no_request_leaves_the_process(self, app):
        with app.app_context():
            transport = Mock(
                return_value=FakeResponse(status_code=403, text=CAPTCHA_PAGE)
            )
            with patch(
                "services.listing_status_service.request_with_retries", transport
            ):
                service = ListingStatusService()
                for _ in range(ListingStatusService.REFUSAL_BREAKER_THRESHOLD):
                    service.observe(LISTING_URL)

                spent = transport.call_count
                assert spent == ListingStatusService.REFUSAL_BREAKER_THRESHOLD

                # The next several checks are answered from what we already
                # know, and cost idealista nothing.
                for _ in range(5):
                    observation = service.observe(LISTING_URL)
                    assert observation.status == "error"
                    assert observation.refusal == "backing_off"

                assert transport.call_count == spent, (
                    "the breaker was open and the service dialled anyway"
                )

    def test_a_new_service_instance_inherits_the_wall(self, app):
        """Every caller builds its own service -- the API endpoint does it per
        request -- so an instance attribute would forget on the next press."""
        with app.app_context():
            transport = Mock(
                return_value=FakeResponse(status_code=403, text=CAPTCHA_PAGE)
            )
            with patch(
                "services.listing_status_service.request_with_retries", transport
            ):
                for _ in range(ListingStatusService.REFUSAL_BREAKER_THRESHOLD):
                    ListingStatusService().observe(LISTING_URL)
                spent = transport.call_count

                assert ListingStatusService().observe(LISTING_URL).refusal == (
                    "backing_off"
                )
                assert transport.call_count == spent

    def test_backing_off_still_writes_nothing(self, app):
        """The #136 contract survives the new exit: a check that did not happen
        is not a check, so neither the status nor the date moves."""
        with app.app_context():
            prop = _property("untouched")
            with patch(
                "services.listing_status_service.request_with_retries",
                Mock(return_value=FakeResponse(status_code=403, text=CAPTCHA_PAGE)),
            ):
                service = ListingStatusService()
                for _ in range(ListingStatusService.REFUSAL_BREAKER_THRESHOLD + 2):
                    service.check_property_status(prop)

            stored = db.session.get(Property, prop.id)
            assert stored.listing_status == "active"
            assert stored.listing_status_source == "ingest"
            assert stored.listing_last_checked is None

    def test_a_listing_that_answers_keeps_the_breaker_shut(self, app):
        with app.app_context():
            transport = Mock(return_value=FakeResponse(status_code=200, text=LIVE_PAGE))
            with patch(
                "services.listing_status_service.request_with_retries", transport
            ):
                service = ListingStatusService()
                for _ in range(6):
                    assert service.observe(LISTING_URL).status == "active"
                assert transport.call_count == 6
                assert ListingStatusService.breaker.state()["open"] is False


class TestTheRefusalSaysWhichKind:
    """'blocked' is the site refusing us and will refuse the next press too;
    'timeout' is a bad moment worth retrying. Reporting both as one sentence is
    what made 76 captchas look like 76 unrelated bugs."""

    @pytest.mark.parametrize(
        "response,expected",
        [
            (FakeResponse(status_code=403, text=CAPTCHA_PAGE), "blocked"),
            (FakeResponse(status_code=200, text=CAPTCHA_PAGE), "blocked"),
            (FakeResponse(status_code=403, text="<html>denied</html>"), "blocked"),
            (FakeResponse(status_code=503, text="<html>oops</html>"), "http_error"),
            (
                FakeResponse(
                    status_code=200,
                    text=HOME_PAGE,
                    url="https://www.idealista.com/",
                ),
                "not_the_listing_page",
            ),
        ],
    )
    def test_reason(self, app, response, expected):
        with app.app_context():
            with patch(
                "services.listing_status_service.request_with_retries",
                Mock(return_value=response),
            ):
                observation = ListingStatusService().observe(LISTING_URL)
            assert observation.status == "error"
            assert observation.refusal == expected

    def test_a_timeout_is_its_own_reason(self, app):
        import requests

        with app.app_context():
            with patch(
                "services.listing_status_service.request_with_retries",
                Mock(side_effect=requests.Timeout()),
            ):
                observation = ListingStatusService().observe(LISTING_URL)
            assert observation.refusal == "timeout"

    def test_an_answer_carries_no_reason(self, app):
        with app.app_context():
            with patch(
                "services.listing_status_service.request_with_retries",
                Mock(return_value=FakeResponse(status_code=200, text=LIVE_PAGE)),
            ):
                observation = ListingStatusService().observe(LISTING_URL)
            assert observation.status == "active"
            assert observation.refusal is None

    def test_the_two_value_shape_still_works(self, app):
        """`check_listing_status` is what the sweeps and the tests unpack."""
        with app.app_context():
            with patch(
                "services.listing_status_service.request_with_retries",
                Mock(return_value=FakeResponse(status_code=200, text=LIVE_PAGE)),
            ):
                assert ListingStatusService().check_listing_status(LISTING_URL) == (
                    "active",
                    None,
                )


class TestThePageIsToldWhy:
    def test_the_endpoint_reports_the_refusal_and_the_standing_condition(
        self, app, client
    ):
        with app.app_context():
            prop = _property("api")
            property_id = prop.id

        with patch(
            "services.listing_status_service.request_with_retries",
            Mock(return_value=FakeResponse(status_code=403, text=CAPTCHA_PAGE)),
        ):
            # TESTING makes the endpoint run inline; it refuses `?sync=1` from a
            # request on purpose (issue #136), so this is not asking for it.
            for _ in range(ListingStatusService.REFUSAL_BREAKER_THRESHOLD):
                client.post(f"/api/property/{property_id}/check-status")
            payload = client.post(
                f"/api/property/{property_id}/check-status"
            ).get_json()

        assert payload["success"] is True
        assert payload["observed"] == "error"
        assert payload["refusal"] == "backing_off"
        assert payload["breaker"]["open"] is True
        assert payload["breaker"]["blocked_until"]
        # And it still reports the status it actually has, not the one it wanted.
        assert payload["status"] == "active"
        assert payload["last_checked"] is None
