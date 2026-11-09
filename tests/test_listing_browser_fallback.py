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
        response = app.test_client().post(f"/api/property/{pid}/check-status")
    assert response.status_code == 200
    assert browser.call_count == 1
    result = response.get_json()
    assert result["observed"] == "active" and result["refusal"] is None
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
        result = ListingStatusService().check_property_status(
            p, allow_browser_fallback=True
        )
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
        result = ListingStatusService().check_property_status(
            p, allow_browser_fallback=True
        )
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
        result = ListingStatusService().check_property_status(
            p, allow_browser_fallback=True
        )
    assert result["refusal"] == "superseded"
    db.session.expire_all()
    assert p.listing_status == "removed" and p.listing_status_source == "manual"


def test_conflicting_browser_notice_cannot_hide_a_live_property(app):
    p = property_row()
    old = p.listing_last_checked

    def answer(url, nonce):
        page = browser_page(url, nonce)
        page.update(removed_notice=True, reference="12345")
        return page

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
        result = ListingStatusService().check_property_status(
            p, allow_browser_fallback=True
        )
    assert (
        result["new_status"] == "error"
        and result["refusal"] == "availability_ambiguous"
    )
    db.session.expire_all()
    assert (
        p.listing_status == "active"
        and p.listing_status_source == "manual"
        and p.listing_last_checked == old
    )


def test_unreadable_listing_does_not_pause_healthy_browser_host(app):
    url = "https://www.idealista.com/inmueble/12345/"

    def answer(url, nonce):
        return {"nonce": nonce, "refusal": "not_the_listing_page"}

    with patch(
        "services.subscription_transport.read_listing_status", side_effect=answer
    ) as browser:
        for _ in range(4):
            assert (
                ListingStatusService()._browser_fallback(url)[0].refusal
                == "not_the_listing_page"
            )
    assert browser.call_count == 4
    assert (
        ListingStatusService.browser_breakers.for_url(url).state()[
            "consecutive_refusals"
        ]
        == 0
    )


def test_favorite_batch_keeps_refused_idealista_off_both_transports(app, monkeypatch):
    # Merging #613 initially re-enabled owner Chrome for a refused batch row.
    monkeypatch.setattr(
        ListingStatusService, "REFUSED_SOURCES", frozenset({"idealista"})
    )
    from models import SearchProfile

    profile = SearchProfile(name="Browser favorites", is_active=True, is_hidden=False)
    first = property_row()
    db.session.add(profile)
    db.session.flush()
    first.search_profile_id = profile.id
    first.is_favorite = True
    first.listing_last_checked = None  # both NULL: ID order puts the bad row first
    second = Property(
        source_email_id="browser-second",
        url="https://www.idealista.com/inmueble/23456/",
        title="Second",
        search_profile_id=profile.id,
        is_favorite=True,
        listing_status="active",
    )
    db.session.add(second)
    db.session.commit()

    with (
        patch("services.listing_status_service.request_with_retries") as http,
        patch(
            "services.subscription_transport.read_listing_status",
            side_effect=browser_page,
        ) as browser,
        patch("services.listing_status_service.time.sleep"),
    ):
        result = ListingStatusService().check_property_favorites_status()
    http.assert_not_called()
    browser.assert_not_called()
    assert result["checked"] == result["errors"] == result["unchecked"] == 1
    assert result["stopped_early"]
    assert result["details"][0]["refusal"] == "blocked"
    assert first.listing_status_source == "manual"
    assert first.listing_last_checked is None


def test_public_index_matches_fall_back_without_network_or_refreshing_primary(
    app, monkeypatch
):
    from datetime import timezone
    from services.alternate_publication import record_publication
    from services.listing_verification import read_verdict

    # Most scraper tests disable the standing source stop to exercise parsing.
    monkeypatch.setattr(ListingStatusService, "REFUSED_SOURCES", {"idealista"})
    p = property_row()
    observed_at = datetime.now(timezone.utc)
    yae = "https://www.yaencontre.com/venta/casa/inmueble-4321-12345"
    foto = "https://www.fotocasa.es/es/comprar/vivienda/somewhere/garage/98765/d"
    for link in (yae, foto):
        assert record_publication(p, p.url, link, observed_at.isoformat(), observed_at)

    with (
        patch(
            "services.subscription_transport.read_listing_status",
            side_effect=AssertionError("unexpected Chrome"),
        ),
        patch(
            "services.listing_status_service.request_with_retries",
            side_effect=AssertionError("unexpected HTTP"),
        ),
    ):
        client = app.test_client()
        response = client.post(f"/api/property/{p.id}/check-status")
        assert response.status_code == 200
        outcome = response.get_json()
        assert outcome["observed"] == "error"
        assert outcome["refusal"] == "blocked"
        assert outcome["attempt"]["method"] == "alternate_publication"
        db.session.refresh(p)
        assert p.listing_status == "active"
        assert p.listing_status_source == "manual"
        assert p.listing_last_checked == datetime(2026, 8, 26)
        assert p.enrichment["cadastre"] == {"reference": "preserve"}
        assert {item["url"] for item in read_verdict(p)["alternate_publications"]} == {
            yae,
            foto,
        }
        rendered = client.get(f"/properties/{p.id}").get_data(as_text=True)
        assert (
            "Listed for sale in public index:" in rendered
            and yae in rendered
            and foto in rendered
        )


def test_publication_writer_refuses_wrong_subject_and_stale_evidence(app):
    from datetime import timedelta, timezone
    from services.alternate_publication import current_publications, record_publication

    p = property_row()
    now = datetime.now(timezone.utc)
    yae = "https://www.yaencontre.com/venta/casa/inmueble-4321-12345"
    with pytest.raises(ValueError, match="listing ID differs"):
        record_publication(
            p, p.url, yae.replace("12345", "99999"), now.isoformat(), now
        )
    with pytest.raises(ValueError, match="24 hours"):
        record_publication(p, p.url, yae, (now - timedelta(days=2)).isoformat(), now)
    assert record_publication(p, p.url, yae, now.isoformat(), now)
    assert not record_publication(p, p.url, yae, now.isoformat(), now)
    assert not current_publications(p, now + timedelta(days=2))
    p.listing_status = "removed"
    assert not current_publications(p, now)
    db.session.commit()
    with pytest.raises(ValueError, match="removed/sold"):
        record_publication(p, p.url, yae, now.isoformat(), now)
