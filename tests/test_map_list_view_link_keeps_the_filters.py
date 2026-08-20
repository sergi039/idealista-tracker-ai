"""The map's "List View" button has to open the set the map is drawing.

`/map` applies the same filter vocabulary `/properties` does -- category,
subtype, municipality, source, advertiser, search, inv_metr, sea_view,
favorites -- through the same shared clauses, and the three links that lead
*to* the map carry all of them. The one link leading *back* carried
`profile_id` and nothing else, so a map of the listings sold by their owners
had a button that opened a list of every listing, with nothing saying the set
had changed. That is the defect #435 fixed inside `/properties`, in the seam
between the two surfaces instead of inside one of them.

The tests follow the link rather than reading it, for the reason
`tests/test_filters_survive_page_and_sort_links.py` records: a test asserting
`advertiser=owner` appears in the href would pass on a link that goes
somewhere else, and where the link goes is the whole question.

Two of the parameters are not copied from the request, and each has a test of
its own because both are easy to "correct" into a defect later:

* `hide_removed` is asserted rather than read. The map excludes delisted
  listings unconditionally, so `on` is what it is really showing, and reading
  the parameter would open a list holding rows the map refused to plot.
  **What these two tests pin is the statement, not the outcome, and that
  changed under them on the day they were written.** Until #439 an absent
  `hide_removed` beside any other filter read as an unticked box and the far
  end widened the list, so the row-set assertions below bit. #439 replaced
  that reading: a cross-page link carries neither form marker and now gets the
  default, which is `on`. Measured after rebasing onto it -- with the explicit
  key removed, the withdrawn listing is still absent at the far end. So the
  assertions that fail without the key are the href ones, and the row-set
  assertions are kept as a regression guard on the far end's default rather
  than as proof of this link. Do not read their green as coverage of it.
* `measured` is deliberately absent. `/map` never applies it, and carrying it
  would narrow the list below the map it came from: filtering claimed where
  none happened.

The list is still a superset of the map, and `test_the_list_is_a_superset`
pins that on purpose: the map plots only rows that have coordinates, the list
has no such filter, and the link must not invent one.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


def _list_view_href(body: str) -> str:
    match = re.search(r'<a id="map-list-view-link"[^>]*href="([^"]+)"', body)
    assert match, "the map needs a stable link back to the list view"
    return unescape(match.group(1))


def _params(href: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(href).query, keep_blank_values=True)


def _marker_ids(body: str) -> set[int]:
    """Ids of the markers the map handed to its script."""
    match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
    if match is None:
        match = re.search(r'data-markers="([^"]*)"', body)
    assert match, "could not find the marker payload on the map page"
    return {int(pid) for pid in re.findall(r'"id":\s*(\d+)', unescape(match.group(1)))}


def _listing_ids(body: str) -> set[int]:
    return {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}


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


def _make(profile_id, key, *, seller, coords=(43.5, -6.5), status="active"):
    campaign = "particular" if seller == "owner" else "professional"
    return Property(
        source_email_id=f"maplink_{key}",
        title=f"Plot {key}",
        municipality="Castrillon",
        property_category="land",
        property_subtype="plot",
        price=40000,
        location_lat=coords[0] if coords else None,
        location_lon=coords[1] if coords else None,
        url=(
            f"https://www.idealista.com/inmueble/{abs(hash(key)) % 100000}/"
            f"?utm_campaign=express_newAd_sale_{campaign}"
        ),
        search_profile_id=profile_id,
        listing_status=status,
    )


@pytest.fixture
def listings(app):
    """Owner and agency listings, one of each awkward kind."""
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        rows = {
            "owner_a": _make(profile.id, "owner_a", seller="owner"),
            "owner_b": _make(
                profile.id, "owner_b", seller="owner", coords=(43.6, -6.6)
            ),
            # An owner listing with no coordinate: the map cannot plot it, the
            # list can and must still show it.
            "owner_nocoord": _make(
                profile.id, "owner_nocoord", seller="owner", coords=None
            ),
            # An owner listing the advertiser removed: the map refuses it
            # unconditionally, so the link must not open a list holding it.
            "owner_removed": _make(
                profile.id,
                "owner_removed",
                seller="owner",
                coords=(43.7, -6.7),
                status="removed",
            ),
            "agency_a": _make(
                profile.id, "agency_a", seller="agency", coords=(43.8, -6.8)
            ),
        }
        db.session.add_all(list(rows.values()))
        db.session.commit()

        return {name: row.id for name, row in rows.items()}


class TestTheLinkOpensTheMapsOwnSet:
    def test_the_advertiser_filter_comes_back(self, client, listings):
        map_body = client.get("/map?advertiser=owner").get_data(as_text=True)
        plotted = _marker_ids(map_body)
        assert plotted == {listings["owner_a"], listings["owner_b"]}

        href = _list_view_href(map_body)
        assert _params(href).get("advertiser") == ["owner"]

        listed = _listing_ids(client.get(href).get_data(as_text=True))
        assert listings["agency_a"] not in listed, (
            "the map plotted no agency listing; its List View link opened one"
        )
        assert plotted <= listed

    def test_the_source_filter_comes_back(self, client, listings):
        map_body = client.get("/map?source=idealista").get_data(as_text=True)
        href = _list_view_href(map_body)

        assert _params(href).get("source") == ["idealista"]
        assert client.get(href).status_code == 200

    def test_several_filters_at_once(self, client, listings):
        map_body = client.get(
            "/map?advertiser=owner&category=land&municipality=Castrillon"
        ).get_data(as_text=True)
        params = _params(_list_view_href(map_body))

        assert params.get("advertiser") == ["owner"]
        assert params.get("category") == ["land"]
        assert params.get("municipality") == ["Castrillon"]

    def test_a_filter_that_is_not_set_is_not_invented(self, client, listings):
        """An empty filter must be absent, not sent blank: `?category=` on the
        list is a filter the map was not applying."""
        params = _params(_list_view_href(client.get("/map").get_data(as_text=True)))

        for key in (
            "category",
            "subtype",
            "municipality",
            "source",
            "advertiser",
            "search",
            "inv_metr",
            "sea_view",
            "favorites",
        ):
            assert key not in params, f"{key} was sent although the map applied none"


class TestTheTwoParametersThatAreNotCopied:
    def test_hide_removed_is_asserted_so_the_list_matches_the_map(
        self, client, listings
    ):
        """The map never plots a removed listing, and its link says so rather
        than leaving the far end to decide. Since #439 the far end's default
        agrees, so the assertion that bites here is the href one; the row-set
        assertion guards that default against changing under this link."""
        map_body = client.get("/map?advertiser=owner").get_data(as_text=True)
        assert listings["owner_removed"] not in _marker_ids(map_body)

        href = _list_view_href(map_body)
        assert _params(href).get("hide_removed") == ["on"]

        listed = _listing_ids(client.get(href).get_data(as_text=True))
        assert listings["owner_removed"] not in listed, (
            "the map refused this listing and its List View link served it"
        )

    def test_hide_removed_is_on_even_when_the_caller_asked_otherwise(
        self, client, listings
    ):
        """`/map?hide_removed=` changes nothing about the map, so it must not
        change the link either -- the link describes the map, not the URL."""
        map_body = client.get("/map?advertiser=owner&hide_removed=off").get_data(
            as_text=True
        )
        assert listings["owner_removed"] not in _marker_ids(map_body)

        href = _list_view_href(map_body)
        assert _params(href).get("hide_removed") == ["on"]
        assert listings["owner_removed"] not in _listing_ids(
            client.get(href).get_data(as_text=True)
        )

    def test_measured_is_not_carried_because_the_map_never_applied_it(
        self, client, listings
    ):
        map_body = client.get("/map?measured=travel").get_data(as_text=True)

        assert "measured" not in _params(_list_view_href(map_body)), (
            "the map applies no `measured` filter; carrying it would open a "
            "list narrower than the map it came from"
        )


class TestTheListIsAllowedToBeWider:
    def test_the_list_is_a_superset(self, client, listings):
        """The map plots only what has a coordinate. The link carries no
        coordinate filter and must not grow one: an owner listing with no
        coordinate belongs on the list."""
        map_body = client.get("/map?advertiser=owner").get_data(as_text=True)
        assert listings["owner_nocoord"] not in _marker_ids(map_body)

        listed = _listing_ids(
            client.get(_list_view_href(map_body)).get_data(as_text=True)
        )
        assert listings["owner_nocoord"] in listed
