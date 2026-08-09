"""One surface, every saved search on it (owner decision 2026-08-09).

The owner has two live subscriptions on idealista.com and a pile of retired
ones in the database. Before this change the app answered that with two
listing pages -- `/properties` and an archived `/lands` behind a banner -- and
a `/properties` that opened on *one* saved search, picked for them, with the
rest hidden behind a dropdown listing nine near-identical names.

What is pinned here:

* `/lands` is not a second surface any more; it redirects.
* A bare `/properties` shows every live subscription, not one of them.
* The filter offers the live subscriptions first and the retired ones under
  an archive label -- reachable, clearly not current, and never part of "all".
* When more than one subscription is on screen the rows say which one they
  came from, and the travel columns still render (they used to vanish
  entirely, which read as "no travel data" rather than "several profiles").
"""

import re

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


def _only_the_airport():
    """Preset config with one target enabled.

    A preset with no entry counts as enabled, so leaving the others out would
    give every profile six columns -- and the list renders four before it
    folds the rest into a "+N" badge, which would hide the custom target this
    file asserts on for reasons that have nothing to do with the behaviour
    under test.
    """
    presets = {"airport": {"enabled": True, "mode": "driving"}}
    for key in ("train_station", "hospital", "police", "supermarket", "school"):
        presets[key] = {"enabled": False, "mode": "driving"}
    return presets


@pytest.fixture
def subscriptions(app):
    """Two live saved searches, one retired one, and an empty catch-all.

    Modelled on the real database: `Land at Norte` and the houses search are
    what still arrives in the mail, `Legacy Lands` is the frozen mirror, and
    `Default` is the routing catch-all that has never held a listing.
    """
    with app.app_context():
        land = SearchProfile(
            name="Land at Norte",
            is_active=True,
            travel_targets={
                "presets": _only_the_airport(),
                # lat/lon are required: `normalize_travel_targets_config`
                # drops a custom target without coordinates.
                "custom": [
                    {
                        "id": "office",
                        "name": "NorteOfficeTarget",
                        "lat": 43.36,
                        "lon": -5.84,
                    }
                ],
            },
        )
        houses = SearchProfile(
            name="houses at your custom search area norte",
            is_active=True,
            travel_targets={
                "presets": _only_the_airport(),
                "custom": [],
            },
        )
        archived = SearchProfile(
            name="Legacy Lands",
            is_active=False,
            travel_targets={"presets": {}, "custom": []},
        )
        catch_all = SearchProfile(
            name="Default",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([land, houses, archived, catch_all])
        db.session.commit()

        def _make(slug, profile_id, minutes):
            prop = Property(
                source_email_id=f"unified_{slug}",
                title=f"{slug}UniqueTitle",
                search_profile_id=profile_id,
                listing_status="active",
                municipality="Cudillero",
                travel={
                    "targets": {
                        "airport": {
                            "status": "ok",
                            "mode": "driving",
                            "duration_min": minutes,
                        }
                    }
                },
            )
            db.session.add(prop)
            db.session.commit()
            return prop.id

        return {
            "land_id": land.id,
            "houses_id": houses.id,
            "archived_id": archived.id,
            "catch_all_id": catch_all.id,
            "land_listing": _make("LandListing", land.id, 21),
            "houses_listing": _make("HouseListing", houses.id, 33),
            "archived_listing": _make("ArchivedListing", archived.id, 44),
        }


def _dropdown(body):
    match = re.search(
        r'id="profile-select-menu"(.*?)</div>\s*</div>\s*</div>', body, re.S
    )
    assert match, "the subscription dropdown is missing"
    return match.group(1)


def _option_label(body, profile_id):
    match = re.search(rf'for="profile-option-{profile_id}"(.*?)</label>', body, re.S)
    return match.group(1) if match else None


class TestOneSurface:
    def test_lands_is_no_longer_a_page(self, client):
        resp = client.get("/lands")
        assert resp.status_code in (301, 302, 308)
        assert resp.headers["Location"].endswith("/properties")

    def test_the_navbar_has_no_second_listing_page(self, client, subscriptions):
        nav = client.get("/properties").get_data(as_text=True).split("</nav>", 1)[0]
        assert 'href="/lands"' not in nav
        assert 'href="/properties"' in nav

    def test_no_archive_banner_on_the_working_page(self, client, subscriptions):
        """The banner the owner did not ask for announced the page as a
        legacy table. The one surface is not an archive and must not say so --
        naming a retired *subscription* in the filter is a different thing."""
        body = client.get("/properties").get_data(as_text=True).lower()
        assert "this is the legacy" not in body
        assert "the working page since" not in body
        assert "nothing new is ingested into it" not in body


class TestEverySubscriptionIsOnIt:
    def test_a_bare_url_shows_every_live_subscription(self, client, subscriptions):
        body = client.get("/properties?per_page=100").get_data(as_text=True)
        assert "LandListingUniqueTitle" in body
        assert "HouseListingUniqueTitle" in body

    def test_all_does_not_include_the_archive(self, client, subscriptions):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        assert "ArchivedListingUniqueTitle" not in body

    def test_the_archive_is_reachable_by_id(self, client, subscriptions):
        body = client.get(
            f"/properties?profile_id={subscriptions['archived_id']}&per_page=100"
        ).get_data(as_text=True)
        assert "ArchivedListingUniqueTitle" in body
        assert "LandListingUniqueTitle" not in body

    def test_one_subscription_can_be_singled_out(self, client, subscriptions):
        body = client.get(
            f"/properties?profile_id={subscriptions['houses_id']}&per_page=100"
        ).get_data(as_text=True)
        assert "HouseListingUniqueTitle" in body
        assert "LandListingUniqueTitle" not in body


class TestTheFilterReadsLikeTheSavedSearches:
    def test_live_subscriptions_come_first(self, client, subscriptions):
        menu = _dropdown(client.get("/properties").get_data(as_text=True))
        positions = {
            key: menu.index(f'value="{subscriptions[key]}"')
            for key in ("land_id", "houses_id", "archived_id")
        }
        assert positions["land_id"] < positions["archived_id"]
        assert positions["houses_id"] < positions["archived_id"]

    def test_the_retired_one_is_labelled_archive(self, client, subscriptions):
        body = client.get("/properties").get_data(as_text=True)
        label = _option_label(body, subscriptions["archived_id"])
        assert label and "Archive" in label

    def test_a_live_subscription_is_not_labelled_archive(self, client, subscriptions):
        body = client.get("/properties").get_data(as_text=True)
        label = _option_label(body, subscriptions["land_id"])
        assert label and "Archive" not in label

    def test_each_option_says_how_many_listings_it_holds(self, client, subscriptions):
        body = client.get("/properties").get_data(as_text=True)
        assert "(1)" in _option_label(body, subscriptions["land_id"])

    def test_an_empty_catch_all_is_not_offered(self, client, subscriptions):
        """`Default` exists to route unrecognised mail, not to be filtered on.
        Offering it is an option that can only ever return an empty page."""
        body = client.get("/properties").get_data(as_text=True)
        assert _option_label(body, subscriptions["catch_all_id"]) is None

    def test_the_empty_catch_all_still_gets_a_checkbox_when_selected(
        self, client, subscriptions
    ):
        """Otherwise the page's script reads the selection as "nothing
        ticked" and the next Apply silently widens the view."""
        catch_all = subscriptions["catch_all_id"]
        body = client.get(f"/properties?profile_id={catch_all}").get_data(as_text=True)
        checkbox = re.search(
            rf'<input[^>]*name="profile_id"[^>]*value="{catch_all}"[^>]*>', body
        )
        assert checkbox and "checked" in checkbox.group(0)

    def test_no_subscription_is_offered_only_when_it_holds_something(
        self, client, subscriptions, app
    ):
        body = client.get("/properties").get_data(as_text=True)
        assert 'value="unassigned"' not in body

        with app.app_context():
            db.session.add(
                Property(
                    source_email_id="unified_orphan",
                    title="OrphanListingUniqueTitle",
                    search_profile_id=None,
                    listing_status="active",
                )
            )
            db.session.commit()

        body = client.get("/properties").get_data(as_text=True)
        assert 'value="unassigned"' in body


class TestRowsSayWhichSubscription:
    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_the_badge_shows_when_several_are_on_screen(
        self, client, subscriptions, view_type
    ):
        body = client.get(f"/properties?view_type={view_type}").get_data(as_text=True)
        assert "property-subscription-badge" in body
        assert "houses at your custom search area norte" in body

    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_the_badge_is_dropped_for_a_single_subscription(
        self, client, subscriptions, view_type
    ):
        body = client.get(
            f"/properties?profile_id={subscriptions['land_id']}&view_type={view_type}"
        ).get_data(as_text=True)
        assert "property-subscription-badge" not in body


class TestTravelSurvivesTheUnion:
    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_preset_times_still_render_across_subscriptions(
        self, client, subscriptions, view_type
    ):
        """The travel column used to be computed for a single profile only,
        so the default view -- now every subscription at once -- would have
        rendered an empty column over data that is actually there."""
        body = client.get(f"/properties?view_type={view_type}").get_data(as_text=True)
        assert "21m" in body
        assert "33m" in body

    def test_custom_targets_stay_with_their_own_subscription(
        self, client, subscriptions
    ):
        """A custom target id belongs to one profile. Naming it while several
        are on screen would label a column with a destination most rows were
        never measured against."""
        across = client.get("/properties").get_data(as_text=True)
        assert "NorteOfficeTarget" not in across

        single = client.get(
            f"/properties?profile_id={subscriptions['land_id']}"
        ).get_data(as_text=True)
        assert "NorteOfficeTarget" in single
