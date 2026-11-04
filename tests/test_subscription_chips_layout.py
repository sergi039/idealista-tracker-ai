"""The approved workspace replaces the chip strip with one readable multiselect.

Selection, long names and native form associations are proved through rendered
responses. Geometry is exercised against a built image, not asserted here.
"""

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
def two_subscriptions(app):
    """Two live subscriptions, one with a name long enough to truncate."""
    with app.app_context():
        short = SearchProfile(
            name="Asturias",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        long = SearchProfile(
            name="houses at your custom search area norte",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([short, long])
        db.session.commit()
        for index, profile in enumerate((short, long)):
            db.session.add(
                Property(
                    source_email_id=f"chips_layout_{index}",
                    title=f"ChipsLayoutListing{index}",
                    municipality="Cudillero",
                    search_profile_id=profile.id,
                    listing_status="active",
                    property_category="land",
                    property_subtype="plot",
                    price=60000,
                    area=1300,
                )
            )
        db.session.commit()


def test_one_subscription_selector_replaces_the_strip(client, two_subscriptions):
    body = client.get("/properties").get_data(as_text=True)
    assert body.count('id="profile-select-dropdown"') == 1
    assert "properties-subscription-chips" not in body
    assert "houses at your custom search area norte" in body
    assert 'id="subscription-search"' in body


def test_selected_long_name_is_the_visible_scope_label(app, client, two_subscriptions):
    import re

    with app.app_context():
        chosen = (
            SearchProfile.query.filter_by(
                name="houses at your custom search area norte"
            )
            .one()
            .id
        )
    body = client.get(f"/properties?profile_id={chosen}").get_data(as_text=True)
    label = re.search(r'id="profile-select-label">(.*?)</span>', body, re.S)
    assert label and label.group(1) == "houses at your custom search area norte"


def test_menu_has_an_explicit_apply_for_the_same_filter_form(client, two_subscriptions):
    body = client.get("/properties").get_data(as_text=True)
    assert 'type="submit" form="filter-form"' in body
    assert 'data-submit="explicit"' in body
    assert "hidden.bs.dropdown" not in body
