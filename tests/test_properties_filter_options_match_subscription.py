"""The /properties Type and Subtype filters must describe the subscriptions
on screen, not the whole table.

Both dropdowns used to be built from `properties` as a whole and to carry a
hard-coded "Unclassified" option. On the owner's install that meant a saved
search for land offered `apartment`, `house` and `developed` -- values owned
by other (mostly retired) subscriptions, which can only ever return an empty
page there -- plus an "Unclassified" choice matching zero rows, because every
ingested listing is classified.

The option is not deleted, though: ingestion can still persist a listing no
regex rule matches, and that is exactly when the owner needs to find it. It is
offered on the same terms as the "No subscription" checkbox -- only when such
rows exist in the current selection.
"""

import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


def _select_options(body, select_id):
    """Values offered by the <select id="..."> on the rendered page."""
    block = re.search(
        rf'<select[^>]*id="{select_id}"[^>]*>(.*?)</select>', body, re.DOTALL
    )
    assert block, f"no <select id={select_id!r}> in the page"
    return re.findall(r'<option value="([^"]*)"', block.group(1))


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
def two_subscriptions(app):
    """A land subscription and a housing one, like the owner's live pair."""
    with app.app_context():
        land_profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        housing_profile = SearchProfile(
            name="houses at your custom search area norte",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([land_profile, housing_profile])
        db.session.commit()

        db.session.add_all(
            [
                Property(
                    source_email_id="opts_land_plot",
                    title="Land plot in Parroquias Norte, Siero",
                    municipality="Siero",
                    property_category="land",
                    property_subtype="plot",
                    search_profile_id=land_profile.id,
                    listing_status="active",
                ),
                Property(
                    source_email_id="opts_housing_house",
                    title="Casa in Anes",
                    municipality="Llanera",
                    property_category="housing",
                    property_subtype="house",
                    search_profile_id=housing_profile.id,
                    listing_status="active",
                ),
                Property(
                    source_email_id="opts_housing_apartment",
                    title="Piso in Oviedo",
                    municipality="Oviedo",
                    property_category="housing",
                    property_subtype="apartment",
                    search_profile_id=housing_profile.id,
                    listing_status="active",
                ),
            ]
        )
        db.session.commit()

        return {"land_id": land_profile.id, "housing_id": housing_profile.id}


class TestFilterOptionsFollowTheSubscription:
    def test_land_subscription_does_not_offer_other_subscriptions_values(
        self, client, two_subscriptions
    ):
        response = client.get(f"/properties?profile_id={two_subscriptions['land_id']}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        assert _select_options(body, "category") == ["", "land"]
        assert _select_options(body, "subtype") == ["", "plot"]
        assert _select_options(body, "municipality") == ["", "Siero"]

    def test_housing_subscription_offers_its_own_values(
        self, client, two_subscriptions
    ):
        response = client.get(
            f"/properties?profile_id={two_subscriptions['housing_id']}"
        )
        body = response.get_data(as_text=True)

        assert _select_options(body, "category") == ["", "housing"]
        assert _select_options(body, "subtype") == ["", "apartment", "house"]
        assert _select_options(body, "municipality") == ["", "Llanera", "Oviedo"]

    def test_all_subscriptions_offers_the_union(self, client, two_subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)

        assert _select_options(body, "category") == ["", "housing", "land"]
        assert _select_options(body, "subtype") == ["", "apartment", "house", "plot"]

    def test_chosen_category_narrows_the_subtypes(self, client, two_subscriptions):
        body = client.get("/properties?profile_id=all&category=land").get_data(
            as_text=True
        )

        assert _select_options(body, "subtype") == ["", "plot"]

    def test_applied_filter_survives_a_subscription_it_is_absent_from(
        self, client, two_subscriptions
    ):
        """Switching subscriptions must not leave the control reading "All
        subtypes" over a page that is still filtered to something else. The
        subscription's own values stay listed alongside it, so the way out of
        the empty page is one click."""
        body = client.get(
            f"/properties?profile_id={two_subscriptions['land_id']}&subtype=apartment"
        ).get_data(as_text=True)

        assert _select_options(body, "subtype") == ["", "apartment", "plot"]
        assert 'value="apartment" selected' in body


class TestUnclassifiedOption:
    def test_absent_when_everything_is_classified(self, client, two_subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)

        assert "__none__" not in _select_options(body, "category")
        assert "__none__" not in _select_options(body, "subtype")
        assert "Unclassified" not in body

    def test_offered_once_an_unclassified_listing_exists(
        self, app, client, two_subscriptions
    ):
        with app.app_context():
            db.session.add(
                Property(
                    source_email_id="opts_unclassified",
                    title="Something no rule matches",
                    property_category=None,
                    property_subtype="",
                    search_profile_id=two_subscriptions["land_id"],
                    listing_status="active",
                )
            )
            db.session.commit()

        body = client.get(
            f"/properties?profile_id={two_subscriptions['land_id']}"
        ).get_data(as_text=True)

        assert "__none__" in _select_options(body, "category")
        assert "__none__" in _select_options(body, "subtype")

    def test_offered_only_in_the_subscription_that_has_one(
        self, app, client, two_subscriptions
    ):
        with app.app_context():
            db.session.add(
                Property(
                    source_email_id="opts_unclassified_land",
                    title="Something no rule matches",
                    property_category=None,
                    property_subtype=None,
                    search_profile_id=two_subscriptions["land_id"],
                    listing_status="active",
                )
            )
            db.session.commit()

        body = client.get(
            f"/properties?profile_id={two_subscriptions['housing_id']}"
        ).get_data(as_text=True)

        assert "__none__" not in _select_options(body, "category")

    def test_stays_selectable_while_it_is_the_applied_filter(
        self, client, two_subscriptions
    ):
        """A hand-typed or bookmarked `category=__none__` must still render as
        the applied filter, empty page or not -- the query still applies it."""
        body = client.get("/properties?profile_id=all&category=__none__").get_data(
            as_text=True
        )

        assert "__none__" in _select_options(body, "category")
        assert 'value="__none__" selected' in body
