"""Issue #224: an observation that contradicted the row was thrown away.

`_apply_observed_status()` stamped `listing_last_checked` and
`listing_status_source = "check"` for every non-error observation, but only
*applied* two of them: `active -> removed/sold` and `removed/sold -> active`. A
`sold -> removed` reading (a listing marked sold by hand, later taken down) fell
through both guards, so the row kept saying `sold` — permanently, since neither
guard can ever match again — while the page repainted today's date and said
"Checked on Idealista: no change", crediting the stale verdict to the very check
that contradicted it.

That is the false confirmation issue #136 removed, surviving in the one
transition it did not enumerate. The rule pinned here: every observation that
differs from the stored value is applied, and no path may leave
`listing_status_source = "check"` on a value the check disagreed with.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land, LandHistory, Property
from services.listing_status_service import ListingStatusService
from tests import setup_test_environment

LISTING_URL = "https://www.idealista.com/en/inmueble/109072919/"
REMOVED_PAGE = "<html><body>Sorry, this listing is no longer published</body></html>"
SOLD_PAGE = "<html><body>This property has been sold</body></html>"
LIVE_PAGE = (
    "<html><head>"
    '<link rel="canonical" href="https://www.idealista.com/inmueble/109072919/"/>'
    "</head><body>Precio 250.000 EUR</body></html>"
)
CAPTCHA_PAGE = (
    '<html lang="es"><head><title>idealista.com</title></head><body>'
    "<p>Please enable JS and disable any ad blocker</p>"
    '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
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


def make_property(key, **overrides):
    fields = {
        "source_email_id": f"issue-224-{key}",
        "title": "A property",
        "url": LISTING_URL,
        "listing_status": "active",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestATerminalStatusCanStillChange:
    def test_a_sold_listing_read_as_removed_is_updated(self, app):
        prop = make_property(
            "sold-then-removed",
            listing_status="sold",
            listing_status_source="manual",
        )

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "removed"
        assert result["changed"] is True, (
            "the page reports 'no change' off this flag, so a discarded "
            "observation reads as a confirmation"
        )
        assert result["previous_status"] == "sold"

    def test_a_removed_listing_read_as_sold_is_updated(self, app):
        prop = make_property("removed-then-sold", listing_status="removed")

        with with_response(FakeResponse(200, SOLD_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "sold"
        assert result["changed"] is True

    def test_the_earlier_removal_date_survives_a_restatement(self, app):
        """It left the market once. A second wording is not a second removal."""
        first_seen_gone = datetime(2026, 3, 1, tzinfo=timezone.utc)
        prop = make_property(
            "keeps-its-date",
            listing_status="sold",
            listing_removed_date=first_seen_gone,
        )

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "removed"
        assert prop.listing_removed_date.replace(tzinfo=timezone.utc) == first_seen_gone


class TestTheStampNeverContradictsTheStoredValue:
    def test_a_check_that_agrees_is_a_confirmation_not_a_change(self, app):
        prop = make_property("still-removed", listing_status="removed")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            result = ListingStatusService().check_property_status(prop)

        assert result["changed"] is False
        assert prop.listing_status == "removed"
        assert prop.listing_status_source == "check"
        assert prop.listing_last_checked is not None, (
            "an agreeing check really did verify the value, and may say so"
        )

    def test_a_check_stamped_as_the_source_always_matches_what_it_read(self, app):
        """The invariant behind the defect, stated directly."""
        for key, stored, page, expected in (
            ("a", "sold", REMOVED_PAGE, "removed"),
            ("b", "removed", SOLD_PAGE, "sold"),
            ("c", "active", REMOVED_PAGE, "removed"),
            ("d", "removed", LIVE_PAGE, "active"),
            ("e", "active", LIVE_PAGE, "active"),
        ):
            prop = make_property(f"invariant-{key}", listing_status=stored)

            with with_response(FakeResponse(200, page)):
                ListingStatusService().check_property_status(prop)

            assert prop.listing_status == expected
            assert prop.listing_status_source == "check"

    def test_a_refused_check_still_writes_nothing(self, app):
        prop = make_property(
            "blocked", listing_status="sold", listing_status_source="email"
        )

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "sold"
        assert prop.listing_status_source == "email"
        assert prop.listing_last_checked is None


class TestTheLandHistorySnapshot:
    def _land(self, key, **overrides):
        fields = {
            "source_email_id": f"issue-224-land-{key}",
            "title": "Legacy land",
            "url": LISTING_URL,
            "listing_status": "active",
            "is_favorite": True,
        }
        fields.update(overrides)
        land = Land(**fields)
        db.session.add(land)
        db.session.commit()
        return land

    def test_a_restatement_records_no_relisting(self, app):
        """`sold -> removed` is not a relisting, and the history said it was."""
        land = self._land("restated", listing_status="sold")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            ListingStatusService().check_land_status(land)

        assert land.listing_status == "removed"
        assert LandHistory.query.filter_by(land_id=land.id).count() == 0

    def test_a_real_relisting_is_still_recorded(self, app):
        land = self._land("relisted", listing_status="removed")

        with with_response(FakeResponse(200, LIVE_PAGE)):
            ListingStatusService().check_land_status(land)

        assert land.listing_status == "active"
        events = [h.change_type for h in LandHistory.query.filter_by(land_id=land.id)]
        assert events == ["relisted"]

    def test_a_real_removal_is_still_recorded(self, app):
        land = self._land("removed")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            ListingStatusService().check_land_status(land)

        assert land.listing_status == "removed"
        events = [h.change_type for h in LandHistory.query.filter_by(land_id=land.id)]
        assert events == ["removed_from_listing"]
