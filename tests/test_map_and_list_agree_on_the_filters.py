"""#445: the map applies what its links carry, and clearing filters clears them.

Two defects with one cause. `/map` kept hand-written lists of "which
parameters are this page's filters", and each went stale when a filter was
added:

* it never read `measured` at all, so pressing Map on a narrowed list widened
  it again in silence -- measured on production 2026-08-20 at `profile_id=all`,
  `/properties?measured=full` showed **72** listings and `/map?measured=full`
  plotted **470**, the same as no filter;
* `_map_focus_link`'s `dropped` set named seven filters and had gone stale by
  four (`source`, `advertiser`, `verdict`, `action`), so "Clear the filters and
  show it" re-issued the filter that had hidden the listing and landed on the
  byte-identical notice. Its docstring promises the opposite, and the call site
  says "Clearing them is guaranteed to work".

The repair is not the four missing strings -- that is the fix that had by then
failed four times (#435, #439, #444 and the `dropped` set itself). The link is
built from the record of what the route read (`utils/listing_filters.FilterArgs`)
and the clearing is expressed as "keep the non-filters", so neither can go
stale when a filter is added.

`TestEveryFilterAgrees` is therefore the test that matters: it walks the whole
vocabulary and asks the property both surfaces owe -- **one URL, one set** --
rather than asking about `measured` by name. A filter added to `/properties`
and forgotten on the map fails there without anyone writing a test for it,
which is what none of the four earlier fixes had.

Note what is deliberately *not* asserted: that the map and the list show the
same number of rows. The map plots only listings that have coordinates, and
that difference is legitimate and permanent. What must agree is which listings
each surface *excludes by filtering*, so the comparison is always "the map's
markers are exactly the coordinate-bearing subset of the list's rows".
"""

from __future__ import annotations

import re
from html import unescape
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# A value for every filter, chosen to really split the fixture: a value that
# selected everything would let a missing filter pass unnoticed, which
# `test_the_matrix_bites` keeps honest.
#
# **This table is checked against the page rather than trusted.**
# `test_the_sweep_covers_every_filter_the_page_has` reads `current_filters` out
# of the rendered `/properties` and fails if anything in it is neither here nor
# excused below. Writing it by hand and calling it "the whole vocabulary" is
# precisely the mistake #445 is about, and the first version of this file made
# it: an independent review found the sweep walked 10 of the 12 filters
# `map_view` reads, missing `sea_view` and `inv_metr`, while the docstring
# above claimed otherwise. Disabling the map's `sea_view` clause left all 37
# tests green.
FILTERS = [
    ("category", "land"),
    ("subtype", "plot"),
    ("municipality", "Castrillon"),
    ("source", "fotocasa"),
    ("advertiser", "owner"),
    ("search", "Findable"),
    ("measured", "full"),
    ("favorites", "on"),
    # Added the morning of the day #445 was written (#430), which is exactly
    # why the sweep matters more than the named cases: a hand-written list is
    # stale the moment a filter lands, and nothing tells you.
    ("verdict", "rejected"),
    ("action", "overdue"),
    ("sea_view", "yes"),
    ("inv_metr", "EXCELLENT"),
    ("sea_dist", "800"),
    ("build", "solar"),
]

# Keys of `current_filters` that are not filters, and why. Anything else the
# page carries must appear in FILTERS above.
NOT_A_FILTER = {
    "profile_id": "the subscription selection, replaced rather than narrowed",
    "sort_by": "ordering",
    "order": "ordering",
    "page": "pagination",
    "per_page": "pagination",
    "mode": "which score is emphasised",
    "active_mode": "derived from the applied sort, never sent",
    "view_type": "cards or table",
    "hide_removed": (
        "the map excludes delisted listings unconditionally, so the two "
        "surfaces cannot be compared on it -- see the List View link's own "
        "comment in map_view()"
    ),
}


def _list_view_href(body: str) -> str:
    match = re.search(r'<a[^>]*id="map-list-view-link"(.*?)>', body, re.DOTALL)
    assert match, "the map needs a stable link back to the list view"
    href = re.search(r'href="([^"]+)"', match.group(1))
    assert href, "the List View anchor has no href"
    return unescape(href.group(1))


def _focus_notice(body: str):
    """The notice and its recovery link, when the map is hiding the focus."""
    block = re.search(
        r'id="map-focus-notice"[^>]*data-reason="([^"]*)"(.*?)</div>', body, re.DOTALL
    )
    if block is None:
        return None
    href = re.search(r'href="([^"]+)"', block.group(2))
    return {
        "reason": block.group(1),
        "href": unescape(href.group(1)) if href else None,
    }


def _marker_ids(body: str) -> set[int]:
    match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
    assert match, "could not find the marker payload on the map page"
    return {int(pid) for pid in re.findall(r'"id":\s*(\d+)', unescape(match.group(1)))}


def _listing_ids(body: str) -> set[int]:
    return {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}


def _params(href: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(href).query, keep_blank_values=True)


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


def _make(profile_id, key, **kw):
    share = kw.pop("share", 1.0)
    seller = kw.pop("seller", "owner")
    site = kw.pop("site", "fotocasa")
    campaign = "particular" if seller == "owner" else "professional"
    url = (
        f"https://www.fotocasa.es/es/comprar/terreno/aviles/{abs(hash(key)) % 90000}/d"
        if site == "fotocasa"
        else f"https://www.idealista.com/inmueble/{abs(hash(key)) % 90000}/"
        f"?utm_campaign=express_newAd_sale_{campaign}"
    )
    coords = kw.pop("coords", (43.5, -6.5))
    return Property(
        source_email_id=f"m445_{key}",
        title=kw.pop("title", f"Findable plot {key}"),
        municipality=kw.pop("municipality", "Castrillon"),
        property_category=kw.pop("category", "land"),
        property_subtype=kw.pop("subtype", "plot"),
        price=40000,
        url=url,
        location_lat=coords[0] if coords else None,
        location_lon=coords[1] if coords else None,
        scoring=({"coverage": {"share": share}} if share is not None else None),
        search_profile_id=profile_id,
        listing_status=kw.pop("listing_status", "active"),
        **kw,
    )


@pytest.fixture
def listings(app):
    """One listing per way of *failing* each filter, all of them mappable."""
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        pid = profile.id

        rows = {
            # Matches every filter in FILTERS at once.
            "match_all": _make(pid, "match_all", is_favorite=True),
            "other_category": _make(pid, "other_category", category="housing"),
            "other_subtype": _make(pid, "other_subtype", subtype="house"),
            "other_municipality": _make(
                pid, "other_municipality", municipality="Gijon"
            ),
            "other_source": _make(pid, "other_source", site="idealista"),
            "agency": _make(pid, "agency", seller="agency", site="idealista"),
            "unfindable": _make(pid, "unfindable", title="Nothing matches here"),
            "half_measured": _make(pid, "half_measured", share=0.5),
            "not_favorite": _make(pid, "not_favorite"),
            # The two review filters need a row each that answers them, or
            # `test_the_matrix_bites` correctly reports them as toothless.
            "rejected": _make(pid, "rejected", owner_verdict="rejected"),
            "sea_yes": _make(
                pid,
                "sea_yes",
                enrichment={"environment": {"sea_view": "yes"}},
            ),
            # The one row `sea_dist=800` keeps: every other row has no sea
            # block at all, so the cut bites without a "far" twin.
            "near_sea": _make(
                pid,
                "near_sea",
                enrichment={"sea": {"status": "ok", "distance_m": 350.0}},
            ),
            # The one row `build=solar` keeps -- everything else is uncurated.
            "buildable": _make(
                pid,
                "buildable",
                attributes={"land_classification": "urbano_solar"},
            ),
            "excellent": _make(
                pid,
                "excellent",
                ai_analysis={
                    "rental_market_analysis": {"investment_rating": "EXCELLENT"}
                },
            ),
            "overdue": _make(
                pid,
                "overdue",
                next_action="Ask the agency for the cadastral reference",
                next_action_due_on=date(2020, 1, 1),
            ),
        }
        db.session.add_all(list(rows.values()))
        db.session.commit()
        return {name: row.id for name, row in rows.items()}


def _profile_of(client, listing_id: int) -> int:
    """The subscription a fixture listing belongs to, read from the app."""
    from models import Property as _P

    return db.session.get(_P, listing_id).search_profile_id


def _mapped(client, query: str) -> set[int]:
    return _marker_ids(client.get(f"/map?{query}").get_data(as_text=True))


def _listed(client, query: str) -> set[int]:
    body = client.get(f"/properties?{query}&per_page=100").get_data(as_text=True)
    return _listing_ids(body)


class TestEveryFilterAgrees:
    """One URL, one set -- walked over the whole vocabulary, not by name."""

    @pytest.mark.parametrize("name,value", FILTERS)
    def test_the_map_narrows_exactly_as_the_list_does(
        self, client, listings, name, value
    ):
        query = f"profile_id=all&{name}={value}"
        assert _mapped(client, query) == _listed(client, query), (
            f"?{name}={value} selects different listings on the two surfaces"
        )

    def test_the_sweep_covers_every_filter_the_page_has(self, app, client, listings):
        """The sweep is only a sweep if it walks everything.

        `FILTERS` above is a hand-written table, and a hand-written table is
        what this whole ticket is about, so it is checked rather than trusted:
        `current_filters` is read out of the rendered page -- the same reading
        `tests/test_filters_survive_page_and_sort_links.py` uses -- and every
        key must be swept or excused by name. A filter added to `/properties`
        fails here until somebody gives it a value, which is the failure this
        file exists to produce."""
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            assert client.get("/properties?profile_id=all").status_code == 200
        finally:
            template_rendered.disconnect(record, app)

        assert seen, "properties.html did not render"
        swept = {name for name, _ in FILTERS}
        missing = sorted(
            key
            for key in seen[-1]["current_filters"]
            if key not in swept and key not in NOT_A_FILTER
        )

        assert not missing, (
            f"these filters are applied by /properties and not swept here: "
            f"{missing}. Add a value to FILTERS, or name it in NOT_A_FILTER "
            "with the reason it is not a filter."
        )

    def test_the_matrix_bites(self, client, listings):
        """Each filter must actually exclude something, or a missing filter
        would satisfy the test above without doing anything."""
        everything = _listed(client, "profile_id=all")
        toothless = [
            f"{name}={value}"
            for name, value in FILTERS
            if _listed(client, f"profile_id=all&{name}={value}") == everything
        ]
        assert not toothless, f"these filters excluded nothing: {toothless}"

    def test_measured_is_the_one_that_was_broken(self, client, listings):
        """The instance in the ticket, kept by name as well as by the sweep:
        production showed 72 rows and plotted 470."""
        narrowed = _mapped(client, "profile_id=all&measured=full")

        assert listings["half_measured"] not in narrowed
        assert narrowed != _mapped(client, "profile_id=all")


class TestClearingTheFiltersReallyClears:
    """`_map_focus_link(keep_filters=False)` -- followed, never read."""

    # Each pair is a filter and a listing that filter really hides. Two of
    # them, `source` and `advertiser`, are names the old `dropped` set had gone
    # stale by; `verdict` is a third, and it arrived on the morning of the day
    # this was written, which is the point.
    @pytest.mark.parametrize(
        "hiding,target_key",
        [
            ("source=idealista", "match_all"),
            ("advertiser=agency", "match_all"),
            ("category=housing", "match_all"),
            ("verdict=rejected", "match_all"),
            ("measured=full", "half_measured"),
        ],
    )
    def test_the_recovery_link_shows_the_listing(
        self, client, listings, hiding, target_key
    ):
        target = listings[target_key]
        body = client.get(f"/map?focus={target}&{hiding}").get_data(as_text=True)

        notice = _focus_notice(body)
        assert notice and notice["reason"] == "filtered", (
            f"?{hiding} should hide listing {target} and say so"
        )
        assert notice["href"], "the notice offered no way out"

        # The link must still be *about* this listing. Without this the test
        # cannot tell "filters cleared, listing revealed" from "focus dropped
        # too, so the map shows everything and highlights nothing" -- and an
        # independent review proved it could not: dropping `focus` alongside
        # the filters left all 29 tests in this file green, one token away
        # from reintroducing #287 in the file meant to make that loud.
        assert _params(notice["href"]).get("focus") == [str(target)], (
            "clearing the filters dropped the focus as well, so the notice's "
            "own subject is gone from the link it offers"
        )

        cleared = client.get(notice["href"]).get_data(as_text=True)
        assert target in _marker_ids(cleared), (
            f"following 'Clear the filters' with ?{hiding} did not reveal the "
            "listing -- the filter survived the clearing"
        )
        assert _focus_notice(cleared) is None, (
            "the recovery link landed on the same notice it came from"
        )

    def test_switching_subscription_keeps_the_filters(self, client, listings, app):
        """The other branch of the same helper: `keep_filters=True`.

        The first version of this test looked for other `/map?` links on the
        page and asserted over them -- and `templates/map.html` renders exactly
        one, the recovery link itself, so the list was always empty and the
        assertion never ran. It passed whether or not filters were preserved.
        This builds the notice that actually uses the keeping branch: a focus
        that exists but sits in a subscription the map is not showing."""
        with app.app_context():
            other = SearchProfile(
                name="Another subscription",
                is_active=True,
                is_default=False,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(other)
            db.session.commit()
            elsewhere = _make(other.id, "elsewhere")
            db.session.add(elsewhere)
            db.session.commit()
            elsewhere_id, other_id = elsewhere.id, other.id

        mine = next(iter(listings.values()))
        body = client.get(
            f"/map?focus={elsewhere_id}&profile_id={_profile_of(client, mine)}"
            "&category=land"
        ).get_data(as_text=True)

        notice = _focus_notice(body)
        assert notice and notice["reason"] != "filtered", (
            "this listing is hidden by the subscription, not by the filters"
        )
        assert notice["href"], "the notice offered no way to the other subscription"

        params = _params(notice["href"])
        assert params.get("category") == ["land"], (
            "switching subscription dropped the filters, which is the recovery "
            "link's job and not this one's"
        )
        assert params.get("focus") == [str(elsewhere_id)]
        assert params.get("profile_id") == [str(other_id)]


class TestTheLinksThatLeadToTheMap:
    def test_they_carry_measured(self, client, listings):
        """The route applying the filter is half of it; the button on
        /properties has to send it."""
        body = client.get("/properties?profile_id=all&measured=full").get_data(
            as_text=True
        )
        hrefs = [unescape(h) for h in re.findall(r'href="(/map\?[^"]*)"', body)]
        assert hrefs, "no map link on the page"

        for href in hrefs:
            assert _params(href).get("measured") == ["full"], (
                f"a link to the map dropped the filter the page is applying: {href}"
            )

    @pytest.mark.parametrize("name,value", FILTERS)
    def test_the_round_trip_keeps_the_set(self, client, listings, name, value):
        """/properties -> Map -> List View, following each link, for every
        filter rather than for a chosen one.

        This is parametrized because of what a mutation showed: with a
        hand-written `list_view_args` restored -- stale by `verdict` and
        `action`, exactly as the real one had been within an hour of being
        written -- the single-filter version of this test passed. Only the
        named `measured` case failed, so the suite would have caught the
        instance and missed the class, which is the whole complaint of #445."""
        query = f"profile_id=all&{name}={value}"
        started = _listed(client, query)

        page = client.get(f"/properties?{query}").get_data(as_text=True)
        map_href = unescape(re.search(r'href="(/map\?[^"]*)"', page).group(1))
        map_body = client.get(map_href).get_data(as_text=True)
        back = client.get(_list_view_href(map_body)).get_data(as_text=True)

        assert _listing_ids(back) == started, (
            "the round trip through the map changed which listings are listed"
        )
