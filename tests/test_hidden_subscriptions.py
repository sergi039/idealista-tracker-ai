"""Hiding a subscription takes it off the screens and changes nothing else.

Owner request, 2026-08-17. `is_active` already retires a saved search: it
leaves the chips on /properties and moves into the *Archive* section of the
subscription menu, still one tick away, because a search that stopped still
holds listings worth reaching. Production has fourteen subscriptions and
eleven of them are active -- three created by the ingester, holding one
listing each -- so every one of them takes a chip on the one working page.

`is_hidden` is the answer to that, and it is deliberately narrower than it
looks:

* the subscription is not offered anywhere -- no chip, no menu entry, not
  under *Archive* -- and its listings are out of `profile_id=all`, so /map
  and the CSV export drop them along with the page;
* it is still reachable. /profiles lists it, and `profile_id=<id>` still
  renders it with its own checkbox in the menu -- a selected id with no
  checkbox reads as "nothing ticked" to the page's own script, and the next
  Apply would silently widen the view;
* ingestion never sees the flag. A hidden subscription keeps matching its own
  emails, because routing them into the catch-all instead would be a data
  change wearing a UI change's clothes;
* the catch-all cannot be hidden at all: it receives every unmatched email,
  so hiding it would take listings off the page as they arrive.
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


def _preview_job():
    """A finished import preview holding one importable row.

    The destination `<select>` only renders next to a preview, because the
    first step (paste the links) does not use one. The row carries just the
    fields the preview table reads.
    """
    from services.background_jobs import run_job_sync

    row = {
        "status": "new",
        "title": "A plot the sheet found",
        "url": "https://www.fotocasa.es/es/comprar/terreno/aviles/x/181818181/d",
        "price": 42000.0,
        "area": 900.0,
        "municipality": "Avilés",
        "publisher_type": None,
        "client_type_id": None,
        "agency": None,
        "latitude": None,
        "longitude": None,
        "existing_id": None,
        "reason": None,
    }
    return run_job_sync(lambda: {"rows": [row]}, job_type="fotocasa_import_read")


@pytest.fixture
def subscriptions(app):
    """One live search, one archived, one hidden, plus the catch-all.

    Each holds a listing with a title nothing else uses, so "is this row on
    the page" is a substring test that cannot pass by accident.
    """
    with app.app_context():
        live = SearchProfile(name="Land at Norte", is_active=True)
        archived = SearchProfile(name="Legacy Lands", is_active=False)
        hidden = SearchProfile(
            name="Solares Norte Gijon", is_active=True, is_hidden=True
        )
        # Hidden *and* retired. The menu builds its archive section from its
        # own query, so a hidden-but-active subscription alone would leave
        # that query untested -- and it is the one that would put this row
        # back under `Archive`.
        hidden_archived = SearchProfile(
            name="Homes in Ciudad Quesada", is_active=False, is_hidden=True
        )
        catch_all = SearchProfile(name="Default", is_active=True, is_default=True)
        db.session.add_all([live, archived, hidden, hidden_archived, catch_all])
        db.session.commit()

        def _listing(slug, profile_id):
            prop = Property(
                source_email_id=f"hidden_{slug}",
                title=f"{slug}UniqueTitle",
                search_profile_id=profile_id,
                listing_status="active",
                municipality="Cudillero",
                location_lat=43.56,
                location_lon=-6.14,
            )
            db.session.add(prop)
            return prop

        _listing("live", live.id)
        _listing("archived", archived.id)
        _listing("hidden", hidden.id)
        _listing("hiddenarchived", hidden_archived.id)
        db.session.commit()

        return {
            "live": live.id,
            "archived": archived.id,
            "hidden": hidden.id,
            "hidden_archived": hidden_archived.id,
            "default": catch_all.id,
        }


class TestTheSubscriptionControls:
    def test_a_hidden_subscription_is_not_offered_anywhere(self, client, subscriptions):
        body = client.get("/properties").get_data(as_text=True)

        assert "Land at Norte" in body, "the live subscription still has its chip"
        assert "Legacy Lands" in body, "the archived one is still offered"
        assert "Solares Norte Gijon" not in body, (
            "a hidden subscription must not appear as a chip, in the menu, or "
            "under Archive"
        )
        assert "Homes in Ciudad Quesada" not in body, (
            "a retired subscription that is also hidden must not come back "
            "through the archive section"
        )
        for key in ("hidden", "hidden_archived"):
            assert 'value="{}"'.format(subscriptions[key]) not in body, (
                "and neither of them keeps a checkbox"
            )

    def test_its_listings_are_out_of_all_subscriptions(self, client, subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)

        assert "liveUniqueTitle" in body
        assert "hiddenUniqueTitle" not in body, (
            "hiding a subscription takes its listings out of the default view "
            "-- that is the difference between hiding one and retiring one"
        )

    def test_a_direct_link_still_reaches_it(self, client, subscriptions):
        body = client.get(
            "/properties?profile_id={}".format(subscriptions["hidden"])
        ).get_data(as_text=True)

        assert "hiddenUniqueTitle" in body, "nothing is deleted, and the id still works"
        assert 'value="{}"'.format(subscriptions["hidden"]) in body, (
            "the selected id needs its checkbox back, or the page's own script "
            "reads the state as 'nothing ticked' and the next Apply widens it"
        )
        assert 'id="hidden-subscriptions"' in body, (
            "and it renders under its own heading -- calling it archived would "
            "say something false about why it is on screen"
        )

    def test_the_page_says_what_it_is_not_showing(self, client, subscriptions):
        body = client.get("/properties").get_data(as_text=True)

        note = re.search(r'id="hidden-subscriptions-note".*?</div>', body, re.S)
        assert note, "a page that simply stopped mentioning them would read as complete"
        text = re.sub(r"\s+", " ", note.group(0))
        assert "2 hidden subscriptions" in text
        assert "2 listings" in text, "the number that moved is the row count"

    def test_a_selected_hidden_subscription_is_not_also_withheld(
        self, client, subscriptions
    ):
        body = client.get(
            "/properties?profile_id={}".format(subscriptions["hidden"])
        ).get_data(as_text=True)

        note = re.search(r'id="hidden-subscriptions-note".*?</div>', body, re.S)
        assert note, "the other hidden subscription is still being withheld"
        text = re.sub(r"\s+", " ", note.group(0))
        assert "1 hidden subscription" in text, (
            "the selected one is on screen already, so it is not part of what "
            "is being withheld"
        )

    def test_the_map_follows_the_same_rule(self, client, subscriptions):
        body = client.get("/map?profile_id=all").get_data(as_text=True)

        assert "liveUniqueTitle" in body
        assert "hiddenUniqueTitle" not in body

    def test_the_map_says_what_it_is_not_plotting(self, client, subscriptions):
        """It drops the markers, so it owes the same disclosure as the list.

        Without it a map missing several subscriptions looks like a map of
        everything there is.
        """
        body = client.get("/map?profile_id=all").get_data(as_text=True)

        note = re.search(r'id="hidden-subscriptions-note".*?</div>', body, re.S)
        assert note, "the map dropped two subscriptions and said nothing"
        text = re.sub(r"\s+", " ", note.group(0))
        assert "2 hidden subscriptions" in text
        assert "2 listings" in text

    def test_the_map_withholds_nothing_it_is_showing(self, client, subscriptions):
        body = client.get(
            "/map?profile_id={}".format(subscriptions["hidden"])
        ).get_data(as_text=True)

        assert "hiddenUniqueTitle" in body, "a named hidden id still plots"
        note = re.search(r'id="hidden-subscriptions-note".*?</div>', body, re.S)
        assert note and "1 hidden subscription" in re.sub(r"\s+", " ", note.group(0)), (
            "only the other one is being withheld"
        )

    def test_the_export_matches_the_page_it_was_taken_from(self, client, subscriptions):
        body = client.get("/properties/export.csv?profile_id=all").get_data(
            as_text=True
        )

        assert "liveUniqueTitle" in body
        assert "hiddenUniqueTitle" not in body


class TestTheAutomaticFallbacks:
    """A page that names no subscription must not open on a hidden one.

    `/properties` never asks -- a bare page is `all` -- but /map and the CSV
    export both resolve an `auto` selection by picking the busiest
    subscription, and both queries filtered on `is_active` alone. Found by a
    Spanish-language test that expected "2 suscripciones ocultas" and got
    "1": the fallback had opened the map on one of the two hidden ones.
    """

    def test_the_map_does_not_open_on_a_hidden_subscription(
        self, app, client, subscriptions
    ):
        with app.app_context():
            # The hidden one is the busiest, so it wins any ranking that does
            # not exclude it.
            for extra in range(3):
                db.session.add(
                    Property(
                        source_email_id=f"hidden_bulk_{extra}",
                        title=f"hiddenBulk{extra}Title",
                        search_profile_id=subscriptions["hidden"],
                        listing_status="active",
                        location_lat=43.56,
                        location_lon=-6.14,
                    )
                )
            db.session.commit()

        body = client.get("/map").get_data(as_text=True)

        assert "hiddenBulk0Title" not in body, (
            "a bare /map opened on the subscription the owner hid"
        )
        assert "liveUniqueTitle" in body

    def test_the_export_does_not_fall_back_to_a_hidden_subscription(
        self, app, client, subscriptions
    ):
        with app.app_context():
            profile = db.session.get(SearchProfile, subscriptions["default"])
            profile.is_active = True
            db.session.commit()

        body = client.get("/properties/export.csv").get_data(as_text=True)

        assert "hiddenUniqueTitle" not in body, (
            "an export that named no subscription pulled a hidden one"
        )


class TestTheControlOnProfiles:
    def test_profiles_lists_the_hidden_one_and_offers_the_way_back(
        self, client, subscriptions
    ):
        body = client.get("/profiles").get_data(as_text=True)

        assert "Solares Norte Gijon" in body, (
            "this is the page that manages them; hiding it from its own "
            "control would leave no way back"
        )
        assert "/profiles/{}/visibility".format(subscriptions["hidden"]) in body

    def test_hiding_and_showing_a_subscription(self, app, client, subscriptions):
        client.post(
            "/profiles/{}/visibility".format(subscriptions["live"]),
            data={"hidden": "on"},
        )
        with app.app_context():
            assert (
                db.session.get(SearchProfile, subscriptions["live"]).is_hidden is True
            )

        # Asserted on the listing rather than on the subscription's name: the
        # confirmation flash carries that name into the very next response.
        assert "liveUniqueTitle" not in client.get("/properties").get_data(as_text=True)

        client.post(
            "/profiles/{}/visibility".format(subscriptions["live"]),
            data={"hidden": "off"},
        )
        with app.app_context():
            assert (
                db.session.get(SearchProfile, subscriptions["live"]).is_hidden is False
            )
        assert "liveUniqueTitle" in client.get("/properties").get_data(as_text=True)

    def test_the_catch_all_cannot_be_hidden(self, app, client, subscriptions):
        client.post(
            "/profiles/{}/visibility".format(subscriptions["default"]),
            data={"hidden": "on"},
        )

        with app.app_context():
            catch_all = db.session.get(SearchProfile, subscriptions["default"])
            assert catch_all.is_hidden is False, (
                "it receives every email that matches nothing else, so hiding "
                "it would take listings off the page as they arrive"
            )

    def test_a_hidden_subscription_cannot_be_made_the_catch_all(
        self, app, client, subscriptions
    ):
        """The same pair from the other side (#533).

        Migration 028 refuses `is_default AND is_hidden` at the database, and
        a CHECK on a pair refuses the pair whichever column moved last -- so
        the "make default" tick on a hidden subscription's edit form, which
        /profiles offers for every subscription including the hidden ones,
        used to be a 500 waiting to happen. The route refuses it first and
        says why, the way `set_profile_hidden` refuses the other direction.
        """
        from utils.i18n import TRANSLATIONS

        response = client.post(
            "/profiles/{}/edit".format(subscriptions["hidden"]),
            data={
                "action": "save_profile_settings",
                "is_active": "on",
                "is_default": "on",
                "description": "still hidden",
            },
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        assert TRANSLATIONS["en"]["profile_hidden_cannot_be_default"] in body
        assert "profile_hidden_cannot_be_default" in TRANSLATIONS["es"]

        with app.app_context():
            hidden = db.session.get(SearchProfile, subscriptions["hidden"])
            assert hidden.is_default is False, (
                "a hidden catch-all would take listings off the page as they "
                "arrive; the database refuses it and so must the form"
            )
            assert hidden.is_hidden is True, "refusing must not un-hide it either"
            assert hidden.description == "still hidden", (
                "the rest of the form is saved; only the tick is refused"
            )
            catch_all = db.session.get(SearchProfile, subscriptions["default"])
            assert catch_all.is_default is True, "the real catch-all keeps its role"

    def test_the_import_destination_drops_the_hidden_ones(
        self, app, client, subscriptions
    ):
        """Asserted on the page that actually offers the destinations.

        A bare /properties/import is the paste-the-links step and renders no
        `<select>` at all, so checking that one for an absent name passes
        whatever the rule does -- the first version of this test did exactly
        that, and a mutation that dropped the filter kept it green.
        """
        with app.app_context():
            job_id = _preview_job()

        body = client.get(f"/properties/import?job={job_id}").get_data(as_text=True)

        assert 'name="profile_id"' in body, "this is the step that asks"
        assert "Land at Norte" in body, "a live subscription is a destination"
        assert "Legacy Lands" in body, "so is an archived one"
        assert "Solares Norte Gijon" not in body, (
            "importing into a hidden subscription would file the listing where "
            "the page does not show it"
        )

    def test_it_says_so_when_every_destination_is_hidden(
        self, app, client, subscriptions
    ):
        """An empty `required` select explains nothing on its own.

        The catch-all is un-defaulted before everything is hidden: since
        migration 028 (#533) the schema refuses a hidden catch-all -- the
        model carries the CHECK, so SQLite refuses it here too -- which is
        exactly the state the first version of this test built. "Every
        destination is hidden" is therefore reachable only on a database
        with no catch-all at all, a transient state `get_default_profile()`
        repairs on the next ingest; the guard is kept because an empty
        `required` select still explains nothing while it lasts.
        """
        with app.app_context():
            for profile in SearchProfile.query.all():
                profile.is_default = False
                profile.is_hidden = True
            db.session.commit()
            job_id = _preview_job()

        body = client.get(f"/properties/import?job={job_id}").get_data(as_text=True)

        assert 'id="import-no-destination"' in body


class TestIngestionDoesNotSeeTheFlag:
    def test_a_hidden_subscription_still_matches_its_own_email(
        self, app, subscriptions
    ):
        from services.search_profile_service import SearchProfileService

        with app.app_context():
            hidden = db.session.get(SearchProfile, subscriptions["hidden"])
            hidden.email_matchers = [{"pattern": "solares norte", "priority": 10}]
            db.session.commit()

            resolved = SearchProfileService.resolve_profile(
                "New listing", "solares norte gijon, 900 m2"
            )

            assert resolved is not None
            assert resolved.id == subscriptions["hidden"], (
                "routing its mail to the catch-all instead would be a data "
                "change wearing a UI change's clothes"
            )

    def test_list_profiles_still_returns_every_profile(self, app, subscriptions):
        from services.search_profile_service import SearchProfileService

        with app.app_context():
            everything = {p.id for p in SearchProfileService.list_profiles()}
            offered = {p.id for p in SearchProfileService.list_visible_profiles()}

            assert subscriptions["hidden"] in everything, (
                "the default is what ingestion reads; it must not quietly narrow"
            )
            assert subscriptions["hidden"] not in offered
            assert subscriptions["live"] in offered


class TestTheCountsAreTakenOnce:
    def test_the_menu_and_its_note_share_one_group_by(self, app, client, subscriptions):
        """One query, not two, for numbers that must agree anyway.

        Asserted on the count rather than on which function ran: the menu's
        per-subscription counts and the "not shown" line are the same
        group-by, and taking it twice was how they could have disagreed
        between the two statements.
        """
        from routes import main_routes

        calls = []
        original = main_routes._listing_counts_by_profile

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        main_routes._listing_counts_by_profile = counted
        try:
            body = client.get("/properties").get_data(as_text=True)
        finally:
            main_routes._listing_counts_by_profile = original

        assert "hidden-subscriptions-note" in body, "the page really rendered it"
        assert len(calls) == 1, f"the listing counts were taken {len(calls)} times"


class TestTheColumnIsFalseNotNull:
    def test_a_profile_created_without_the_flag_is_visible(self, app):
        with app.app_context():
            profile = SearchProfile(name="Freshly created", is_active=True)
            db.session.add(profile)
            db.session.commit()

            assert profile.is_hidden is False, (
                "hiding is a choice somebody makes; a row that says nothing is "
                "not hidden"
            )
