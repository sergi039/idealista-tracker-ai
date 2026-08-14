"""Issue #287: `/map?focus=<id>` has to reach the listing it names.

The map icon on a property page links to `/map?focus=<id>` and passes no
`profile_id`, so the map resolved the subscription itself -- and its rule is
"the one with the most mappable rows". A listing in any other subscription was
therefore never in the marker set, `templates/map.html` found nothing for the
id, and its fallback fitted the bounds of every marker: a map showing the
whole coast, no focused marker, and not a word about it. The owner found that,
not us.

What these tests pin:

* **`focus` decides the subscription when the request names none.** Not the
  biggest one, not the default one. The fixture makes the two answers
  different on purpose -- `coast` holds more mappable rows than `houses` --
  and `test_fixture_can_tell_the_implementations_apart` fails if a later edit
  makes them agree, which would let the old behaviour pass every assertion
  here.
* **It resolves the subscription; it does not drop the filter.** A focus must
  not turn the map into "every listing there is", so the markers are asserted
  to be exactly the focused subscription's own.
* **An absence is explained.** Every reason a focused listing can be missing
  -- unknown id, no coordinates, delisted, another subscription, a filter --
  renders a notice naming that reason, and where the page offers a way out the
  link is *followed* and asserted to actually show the listing. A notice
  offering a link to another empty map would be the same defect wearing a
  banner.
* **The owner's own path.** The last test starts where the report started: it
  reads the map link off `/properties/<id>` and follows it.
"""

import html
import json
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


def _marker_ids(body):
    """Property ids of the markers `/map` handed to Leaflet."""
    match = re.search(r"const markers = (\[.*?\]);", body, re.S)
    assert match, "the map page no longer emits a `const markers = [...]` literal"
    return sorted(int(marker["id"]) for marker in json.loads(match.group(1)))


def _notice(body):
    """The focus notice as `(reason, text, href)`, or None when absent."""
    match = re.search(
        r'<div class="[^"]*" id="map-focus-notice" data-reason="([^"]+)">(.*?)</div>',
        body,
        re.S,
    )
    if not match:
        return None
    reason, inner = match.group(1), match.group(2)
    href = re.search(r'href="([^"]*)"', inner)
    text = html.unescape(re.sub(r"<[^>]+>", " ", inner))
    return (
        reason,
        " ".join(text.split()),
        html.unescape(href.group(1)) if href else None,
    )


def _anchor_href(body, needle):
    for tag in re.findall(r"<a\b[^>]*>", body):
        if needle not in tag:
            continue
        match = re.search(r'href="([^"]*)"', tag)
        if match:
            return html.unescape(match.group(1))
    return None


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
def world(app):
    """Two live subscriptions of deliberately different size, plus the edges.

    `coast` is the one the map picks cold -- it holds the most mappable rows.
    `houses` is the smaller one the owner's listing was actually in, and every
    reason a focused listing can go missing has a row of its own.
    """
    with app.app_context():
        coast = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        houses = SearchProfile(
            name="houses at your custom search area norte",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        retired = SearchProfile(
            name="Homes in Ciudad Quesada",
            is_active=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([coast, houses, retired])
        db.session.commit()

        def _add(slug, profile_id, **kwargs):
            fields = {
                "source_email_id": f"issue287_{slug}",
                "title": f"Issue287 {slug}",
                "search_profile_id": profile_id,
                "listing_status": "active",
                "municipality": "El Franco",
                "location_lat": 43.55,
                "location_lon": -6.83,
                "property_category": "land",
            }
            fields.update(kwargs)
            prop = Property(**fields)
            db.session.add(prop)
            db.session.commit()
            return prop.id

        ids = {
            # Three mappable rows: `coast` wins the "most mappable" rule.
            "coast_a": _add("coast_a", coast.id),
            "coast_b": _add("coast_b", coast.id, location_lat=43.56),
            "coast_c": _add("coast_c", coast.id, location_lat=43.57),
            # One mappable row in the smaller subscription: the reported case.
            "target": _add("target", houses.id, property_category="house"),
            "no_coords": _add(
                "no_coords", houses.id, location_lat=None, location_lon=None
            ),
            "removed": _add("removed", houses.id, listing_status="removed"),
            "retired": _add("retired", retired.id),
            "unassigned": _add("unassigned", None),
        }
        return {
            "ids": ids,
            "coast_id": coast.id,
            "houses_id": houses.id,
            "retired_id": retired.id,
            "coast_markers": sorted([ids["coast_a"], ids["coast_b"], ids["coast_c"]]),
        }


def test_fixture_can_tell_the_implementations_apart(client, world):
    """The subscription the map picks cold must not be the focused one.

    Without this the whole file would pass against the defect.
    """
    assert len(world["coast_markers"]) > 1
    bare = _marker_ids(client.get("/map").get_data(as_text=True))
    assert bare == world["coast_markers"]
    assert world["ids"]["target"] not in bare


class TestFocusResolvesTheSubscription:
    def test_focus_reaches_a_listing_in_another_subscription(self, client, world):
        """The regression: the reported URL shape, with no `profile_id`."""
        body = client.get(f"/map?focus={world['ids']['target']}").get_data(as_text=True)
        assert world["ids"]["target"] in _marker_ids(body)

    def test_focus_does_not_widen_the_map_to_everything(self, client, world):
        """Resolving the subscription is the fix -- not dropping the filter."""
        body = client.get(f"/map?focus={world['ids']['target']}").get_data(as_text=True)
        markers = _marker_ids(body)
        assert markers == [world["ids"]["target"]]
        assert not set(markers) & set(world["coast_markers"])

    def test_focus_says_nothing_when_the_listing_is_on_the_map(self, client, world):
        body = client.get(f"/map?focus={world['ids']['target']}").get_data(as_text=True)
        assert _notice(body) is None

    def test_focus_reaches_a_retired_subscription(self, client, world):
        """A retired subscription's listings are real and stay reachable."""
        body = client.get(f"/map?focus={world['ids']['retired']}").get_data(
            as_text=True
        )
        assert _marker_ids(body) == [world["ids"]["retired"]]

    def test_the_link_back_to_the_list_follows_the_focused_subscription(
        self, client, world
    ):
        body = client.get(f"/map?focus={world['ids']['target']}").get_data(as_text=True)
        href = _anchor_href(body, 'id="map-list-view-link"')
        assert href is not None
        assert f"profile_id={world['houses_id']}" in href

    def test_an_unparseable_focus_is_no_focus_at_all(self, client, world):
        for raw in ("abc", "-3", "0", "9" * 40):
            body = client.get(f"/map?focus={raw}").get_data(as_text=True)
            assert _marker_ids(body) == world["coast_markers"], raw
            assert _notice(body) is None, raw

    def test_an_explicit_subscription_still_wins(self, client, world):
        """`focus` answers the *auto* fallback; it never overrides a choice."""
        body = client.get(
            f"/map?focus={world['ids']['target']}&profile_id={world['coast_id']}"
        ).get_data(as_text=True)
        assert _marker_ids(body) == world["coast_markers"]


class TestTheAbsenceIsExplained:
    def test_unknown_id(self, client, world):
        body = client.get("/map?focus=987654").get_data(as_text=True)
        reason, text, href = _notice(body)
        assert reason == "unknown"
        assert "987654" in text
        assert href is None

    def test_listing_without_coordinates(self, client, world):
        body = client.get(f"/map?focus={world['ids']['no_coords']}").get_data(
            as_text=True
        )
        reason, text, _ = _notice(body)
        assert reason == "no_coordinates"
        assert "coordinates" in text

    def test_delisted_listing(self, client, world):
        body = client.get(f"/map?focus={world['ids']['removed']}").get_data(
            as_text=True
        )
        reason, text, _ = _notice(body)
        assert reason == "delisted"
        assert "removed" in text

    def test_another_subscription_and_the_way_out_works(self, client, world):
        """The notice names the subscription, and its link really shows it."""
        body = client.get(
            f"/map?focus={world['ids']['target']}&profile_id={world['coast_id']}"
        ).get_data(as_text=True)
        reason, text, href = _notice(body)
        assert reason == "other_subscription"
        assert "houses at your custom search area norte" in text
        followed = client.get(href).get_data(as_text=True)
        assert world["ids"]["target"] in _marker_ids(followed)
        assert _notice(followed) is None

    def test_a_listing_with_no_subscription(self, client, world):
        body = client.get(f"/map?focus={world['ids']['unassigned']}").get_data(
            as_text=True
        )
        reason, text, href = _notice(body)
        assert reason == "other_subscription"
        assert "no subscription" in text
        followed = client.get(href).get_data(as_text=True)
        assert world["ids"]["unassigned"] in _marker_ids(followed)

    def test_a_filter_hiding_it_is_named_as_the_reason(self, client, world):
        """The target is a `house`; the filter asks for `land`."""
        body = client.get(
            f"/map?focus={world['ids']['target']}&category=land"
        ).get_data(as_text=True)
        reason, text, href = _notice(body)
        assert reason == "filtered"
        assert "filters" in text
        followed = client.get(href).get_data(as_text=True)
        assert world["ids"]["target"] in _marker_ids(followed)

    def test_the_link_out_keeps_the_filters_it_is_not_about(self, client, world):
        """Switching subscription must not silently discard the rest.

        The listing is proven to pass every other filter at that point, so
        carrying them costs the reader nothing and keeps their view.
        """
        body = client.get(
            f"/map?focus={world['ids']['target']}"
            f"&profile_id={world['coast_id']}&category=house&search=Issue287"
        ).get_data(as_text=True)
        _, _, href = _notice(body)
        assert "category=house" in href
        assert "search=Issue287" in href
        assert world["ids"]["target"] in _marker_ids(
            client.get(href).get_data(as_text=True)
        )


def test_the_owners_path_end_to_end(client, world):
    """From the property page to a map that shows that property.

    The report was "I click our map from /properties/360 and the object is not
    there", so the link is read off the real page rather than assumed.
    """
    target = world["ids"]["target"]
    page = client.get(f"/properties/{target}").get_data(as_text=True)
    href = _anchor_href(page, f"/map?focus={target}")
    assert href, "the property page no longer links to the map with a focus"
    assert target in _marker_ids(client.get(href).get_data(as_text=True))
