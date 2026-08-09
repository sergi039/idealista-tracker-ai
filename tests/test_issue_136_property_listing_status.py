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
  anything. idealista answers 403 + a DataDome captcha to the scraper today,
  and recording that as `active` with a fresh `listing_last_checked` would be a
  false confirmation -- worse than the missing check it replaces.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property
from services.listing_status_service import ListingStatusService
from tests import setup_test_environment


class FakeResponse:
    """Minimal stand-in for requests.Response as the service uses it."""

    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


LIVE_PAGE = "<html><body>Precio 250.000 EUR info-data-price</body></html>"
REMOVED_PAGE = "<html><body>Sorry, this listing is no longer published</body></html>"
SOLD_PAGE = "<html><body>Esta vivienda se ha vendido</body></html>"
# Verbatim shape of what idealista returns to the scraper right now.
CAPTCHA_PAGE = (
    '<html lang="es"><head><title>idealista.com</title></head><body>'
    "<p>Please enable JS and disable any ad blocker</p>"
    '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
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
        "url": "https://www.idealista.com/en/inmueble/109072919/",
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
            status, removed = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/inmueble/1/"
            )

        assert status == "error", (
            f"HTTP {status_code} tells us nothing about the listing; "
            "reporting 'active' would be a false confirmation"
        )
        assert removed is None

    def test_captcha_is_an_error(self, app):
        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            status, _ = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/inmueble/1/"
            )
        assert status == "error"

    def test_404_is_removed(self, app):
        with with_response(FakeResponse(404, "")):
            status, _ = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/inmueble/1/"
            )
        assert status == "removed"

    def test_200_with_a_real_page_is_active(self, app):
        with with_response(FakeResponse(200, LIVE_PAGE)):
            status, _ = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/inmueble/1/"
            )
        assert status == "active"

    def test_removal_notice_on_a_200_is_removed(self, app):
        with with_response(FakeResponse(200, REMOVED_PAGE)):
            status, _ = ListingStatusService().check_listing_status(
                "https://www.idealista.com/en/inmueble/1/"
            )
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

    def test_a_blocked_fetch_records_the_attempt_but_not_a_status(self, app):
        """The DataDome case: we tried, we learned nothing, we say so."""
        prop = make_property(key="blocked")

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["new_status"] == "error"
        assert result["changed"] is False

        stored = db.session.get(Property, prop.id)
        assert stored.listing_status == "active", "a blocked fetch must not rewrite it"
        assert stored.listing_removed_date is None
        assert stored.listing_last_checked is not None, "the attempt is still recorded"

    def test_a_blocked_fetch_does_not_resurrect_a_removed_listing(self, app):
        prop = make_property(key="stays-removed", listing_status="removed")

        with with_response(FakeResponse(503, "<html>bad gateway</html>")):
            ListingStatusService().check_property_status(prop)

        assert db.session.get(Property, prop.id).listing_status == "removed"

    def test_relisted_property_goes_back_to_active(self, app):
        prop = make_property(
            key="relisted",
            listing_status="removed",
            listing_removed_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
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


class TestTheSweep:
    def test_never_checked_rows_come_first_and_inactive_ones_are_left_alone(self, app):
        stale = make_property(
            key="stale",
            listing_last_checked=datetime.now(timezone.utc) - timedelta(days=30),
        )
        never = make_property(key="never")
        removed = make_property(key="already-removed", listing_status="removed")

        with (
            with_response(FakeResponse(200, LIVE_PAGE)),
            patch("services.listing_status_service.time.sleep"),
        ):
            results = ListingStatusService().check_all_active_properties(limit=10)

        assert results["checked"] == 2, "the removed row is not re-checked"
        assert results["active"] == 2

        checked_ids = {never.id, stale.id}
        assert all(
            db.session.get(Property, pid).listing_last_checked is not None
            for pid in checked_ids
        )
        assert db.session.get(Property, removed.id).listing_last_checked is None

    def test_recently_checked_rows_are_skipped(self, app):
        make_property(key="fresh", listing_last_checked=datetime.now(timezone.utc))

        with (
            with_response(FakeResponse(200, LIVE_PAGE)),
            patch("services.listing_status_service.time.sleep"),
        ):
            results = ListingStatusService().check_all_active_properties(
                limit=10, days_since_check=7
            )

        assert results["checked"] == 0

    def test_the_throttle_is_still_there(self, app):
        make_property(key="throttle-a")
        make_property(key="throttle-b")

        with (
            with_response(FakeResponse(200, LIVE_PAGE)),
            patch("services.listing_status_service.time.sleep") as sleep,
        ):
            ListingStatusService().check_all_active_properties(limit=10)

        assert sleep.call_count == 2, "one deliberate pause per listing fetch"
        assert all(call.args[0] >= 2 for call in sleep.call_args_list)


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

    def test_endpoint_refuses_a_property_with_no_url(self, app, client):
        prop = make_property(key="api-nourl", url=None)

        response = client.post(f"/api/property/{prop.id}/check-status")

        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_endpoint_404s_on_an_unknown_property(self, app, client):
        assert client.post("/api/property/999999/check-status").status_code == 404

    def test_page_offers_the_check_and_admits_it_never_ran(self, app, client):
        prop = make_property(key="page")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "Check status" in body
        assert "Checked: never" in body, (
            "an unverified 'active' must not read as a checked fact"
        )

    def test_page_shows_the_date_once_a_check_has_run(self, app, client):
        prop = make_property(
            key="page-checked",
            listing_last_checked=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "Checked: 2026-08-09" in body
        assert "Checked: never" not in body
