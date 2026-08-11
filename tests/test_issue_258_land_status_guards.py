"""Issue #258: the legacy Land status check had neither guard its twin has.

`check_property_status` is rate-limited and refuses the `?sync=1` escape hatch,
both for reasons #136 wrote down: every call spends one outbound request against
idealista from a page the owner can hold down, and a synchronous call holds a
worker for the whole fetch timeout — which, on an app with no authentication in
front of it, a handful of concurrent calls turns into an exhausted pool.

`check_land_status` had neither, against the same scraper and the same
idealista.com. These pin that both endpoints are now protected the same way.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land
from tests import setup_test_environment

LISTING_URL = "https://www.idealista.com/en/inmueble/109072919/"
LIVE_PAGE = (
    "<html><head>"
    '<link rel="canonical" href="https://www.idealista.com/inmueble/109072919/"/>'
    "</head><body>Precio 250.000 EUR</body></html>"
)


class FakeResponse:
    def __init__(self, status_code=200, text="", url=LISTING_URL):
        self.status_code = status_code
        self.text = text
        self.url = url


def with_response(response):
    return patch(
        "services.listing_status_service.request_with_retries",
        return_value=response,
    )


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
def land(app):
    land = Land(source_email_id="issue-258", title="A plot", url=LISTING_URL)
    db.session.add(land)
    db.session.commit()
    return land


def test_the_land_endpoint_is_rate_limited(app, client, land):
    """One outbound fetch per call, from a page that can be held down."""
    codes = []
    with with_response(FakeResponse(200, LIVE_PAGE)):
        for _ in range(8):
            codes.append(client.post(f"/api/land/{land.id}/check-status").status_code)

    assert 429 in codes, f"no limit kicked in: {codes}"


def test_the_land_endpoint_refuses_the_sync_escape_hatch(app):
    """The same reasoning as its Property twin: a per-IP limit bounds how often
    a client calls, not how many calls it holds open at once."""
    import inspect

    from routes.api_routes import check_land_status

    source = inspect.getsource(check_land_status)

    assert "_should_run_sync(allow_request_override=False)" in source, (
        "?sync=1 would hold a worker for the whole outbound timeout"
    )


def test_both_status_endpoints_carry_the_same_guards():
    """They scrape the same site through the same service; a guard on one and
    not the other is an accident, not a decision."""
    import inspect

    from routes import api_routes

    source = inspect.getsource(api_routes)
    for name in ("check_land_status", "check_property_status"):
        index = source.index(f"def {name}(")
        preamble = source[max(0, index - 300) : index]
        assert "@limiter.limit" in preamble, f"{name} is not rate limited"
