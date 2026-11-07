"""A refused HTTP check must use a real, fresh browser observation when enabled.

Before #610 the real Property writer returned error without invoking any second
reader. The successful browser response is mocked only at the host transport.
"""

from datetime import datetime
from unittest.mock import patch
import pytest
from app import create_app, db
from config import Config
from models import Property
from services.listing_status_service import ListingStatusService, Observation
from tests import setup_test_environment


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    monkeypatch.setattr(Config, "LISTING_BROWSER_FALLBACK_ENABLED", True, raising=False)
    app = create_app(testing=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def property_row():
    p = Property(
        source_email_id="browser-proof",
        url="https://www.idealista.com/inmueble/12345/",
        title="House",
        listing_status="active",
        listing_status_source="manual",
        listing_last_checked=datetime(2026, 8, 26),
        enrichment={"cadastre": {"reference": "preserve"}},
    )
    db.session.add(p)
    db.session.commit()
    return p


def browser_page(url, nonce):
    from services.listing_browser import navigation_url

    wanted = navigation_url(url, nonce)
    return {
        "url": wanted,
        "archive_url": wanted,
        "main_url": wanted,
        "saved": True,
        "canonical": url,
        "nonce": nonce,
        "ready": "complete",
        "h1": "Casa independiente",
        "price": "285.000 €",
        "reader_elapsed_ms": 1,
    }


def test_http_refusal_then_browser_success_confirms_and_survives_reload(app):
    p = property_row()
    pid = p.id
    with (
        patch.object(
            ListingStatusService,
            "observe",
            return_value=Observation("error", None, "blocked"),
        ),
        patch(
            "services.subscription_transport.read_listing_status",
            create=True,
            side_effect=browser_page,
        ) as browser,
    ):
        result = ListingStatusService().check_property_status(p)
    assert browser.call_count == 1
    assert result["new_status"] == "active" and result["refusal"] is None
    db.session.remove()
    p = db.session.get(Property, pid)
    assert p.listing_status_source == "check" and p.listing_last_checked > datetime(
        2026, 8, 26
    )
    assert p.enrichment["cadastre"] == {"reference": "preserve"}
    assert p.enrichment["listing_status_check"]["method"] == "browser_archive"
    assert p.enrichment["listing_status_check"]["http_refusal"] == "blocked"
    html = app.test_client().get(f"/properties/{pid}")
    assert html.status_code == 200 and b"Verification blocked</span>" not in html.data


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "wrong"},
        {"canonical": "https://www.idealista.com/inmueble/99999/"},
        {"ready": "loading"},
        {"blocked": True},
        {"saved": False},
        {"archive_url": "https://www.idealista.com/inmueble/12345/"},
        {"main_url": "https://www.idealista.com/inmueble/99999/"},
        {"reader_elapsed_ms": 100000},
    ],
)
def test_browser_cannot_turn_unproven_page_into_confirmation(app, changes):
    p = property_row()
    old = p.listing_last_checked

    def answer(url, nonce):
        page = browser_page(url, nonce)
        page.update(changes)
        return page

    with (
        patch.object(
            ListingStatusService,
            "observe",
            return_value=Observation("error", None, "blocked"),
        ),
        patch(
            "services.subscription_transport.read_listing_status",
            create=True,
            side_effect=answer,
        ),
    ):
        result = ListingStatusService().check_property_status(p)
    assert result["new_status"] == "error"
    assert p.listing_last_checked == old and p.listing_status_source == "manual"


@pytest.mark.parametrize(
    "remote_time", ["2026-11-07T01:00:00Z", "2026-11-07T21:00:00Z"]
)
def test_wall_clock_offset_does_not_reject_a_fresh_network_navigation(app, remote_time):
    p = property_row()

    def answer(url, nonce):
        page = browser_page(url, nonce)
        page["observed_at"] = remote_time
        return page

    with (
        patch.object(
            ListingStatusService,
            "observe",
            return_value=Observation("error", None, "blocked"),
        ),
        patch(
            "services.subscription_transport.read_listing_status",
            create=True,
            side_effect=answer,
        ),
    ):
        result = ListingStatusService().check_property_status(p)
    assert result["new_status"] == "active"


def test_disabled_fallback_never_calls_host_bridge(app, monkeypatch):
    monkeypatch.setattr(Config, "LISTING_BROWSER_FALLBACK_ENABLED", False)
    with patch("services.subscription_transport.read_listing_status") as browser:
        assert ListingStatusService()._browser_fallback(
            "https://www.idealista.com/inmueble/12345/"
        ) == (None, None)
    browser.assert_not_called()


@pytest.mark.parametrize(
    "reason", ["browser_busy", "missing_configuration", "browser_unavailable"]
)
def test_unavailable_browser_does_not_poison_portal_breaker(app, reason):
    url = "https://www.idealista.com/inmueble/12345/"

    def answer(url, nonce):
        return {"nonce": nonce, "refusal": reason}

    with patch(
        "services.subscription_transport.read_listing_status", side_effect=answer
    ) as browser:
        for _ in range(4):
            observed, _ = ListingStatusService()._browser_fallback(url)
            assert observed.refusal == reason
    assert browser.call_count == 4
    assert (
        ListingStatusService.browser_breakers.for_url(url).state()[
            "consecutive_refusals"
        ]
        == 0
    )


def test_browser_refusal_has_its_own_shared_cooldown(app):
    url = "https://www.idealista.com/inmueble/12345/"

    def answer(url, nonce):
        return {"nonce": nonce, "refusal": "blocked"}

    with patch(
        "services.subscription_transport.read_listing_status", side_effect=answer
    ) as browser:
        for _ in range(3):
            assert ListingStatusService()._browser_fallback(url)[0].refusal == "blocked"
        assert ListingStatusService()._browser_fallback(url)[0].refusal == "backing_off"
    assert browser.call_count == 3
    assert not ListingStatusService.breakers.for_url(url).state()["open"]


def test_owner_edit_during_browser_fetch_supersedes_observation(app):
    from sqlalchemy import update

    p = property_row()

    def answer(url, nonce):
        db.session.execute(
            update(Property)
            .where(Property.id == p.id)
            .values(listing_status="removed", listing_status_source="manual"),
            execution_options={"synchronize_session": False},
        )
        db.session.commit()
        return browser_page(url, nonce)

    with (
        patch.object(
            ListingStatusService,
            "observe",
            return_value=Observation("error", None, "blocked"),
        ),
        patch(
            "services.subscription_transport.read_listing_status", side_effect=answer
        ),
    ):
        result = ListingStatusService().check_property_status(p)
    assert result["refusal"] == "superseded"
    db.session.expire_all()
    assert p.listing_status == "removed" and p.listing_status_source == "manual"
