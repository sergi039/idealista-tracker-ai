"""Listing status for the surface that actually holds the listings (issue #136).

`/properties/212` was gone from idealista while the app still called it
`active`. It was not one bad row: no property had *ever* been checked, because
`ListingStatusService` only knew `Land` and the scheduled job returns early
whenever `INGESTION_TARGET` is anything but `lands` (the default is
`properties`).

Two contracts are pinned here.

* `Property` rows can be checked, one at a time from the page and in a
  throttled sweep, with the same status transitions `Land` gets.
* A fetch that did not actually show us the listing is **not** evidence of
  anything -- and is not recorded as a check either. idealista answers 403 plus
  a DataDome captcha to the scraper today; storing that as `active` with a
  fresh `listing_last_checked` would be a false confirmation, worse than the
  missing check it replaces.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property
from services.listing_status_service import ListingStatusService
from tests import setup_test_environment

LISTING_ID = "109072919"
LISTING_URL = f"https://www.idealista.com/en/inmueble/{LISTING_ID}/"


class FakeResponse:
    """Minimal stand-in for requests.Response as the service uses it.

    `url` is the *final* URL after redirects, which is how the service tells a
    listing page apart from the home page idealista sometimes sends instead.
    """

    def __init__(self, status_code=200, text="", url=LISTING_URL):
        self.status_code = status_code
        self.text = text
        self.url = url


LIVE_PAGE = (
    "<html><head>"
    f'<link rel="canonical" href="https://www.idealista.com/inmueble/{LISTING_ID}/"/>'
    "</head><body>Precio 250.000 EUR</body></html>"
)
REMOVED_PAGE = "<html><body>Sorry, this listing is no longer published</body></html>"
SOLD_PAGE = "<html><body>Esta vivienda se ha vendido</body></html>"
# Verbatim shape of what idealista returns to the scraper right now.
CAPTCHA_PAGE = (
    '<html lang="es"><head><title>idealista.com</title></head><body>'
    "<p>Please enable JS and disable any ad blocker</p>"
    '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
)
# A 200 that is not the listing: idealista's front page after a redirect.
HOME_PAGE = (
    "<html><head><title>idealista</title></head>"
    "<body>Casas y pisos, alquiler y venta. Precio</body></html>"
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


def make_property(**overrides):
    fields = {
        "source_email_id": f"issue136-{overrides.pop('key', 'default')}",
        "title": "Land plot in Camino Ania Nn, Las Regueras",
        "url": LISTING_URL,
        "listing_status": "active",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def with_response(response):
    """Patch the outbound fetch the service performs."""
    return patch(
        "services.listing_status_service.request_with_retries",
        return_value=response,
    )


class TestWhatCountsAsEvidence:
    """check_listing_status must not turn a failed fetch into a live listing."""

    @pytest.mark.parametrize("status_code", [403, 429, 500, 502, 503])
    def test_non_200_without_known_markers_is_an_error_not_active(
        self, app, status_code
    ):
        with with_response(FakeResponse(status_code, "<html><body>nope</body></html>")):
            status, removed = ListingStatusService().check_listing_status(LISTING_URL)

        assert status == "error", (
            f"HTTP {status_code} tells us nothing about the listing; "
            "reporting 'active' would be a false confirmation"
        )
        assert removed is None

    def test_a_removal_notice_on_an_error_page_is_not_a_removal(self, app):
        """A 403 body is not the advertiser speaking, whatever words it holds."""
        with with_response(FakeResponse(403, REMOVED_PAGE)):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)

        assert status == "error"

    @pytest.mark.parametrize("status_code", [200, 403])
    def test_captcha_is_an_error_whatever_code_carries_it(self, app, status_code):
        with with_response(FakeResponse(status_code, CAPTCHA_PAGE)):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)
        assert status == "error"

    def test_a_200_that_is_not_the_listing_page_is_an_error(self, app):
        """Redirected to the home page with a good status code."""
        with with_response(
            FakeResponse(200, HOME_PAGE, url="https://www.idealista.com/")
        ):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)

        assert status == "error", "a 200 from somewhere else proves nothing"

    def test_a_200_on_the_listing_page_is_active(self, app):
        with with_response(FakeResponse(200, LIVE_PAGE)):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)
        assert status == "active"

    def test_a_same_listing_redirect_still_counts(self, app):
        """Language/slug variants keep the id, and that is what we anchor on."""
        with with_response(
            FakeResponse(
                200,
                "<html><body>Precio</body></html>",
                url=f"https://www.idealista.com/inmueble/{LISTING_ID}/es/",
            )
        ):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)
        assert status == "active"

    def test_a_url_with_no_listing_id_falls_back_to_the_status_code(self, app):
        with with_response(FakeResponse(200, "<html><body>whatever</body></html>")):
            status, _ = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/some-other-shape/"
            )
        assert status == "active"

    def test_a_longer_id_starting_with_ours_is_a_different_listing(self, app):
        """Substring matching would accept /inmueble/1092919991/ for ours."""
        with with_response(
            FakeResponse(
                200,
                "<html><body>Precio</body></html>",
                url=f"https://www.idealista.com/inmueble/{LISTING_ID}9/",
            )
        ):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)

        assert status == "error"

    def test_a_page_merely_echoing_our_url_is_not_the_listing(self, app):
        """An error page that quotes the URL is not the advertiser's page."""
        with with_response(
            FakeResponse(
                200,
                f"<html><body><p>Could not load /inmueble/{LISTING_ID}/</p></body></html>",
                url="https://www.idealista.com/error/",
            )
        ):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)

        assert status == "error", "the body can say anything; the final URL cannot"

    def test_404_is_removed(self, app):
        with with_response(FakeResponse(404, "")):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)
        assert status == "removed"

    def test_removal_notice_on_a_200_is_removed(self, app):
        with with_response(FakeResponse(200, REMOVED_PAGE)):
            status, _ = ListingStatusService().check_listing_status(LISTING_URL)
        assert status == "removed"

    def test_empty_url_is_an_error(self, app):
        status, removed = ListingStatusService().check_listing_status("")
        assert (status, removed) == ("error", None)


class TestCheckingOneProperty:
    def test_removed_listing_is_recorded_with_a_date(self, app):
        prop = make_property(key="removed")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["success"] is True
        assert result["property_id"] == prop.id
        assert result["previous_status"] == "active"
        assert result["new_status"] == "removed"
        assert result["changed"] is True

        stored = db.session.get(Property, prop.id)
        assert stored.listing_status == "removed"
        assert stored.listing_removed_date is not None
        assert stored.listing_last_checked is not None

    def test_sold_listing_is_recorded(self, app):
        prop = make_property(key="sold")

        with with_response(FakeResponse(200, SOLD_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["new_status"] == "sold"
        assert db.session.get(Property, prop.id).listing_status == "sold"

    def test_a_blocked_fetch_is_not_recorded_as_a_check(self, app):
        """The DataDome case: we tried, we learned nothing, we write nothing.

        Not even listing_last_checked -- a date there would make the page read
        "Status: active, Checked: today" about a listing nobody verified.
        """
        prop = make_property(key="blocked")

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["new_status"] == "error"
        assert result["changed"] is False

        stored = db.session.get(Property, prop.id)
        assert stored.listing_status == "active", "a blocked fetch must not rewrite it"
        assert stored.listing_removed_date is None
        assert stored.listing_last_checked is None, (
            "a failed fetch is not a check and must not look like one"
        )

    def test_a_failed_fetch_does_not_refresh_an_earlier_real_check(self, app):
        earlier = datetime(2026, 7, 1, 9, 0)
        prop = make_property(key="keeps-date", listing_last_checked=earlier)

        with with_response(FakeResponse(503, "<html>bad gateway</html>")):
            ListingStatusService().check_property_status(prop)

        assert db.session.get(Property, prop.id).listing_last_checked == earlier

    def test_a_blocked_fetch_does_not_resurrect_a_removed_listing(self, app):
        prop = make_property(key="stays-removed", listing_status="removed")

        with with_response(FakeResponse(503, "<html>bad gateway</html>")):
            ListingStatusService().check_property_status(prop)

        assert db.session.get(Property, prop.id).listing_status == "removed"

    def test_a_captcha_page_does_not_resurrect_a_removed_listing(self, app):
        """The 200-with-captcha shape, which must not read as a relist."""
        prop = make_property(
            key="captcha-relist",
            listing_status="removed",
            listing_removed_date=datetime(2026, 7, 1),
        )

        with with_response(FakeResponse(200, CAPTCHA_PAGE)):
            ListingStatusService().check_property_status(prop)

        stored = db.session.get(Property, prop.id)
        assert stored.listing_status == "removed"
        assert stored.listing_removed_date is not None

    def test_relisted_property_goes_back_to_active(self, app):
        prop = make_property(
            key="relisted",
            listing_status="removed",
            listing_removed_date=datetime(2026, 7, 1),
        )

        with with_response(FakeResponse(200, LIVE_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["changed"] is True
        stored = db.session.get(Property, prop.id)
        assert stored.listing_status == "active"
        assert stored.listing_removed_date is None

    def test_property_without_a_url_is_refused_not_guessed(self, app):
        prop = make_property(key="nourl", url=None)

        result = ListingStatusService().check_property_status(prop)

        assert result["success"] is False
        assert result["property_id"] == prop.id
        assert db.session.get(Property, prop.id).listing_last_checked is None


class TestTheApiAndThePage:
    def test_check_status_endpoint_reports_the_observation(self, app, client):
        prop = make_property(key="api-removed")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            response = client.post(f"/api/property/{prop.id}/check-status")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["success"] is True
        assert payload["status"] == "removed"
        assert payload["observed"] == "removed"
        assert payload["changed"] is True
        assert payload["last_checked"] is not None

    def test_endpoint_separates_the_observation_from_the_stored_status(
        self, app, client
    ):
        prop = make_property(key="api-blocked")

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            payload = client.post(f"/api/property/{prop.id}/check-status").get_json()

        assert payload["observed"] == "error", "what idealista actually gave us"
        assert payload["status"] == "active", "what we still know, unchanged"
        assert payload["changed"] is False
        assert payload["last_checked"] is None

    def test_the_endpoint_is_rate_limited(self, app, client):
        """Unauthenticated + one outbound fetch per call needs a cap."""
        prop = make_property(key="api-ratelimit")

        codes = []
        with with_response(FakeResponse(200, LIVE_PAGE)):
            for _ in range(8):
                codes.append(
                    client.post(f"/api/property/{prop.id}/check-status").status_code
                )

        assert 429 in codes, f"no limit kicked in: {codes}"

    def test_the_sync_escape_hatch_is_refused_for_this_endpoint(self, app):
        """?sync=1 would hold a worker for the full 15s fetch timeout.

        The per-IP rate limit bounds how often a client calls, not how many
        calls it holds open at once, so the override is off here while the rest
        of the API keeps it.
        """
        from routes.api_routes import _should_run_sync

        with app.test_request_context("/api/property/1/check-status?sync=1"):
            app.config["TESTING"] = False
            try:
                assert _should_run_sync() is True, "other endpoints keep the hatch"
                assert _should_run_sync(allow_request_override=False) is False
            finally:
                app.config["TESTING"] = True

    def test_endpoint_refuses_a_property_with_no_url(self, app, client):
        prop = make_property(key="api-nourl", url=None)

        response = client.post(f"/api/property/{prop.id}/check-status")

        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_endpoint_404s_on_an_unknown_property(self, app, client):
        assert client.post("/api/property/999999/check-status").status_code == 404

    def test_page_offers_the_check(self, app, client):
        prop = make_property(key="page")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert 'id="check-listing-status-btn"' in body
        assert "Check status on Idealista" in body

    def test_page_makes_no_claim_about_an_unverified_status(self, app, client):
        """This is what became of "Checked: never" (owner decision, 2026-08-09).

        #136's page-side fix was that qualifier standing beside "Status:
        active", so an ingest default could not read as an observation. The
        owner removed the whole metadata line -- ID, Idealista id, Status,
        Checked, Profile -- and the claim went with the qualifier: the page no
        longer states an active listing's status at all, so there is nothing
        left to mistake for a fact. The endpoint contract above is where #136
        actually lives, and it is untouched.
        """
        for key, checked in (
            ("page-unverified", None),
            ("page-verified", datetime(2026, 8, 9, 12, 0)),
        ):
            prop = make_property(key=key, listing_last_checked=checked)
            body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
            assert "Status: active" not in body
            assert "Checked:" not in body

    def test_a_status_that_was_observed_still_shows(self, app, client):
        """What survived the line: `removed` and `sold` are only ever written
        by a check, so the page still says them -- in the badge and in the
        banner."""
        prop = make_property(key="page-removed", listing_status="removed")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "REMOVED" in body
        assert "This listing is no longer available on Idealista" in body


class TestTheLandEndpointHasTheSameContract:
    """The legacy `/lands/<id>` page gained the same "Check status" control
    (2026-08-09), and its endpoint had no `observed` field: a blocked fetch
    came back `success: true, changed: false`, which the page read as "No
    Change". That is the #136 defect in the other half of the app -- a check
    that never reached the listing, reported as a confirmation.
    """

    def _make_land(self, key, **overrides):
        from models import Land

        fields = {
            "source_email_id": f"issue136-land-{key}",
            "title": "Legacy land",
            "url": LISTING_URL,
            "listing_status": "active",
        }
        fields.update(overrides)
        land = Land(**fields)
        db.session.add(land)
        db.session.commit()
        return land

    def test_it_separates_the_observation_from_the_stored_status(self, app, client):
        land = self._make_land("blocked")

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            payload = client.post(f"/api/land/{land.id}/check-status").get_json()

        assert payload["observed"] == "error", "what idealista actually gave us"
        assert payload["status"] == "active", "what we still know, unchanged"
        assert payload["changed"] is False

    def test_it_reports_a_real_observation(self, app, client):
        land = self._make_land("removed")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            payload = client.post(f"/api/land/{land.id}/check-status").get_json()

        assert payload["observed"] == "removed"
        assert payload["status"] == "removed"
        assert payload["changed"] is True

    def test_the_page_says_could_not_verify_rather_than_no_change(self, app, client):
        """The template must read `observed`, not just `success`."""
        land = self._make_land("page")

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)
        check = body[body.index("async function checkListingStatus") :]

        assert "result.observed === 'error'" in check
        assert "Could not verify" in check
