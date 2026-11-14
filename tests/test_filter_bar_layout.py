"""Which zone each /properties control lives in after the 2026-11-14 regrouping.

The owner called the filters "a mess again": Favorites and Hide removed sat
beside the subscription although they narrow the rows, Apply came before the
Filters trigger it applies, the Decision select had a caption repeating its own
options, and rare subscription tools took a row of their own. These tests pin
the markup the regrouping rests on. Positions and heights are a browser
measurement on a built image (docs/design/README.md), not asserted here.
"""

import html
import re
from urllib.parse import parse_qs, urlsplit

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
def profile_id(app):
    with app.app_context():
        profile = SearchProfile(
            name="Galicia coast",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        # A second live subscription, so a bare /properties shows both and
        # no single subscription is on screen.
        other = SearchProfile(
            name="Asturias villages",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([profile, other])
        db.session.commit()
        db.session.add(
            Property(
                source_email_id="filter_bar_layout_0",
                title="FilterBarLayoutListing",
                municipality="Arteixo",
                search_profile_id=profile.id,
                listing_status="active",
                property_category="housing",
                property_subtype="house",
                price=180000,
                area=150,
            )
        )
        db.session.commit()
        return profile.id


def _zones(body):
    """Split the page into the scope zone and the filter form."""
    scope_start = body.index('id="subscription-switcher"')
    filters_start = body.index('id="filters-card"')
    form_start = body.index('id="filter-form"')
    form_end = body.index("</form>", form_start)
    return body[scope_start:filters_start], body[form_start:form_end]


def test_favorites_and_hide_removed_live_in_the_filter_form_once(client, profile_id):
    body = client.get("/properties?remember=off").get_data(as_text=True)
    scope, form = _zones(body)

    assert body.count("scope-switch") == 2
    assert form.count("scope-switch") == 2
    assert "scope-switch" not in scope

    links = {}
    for tag in re.findall(r'<a class="btn btn-sm scope-switch[^>]*>', form, re.S):
        title = re.search(r'title="([^"]*)"', tag).group(1)
        href = html.unescape(re.search(r'href="([^"]*)"', tag).group(1))
        links[title] = parse_qs(urlsplit(href).query)
    favorites = links["Show only the listings you starred"]
    hide_removed = links["Hide listings that were removed or sold"]

    # Hide removed is on by default, so each link flips only its own switch.
    assert favorites["favorites"] == ["on"]
    assert favorites["hide_removed"] == ["on"]
    assert "favorites" not in hide_removed
    assert "hide_removed" not in hide_removed
    # Both stay one-click links that keep the applied view and reset the page.
    for query in (favorites, hide_removed):
        assert query["page"] == ["1"]
        assert "view_type" in query
        assert "mode" in query


def test_one_apply_closes_the_filter_group_after_the_filters_trigger(
    client, profile_id
):
    body = client.get("/properties?remember=off").get_data(as_text=True)
    _, form = _zones(body)

    assert form.count('type="submit"') == 1
    apply_at = form.index("workspace-filter-apply")
    assert apply_at > form.index("</details>", form.index('id="additional-filters"'))


def test_decision_select_has_one_visible_name(client, profile_id):
    body = client.get("/properties?remember=off").get_data(as_text=True)
    _, form = _zones(body)

    label = re.search(
        r'<label class="properties-queue-switch">(.*?)<select', form, re.S
    )
    assert label
    # The caption is for assistive technology; the options name the field.
    assert re.fullmatch(
        r'\s*<span class="visually-hidden">[^<]+</span>\s*', label.group(1)
    )
    assert 'aria-label="' in form[label.end() : form.index(">", label.end())]


def test_subscription_tools_menu_is_drawn_only_while_it_holds_something(
    client, profile_id
):
    bare = client.get("/properties?remember=off").get_data(as_text=True)
    assert 'id="subscription-tools-toggle"' not in bare

    chosen = client.get(f"/properties?remember=off&profile_id={profile_id}").get_data(
        as_text=True
    )
    scope, form = _zones(chosen)
    assert chosen.count('id="subscription-tools-toggle"') == 1
    assert 'id="subscription-tools-toggle"' in scope
    toggle = re.search(r'<button[^>]*id="subscription-tools-toggle"[^>]*>', scope, re.S)
    assert toggle and 'data-bs-toggle="dropdown"' in toggle.group(0)
    assert "Edit subscription" in scope
    assert "Edit subscription" not in form
