"""Where a listing's status came from, and what a blocked sweep does about it.

Measured 2026-08-10: idealista answers this machine with a DataDome block. Its
*home page* returns 403 with the same block body a listing does, with the
service's User-Agent and with a full modern Chrome header set alike, while
robots.txt returns 200. So the scraper confirms nothing today, and the two
sources that still say anything are idealista's own removal email, which the
ingester reads, and the owner setting a status by hand.

Those three are not equally trustworthy and the row recorded no difference: a
`removed` idealista mailed about and a `removed` somebody typed were the same
two bytes. `listing_status_source` is that difference, and these tests pin who
writes which value.

The second half pins the sweep: when idealista refuses, it refuses everything,
so walking the rest of the list spends one outbound request per row to learn
the same thing. The sweep stops and says it stopped.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from markupsafe import escape

from app import create_app, db
from models import Land, Property
from services.listing_status_service import ListingStatusService
from tests import setup_test_environment

LISTING_URL = "https://www.idealista.com/en/inmueble/109072919/"
CAPTCHA_PAGE = (
    '<html lang="es"><head><title>idealista.com</title></head><body>'
    "<p>Please enable JS and disable any ad blocker</p>"
    '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
)
LIVE_PAGE = (
    "<html><head>"
    '<link rel="canonical" href="https://www.idealista.com/inmueble/109072919/"/>'
    "</head><body>Precio 250.000 EUR</body></html>"
)
REMOVED_PAGE = "<html><body>Sorry, this listing is no longer published</body></html>"


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


def make_land(key, **overrides):
    fields = {
        "source_email_id": f"provenance-land-{key}",
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


def make_property(key, **overrides):
    fields = {
        "source_email_id": f"provenance-{key}",
        "title": "A property",
        "url": LISTING_URL,
        "listing_status": "active",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestWhoWritesTheSource:
    def test_a_new_row_says_it_was_only_ingested(self, app):
        """`active` on a fresh row is the default it arrived with, not an
        observation, and the column says so rather than staying silent."""
        prop = make_property("fresh")
        assert prop.listing_status_source == "ingest"
        assert prop.listing_last_checked is None

    def test_a_check_that_read_the_page_says_check(self, app):
        prop = make_property("checked")

        with with_response(FakeResponse(200, REMOVED_PAGE)):
            ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "removed"
        assert prop.listing_status_source == "check"
        assert prop.listing_last_checked is not None

    def test_a_refused_check_writes_nothing_at_all(self, app):
        """Including the source. A blocked fetch observed nothing, so it must
        not overwrite what a previous email or check established."""
        prop = make_property(
            "blocked",
            listing_status="removed",
            listing_status_source="email",
        )

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            ListingStatusService().check_property_status(prop)

        assert prop.listing_status == "removed"
        assert prop.listing_status_source == "email", "the email's claim survives"
        assert prop.listing_last_checked is None

    def test_a_hand_set_status_says_manual_and_is_not_a_check(self, app, client):
        prop = make_property("manual")

        payload = client.post(
            f"/api/property/{prop.id}/set-status", json={"status": "sold"}
        ).get_json()

        assert payload["status_source"] == "manual"
        db.session.refresh(prop)
        assert prop.listing_status == "sold"
        assert prop.listing_status_source == "manual"
        assert prop.listing_last_checked is None, (
            "nobody read the listing page, so the header must not say a check ran"
        )

    def test_the_land_endpoint_agrees(self, app, client):
        land = make_land("manual")

        payload = client.post(
            f"/api/land/{land.id}/set-status", json={"status": "removed"}
        ).get_json()

        assert payload["status_source"] == "manual"
        db.session.refresh(land)
        assert land.listing_status_source == "manual"
        assert land.listing_last_checked is None


class TestThePageShowsWhereTheStatusCameFrom:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("email", "Recorded from Idealista's own removal email."),
            ("check", "Recorded by a status check that read the listing page."),
            ("manual", "Set by hand."),
            (None, "Source not recorded."),
        ],
    )
    def test_the_banner_says_it_in_words(self, app, client, source, expected):
        prop = make_property(
            f"banner-{source}",
            listing_status="removed",
            listing_removed_date=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )
        # A column default applies on INSERT, so a row created with None still
        # arrives as 'ingest'. NULL is what a row that predates the column has,
        # and it is reached by writing it afterwards -- which is also the only
        # honest way to test the "we do not know" wording.
        prop.listing_status_source = source
        db.session.commit()

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        # The same sentence is also the badge's tooltip, so searching the whole
        # page would pass on the tooltip alone. Cut to the banner first.
        banner = body[body.index("no longer available on Idealista") :]
        banner = banner[: banner.index("</div>")]

        # Jinja escapes the apostrophe in "Idealista's", so compare what the
        # template really emits rather than the source string.
        assert str(escape(expected)) in banner

    def test_the_land_page_says_it_too(self, app, client):
        land = make_land(
            "banner",
            listing_status="removed",
            listing_status_source="email",
        )

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)
        banner = body[body.index("no longer available on Idealista") :]
        banner = banner[: banner.index("</div>")]

        assert str(escape("Recorded from Idealista's own removal email.")) in banner


class TestABlockedSweepStopsInsteadOfGrinding:
    def test_it_stops_after_three_refusals_in_a_row(self, app):
        for index in range(8):
            make_land(f"sweep-{index}")

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            with patch("services.listing_status_service.time.sleep"):
                results = ListingStatusService().check_favorites_status(limit=8)

        assert results["stopped_early"] is True
        assert results["checked"] == ListingStatusService.CONSECUTIVE_ERROR_LIMIT
        assert results["unchecked"] == 8 - ListingStatusService.CONSECUTIVE_ERROR_LIMIT

    def test_a_working_site_is_swept_to_the_end(self, app):
        for index in range(5):
            make_land(f"ok-{index}")

        with with_response(FakeResponse(200, LIVE_PAGE)):
            with patch("services.listing_status_service.time.sleep"):
                results = ListingStatusService().check_favorites_status(limit=5)

        assert results["stopped_early"] is False
        assert results["checked"] == 5
        assert results["unchecked"] == 0

    def test_one_refusal_among_answers_does_not_stop_it(self, app):
        """The counter is consecutive on purpose: a single bad fetch is noise,
        three in a row is the site saying no."""
        for index in range(4):
            make_land(f"mixed-{index}")

        answers = [
            FakeResponse(403, CAPTCHA_PAGE),
            FakeResponse(200, LIVE_PAGE),
            FakeResponse(403, CAPTCHA_PAGE),
            FakeResponse(200, LIVE_PAGE),
        ]
        with patch(
            "services.listing_status_service.request_with_retries",
            side_effect=answers,
        ):
            with patch("services.listing_status_service.time.sleep"):
                results = ListingStatusService().check_favorites_status(limit=4)

        assert results["stopped_early"] is False
        assert results["checked"] == 4

    def test_the_all_listings_sweep_stops_too(self, app):
        for index in range(6):
            make_land(f"all-{index}", is_favorite=False)

        with with_response(FakeResponse(403, CAPTCHA_PAGE)):
            with patch("services.listing_status_service.time.sleep"):
                results = ListingStatusService().check_all_active_listings(
                    limit=6, record_sync=False
                )

        assert results["stopped_early"] is True
        assert results["unchecked"] == 6 - ListingStatusService.CONSECUTIVE_ERROR_LIMIT
