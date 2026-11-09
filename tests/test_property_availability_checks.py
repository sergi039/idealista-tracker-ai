"""Availability attempts survive reload; the daily sweep checks real houses."""

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import update

from app import create_app, db
from models import Property, SearchProfile
from services.listing_status_service import ListingStatusService, Observation
from services.listing_verification import read_verdict
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def listing(**values):
    profile = values.pop("profile", None)
    if profile is None:
        profile = SearchProfile(name="Availability", is_active=True)
        db.session.add(profile)
        db.session.flush()
    p = Property(
        source_email_id=f"availability-{Property.query.count()}",
        title="House",
        url=f"https://www.idealista.com/inmueble/{100 + Property.query.count()}/",
        search_profile_id=profile.id,
        is_favorite=True,
        listing_status="active",
    )
    for key, value in values.items():
        setattr(p, key, value)
    db.session.add(p)
    db.session.commit()
    return p


def test_refused_attempt_survives_reload_without_fresh_confirmation(app):
    old = datetime(2026, 8, 26)
    p = listing(
        listing_status_source="manual",
        listing_last_checked=old,
        enrichment={"cadastre": {"reference": "preserve"}},
    )
    pid = p.id
    with patch.object(
        ListingStatusService,
        "observe",
        return_value=Observation("error", None, "blocked"),
    ):
        ListingStatusService().check_property_status(p)
    db.session.remove()
    p = db.session.get(Property, pid)
    assert p.listing_status == "active"
    assert p.listing_last_checked == old
    assert p.listing_status_source == "manual"
    assert p.enrichment["cadastre"] == {"reference": "preserve"}
    verdict = read_verdict(p)
    assert verdict["attempt_refusal"] == "blocked"
    assert verdict["attempted_at"] is not None
    response = app.test_client().get(f"/properties/{pid}")
    assert response.status_code == 200
    assert b"Verification blocked" in response.data


@pytest.mark.parametrize(
    "changed",
    [
        {"url": "https://www.idealista.com/inmueble/999/"},
        {"listing_status": "removed", "listing_status_source": "manual"},
    ],
)
def test_observation_cannot_overwrite_an_edit_made_during_fetch(app, changed):
    p = listing()

    def fetched(url):
        db.session.execute(
            update(Property).where(Property.id == p.id).values(**changed),
            execution_options={"synchronize_session": False},
        )
        db.session.commit()
        return Observation("active", None)

    with patch.object(ListingStatusService, "observe", side_effect=fetched):
        result = ListingStatusService().check_property_status(p)
    db.session.refresh(p)
    for key, value in changed.items():
        assert getattr(p, key) == value
    assert p.listing_last_checked is None
    assert result["refusal"] == "superseded"


def test_success_replaces_old_refusal_and_records_confirmation(app):
    p = listing()
    with patch.object(
        ListingStatusService,
        "observe",
        return_value=Observation("error", None, "timeout"),
    ):
        ListingStatusService().check_property_status(p)
    with patch.object(
        ListingStatusService, "observe", return_value=Observation("active", None)
    ):
        ListingStatusService().check_property_status(p)
    db.session.refresh(p)
    verdict = read_verdict(p)
    assert verdict["attempt_refusal"] is None
    assert verdict["checked_at"] is not None
    assert p.listing_status_source == "check"


def test_property_favorites_sweep_skips_other_rows_and_stops_a_blocked_host(app):
    first = listing()
    second = listing(profile=first.search_profile)
    listing(profile=first.search_profile, is_favorite=False)
    listing(profile=first.search_profile, listing_status="removed")
    hidden = SearchProfile(name="Hidden", is_active=True, is_hidden=True)
    db.session.add(hidden)
    db.session.commit()
    listing(profile=hidden)
    service = ListingStatusService()
    with (
        patch.object(
            service, "observe", return_value=Observation("error", None, "blocked")
        ) as observe,
        patch("services.listing_status_service.time.sleep"),
    ):
        result = service.check_property_favorites_status(limit=30)
    assert observe.call_count == 1
    assert result["checked"] == 1 and result["unchecked"] == 1
    assert result["errors"] == 1 and result["active"] == 0
    # A later run tries the other favorite first, not the same denied URL forever.
    with (
        patch.object(
            service, "observe", return_value=Observation("active", None)
        ) as observe,
        patch("services.listing_status_service.time.sleep"),
    ):
        service.check_property_favorites_status(limit=1)
    assert observe.call_args.args[0] == second.url
    assert first.listing_last_checked is None


def test_concurrent_enrichment_is_preserved(app):
    p = listing()

    def fetched(url):
        db.session.execute(
            update(Property)
            .where(Property.id == p.id)
            .values(enrichment={"cadastre": {"reference": "arrived-during-fetch"}}),
            execution_options={"synchronize_session": False},
        )
        db.session.commit()
        return Observation("active", None)

    with patch.object(ListingStatusService, "observe", side_effect=fetched):
        ListingStatusService().check_property_status(p)
    db.session.refresh(p)
    assert p.enrichment["cadastre"]["reference"] == "arrived-during-fetch"
    assert p.enrichment["listing_status_check"]["observed"] == "active"


def test_unsupported_source_does_not_fetch_or_confirm(app):
    p = listing(url="https://example.org/house/123")
    with patch.object(ListingStatusService, "observe") as observe:
        result = ListingStatusService().check_property_status(p)
    observe.assert_not_called()
    assert result["refusal"] == "unsupported_source"
    assert p.listing_last_checked is None
    assert read_verdict(p)["attempt_refusal"] == "unsupported_source"


def test_property_status_api_returns_persistent_attempt(app):
    p = listing()
    with patch.object(
        ListingStatusService,
        "observe",
        return_value=Observation("error", None, "blocked"),
    ):
        response = app.test_client().post(f"/api/property/{p.id}/check-status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["observed"] == "error" and payload["last_checked"] is None
    assert payload["attempt"]["refusal"] == "blocked"
    assert (
        payload["attempt"]
        == db.session.get(Property, p.id).enrichment["listing_status_check"]
    )


def test_favorites_api_uses_actual_catalog_and_caps_queue(app):
    p = listing()
    listing(profile=p.search_profile, is_favorite=False)
    app.config["INGESTION_TARGET"] = "properties"
    with (
        patch.object(
            ListingStatusService, "observe", return_value=Observation("removed", None)
        ),
        patch("services.listing_status_service.time.sleep"),
    ):
        response = app.test_client().post("/api/listings/check-favorites?limit=99999")
    assert response.status_code == 200
    result = response.get_json()
    assert result["checked"] == result["removed"] == 1
    assert result["details"][0]["property_id"] == p.id
    assert db.session.get(Property, p.id).listing_status == "removed"


def test_favorites_cannot_force_synchronous_portal_work(app):
    app.config.update(TESTING=False, INGESTION_TARGET="properties")
    with (
        patch("routes.api_routes._enqueue", return_value="queued-once") as queue,
        patch.object(ListingStatusService, "observe") as observe,
    ):
        response = app.test_client().post(
            "/api/listings/check-favorites?sync=1&limit=99999"
        )
    assert response.status_code == 202
    assert response.get_json()["job_id"] == "queued-once"
    assert queue.call_args.kwargs["meta"]["limit"] == 50
    assert queue.call_args.kwargs["dedupe_key"] == "listings_check_favorites:properties"
    observe.assert_not_called()


@pytest.mark.parametrize("raw", ["9999-12-31T23:30:00+00:00", "9999-12-31T23:30:00"])
def test_unrepresentable_local_attempt_is_ignored_without_breaking_pages(app, raw):
    p = listing()
    p.enrichment = {
        "listing_status_check": {
            "url": p.url,
            "attempted_at": raw,
            "observed": "error",
            "refusal": "blocked",
        }
    }
    db.session.commit()
    verdict = read_verdict(p)
    assert verdict["attempted_at"] is None
    assert verdict["attempted_local"] is None
    assert verdict["attempt_refusal"] is None
    assert p.to_dict()["listing_status_verdict"] == "unchecked"
    assert app.test_client().get(f"/properties/{p.id}").status_code == 200
    assert (
        app.test_client().get("/properties?profile_id=all&view_type=list").status_code
        == 200
    )
