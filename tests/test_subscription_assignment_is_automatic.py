"""A listing's subscription is assigned automatically, never by hand.

Owner decision, 2026-08-09. `Property.search_profile_id` records which saved
search sent the alert email, and `services/property_imap_service.py` writes it
on every ingest. Two other writers used to compete with that:

* `ProfileAssignmentService.assign_nearest_profile`, run from enrichment behind
  `AUTO_PROFILE_ASSIGNMENT`, which reassigned a property to whichever active
  profile had the geographically nearest custom target -- silently discarding
  the subscription the email actually came from;
* a "Profile assignment" form on the property detail page, whose only real
  function was to set `manual_override` and thereby *stop* the heuristic above.

Both are gone, along with the flag, the service, the POST route behind the form
and the stored `enrichment.profile_assignment` metadata. Ingestion is the only
writer left. These tests pin that, because the failure mode is silent -- a
property quietly filed under the wrong saved search still renders perfectly.
"""

import importlib
import re
from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


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
def property_from_the_far_subscription(app):
    """A property whose email subscription is *not* the nearest profile.

    "Land at Norte" sits 300 km away from the listing; "Coast" has a custom
    target 1 km from it. The email says Norte, so Norte is the answer -- the
    geo heuristic would have said Coast.
    """
    with app.app_context():
        norte = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {},
                "custom": [{"id": "n1", "name": "Oviedo", "lat": 43.36, "lon": -5.84}],
            },
        )
        coast = SearchProfile(
            name="Coast",
            is_active=True,
            travel_targets={
                "presets": {},
                "custom": [{"id": "c1", "name": "Denia", "lat": 38.84, "lon": 0.11}],
            },
        )
        db.session.add_all([norte, coast])
        db.session.commit()

        prop = Property(
            source_email_id="subscription_sync_1",
            title="PlotNearDenia",
            municipality="Denia",
            search_profile_id=norte.id,
            listing_status="active",
            property_category="land",
            property_subtype="plot",
            price=120000,
            location_lat=38.845,
            location_lon=0.105,
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id, norte.id, coast.id


class _NoopLocationService:
    def ensure_coordinates(self, prop, refresh=False, *, commit=False):
        return True


class _NoopTravelService:
    def calculate_for_property(self, prop, commit=False):
        return True


class _NoopScoringService:
    def calculate_for_property(self, prop, commit=False):
        return True


class _NoopSeaDistanceService:
    def update_property(self, prop, *, commit=False):
        return None


class _NoopEnrichmentService:
    """Stands in for the OSM amenity half of `EnrichmentService`.

    Both this and the sea-distance stub above are injected so the test stays
    offline: the real collaborators query overpass-api.de, and a suite that
    reaches a live third-party endpoint fails for reasons that have nothing to
    do with what it asserts.
    """

    def enrich_osm_amenities(self, prop, *, commit=False):
        return None


def test_the_geo_heuristic_is_gone_entirely():
    """Not merely disabled: the module, the flag and the route do not exist.

    A flag defaulting to off can be flipped back by an `.env` line or a stray
    `Config.AUTO_PROFILE_ASSIGNMENT = True` in a test; a deleted module cannot.
    """
    import config as config_module

    reloaded = importlib.reload(config_module)
    assert not hasattr(reloaded.Config, "AUTO_PROFILE_ASSIGNMENT")

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.profile_assignment_service")


def test_the_manual_override_route_is_gone(app):
    """`main.set_property_profile_form` must not resolve any more.

    A template still calling `url_for` on it would raise BuildError at render
    time, which is exactly how a stale caller announces itself.
    """
    rules = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "main.set_property_profile_form" not in rules
    assert not [
        str(rule) for rule in app.url_map.iter_rules() if str(rule).endswith("/profile")
    ]


def test_enrichment_keeps_the_subscription_the_email_assigned(
    app, property_from_the_far_subscription
):
    """The real contract: enrichment must not refile a property by distance."""
    from services.property_enrichment_service import PropertyEnrichmentService

    property_id, norte_id, coast_id = property_from_the_far_subscription

    with app.app_context():
        prop = db.session.get(Property, property_id)
        PropertyEnrichmentService(
            location_service=_NoopLocationService(),
            travel_service=_NoopTravelService(),
            scoring_service=_NoopScoringService(),
            sea_distance_service=_NoopSeaDistanceService(),
            enrichment_service=_NoopEnrichmentService(),
            # Offline for the same reason as the two stubs above: the
            # sea-view step (#299) reaches Overpass/OpenTopoData through
            # services/sea_view_service.py.
            sea_view_calculator=lambda prop, commit=False, use_ai=True: None,
        ).enrich_property(prop, recalc_scoring=False)

        refreshed = db.session.get(Property, property_id)
        assert refreshed.search_profile_id == norte_id, (
            "enrichment refiled the property under the geographically nearest "
            "profile, discarding the saved search its alert email came from"
        )
        assert refreshed.search_profile_id != coast_id

        enrichment = (
            refreshed.enrichment if isinstance(refreshed.enrichment, dict) else {}
        )
        assert "profile_assignment" not in enrichment, (
            "the geo heuristic ran and stamped its metadata"
        )


def test_property_detail_offers_no_manual_profile_control(
    client, property_from_the_far_subscription
):
    """No profile picker, no save button, no form posting to the override route."""
    property_id, _, _ = property_from_the_far_subscription

    response = client.get(f"/properties/{property_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert "Profile assignment" not in body
    assert 'name="profile_id"' not in body
    assert f"/properties/{property_id}/profile" not in body


def test_no_template_anywhere_still_calls_the_deleted_endpoint(app):
    """Every template, not just the one this suite renders.

    Checking the rendered detail page proves nothing about a partial, a macro
    or a second page that still holds `url_for("main.set_property_profile_form")`
    -- that raises BuildError only when its own branch renders, which a test
    suite can easily never reach. So the templates are scanned as text, and the
    endpoint names they reference are checked against the live URL map.
    """
    templates = Path(__file__).parent.parent / "templates"
    referenced = set()
    offenders = []

    for path in sorted(templates.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        if "set_property_profile_form" in source:
            offenders.append(str(path.relative_to(templates.parent)))
        referenced.update(
            re.findall(r"url_for\(\s*['\"]([a-zA-Z_][\w.]*)['\"]", source)
        )

    assert not offenders, f"templates still calling the deleted route: {offenders}"

    known = {rule.endpoint for rule in app.url_map.iter_rules()}
    unresolvable = sorted(name for name in referenced if name not in known)
    assert not unresolvable, (
        f"templates reference endpoints that do not exist: {unresolvable}"
    )
