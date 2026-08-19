"""`/municipalities` can be narrowed to subscriptions, and says when it is.

MUNIC-002 in #265, the last part of the decision recorded in #410. #421 taught
the page to disclose which subscriptions its numbers are made of; it still
offered no way to narrow them, and `archived` was not available as a name --
it already means removed/sold here.

The control is `profile_id`, the codebase's one spelling of this question,
parsed by `services/profile_selection.py`. What differs is the **fallback**,
which is what `auto_profile_id` exists for: a bare `/properties` is rewritten
to `all`, a bare `/map` resolves to one profile, and a bare `/municipalities`
filters by nothing at all, because this page compares municipalities rather
than saved searches.

That makes this the one surface where `all` is *narrower* than the bare URL.
`all` still means "active and not hidden" here, exactly as it does on
`/properties`, `/map`, the CSV export and the JSON API -- redefining it would
be one token with two meanings across four surfaces, silently, since the
spelling would be identical. The asymmetry is therefore pinned as a fact
rather than left as an understanding.

Two things about the implementation are load bearing and each has its own
case below. The filter goes on the rows **entering** `build_rows`, never on
the rows leaving it: every median, every coverage count, `row["scope"]` and
the drill-down link are all derived from what that function was handed, so
filtering afterwards would leave them describing the unfiltered set while the
page claimed otherwise -- #417 exactly, on a new axis. And every in-page link
is rebuilt from one `base_args` dict, because a link that forgets the
selection does not fail: it silently widens the table and looks like it is
working.
"""

import html as htmlmod
import re

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402

_LINK = re.compile(
    r'<a href="(/properties\?[^"]+)"[^>]*class="text-decoration-none fw-semibold">'
    r"([^<]+)</a>",
    re.S,
)
_END_CELL = re.compile(r'<td class="text-end">(.*?)</td>', re.S)
_FOUND = re.compile(r"<strong>\s*(\d+)\s+[^<]*</strong>")


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
    """One municipality carried by a live and a retired subscription, plus an
    unassigned listing in a municipality of its own."""
    live = SearchProfile(name="Land at Norte", is_active=True)
    retired = SearchProfile(name="Legacy Lands", is_active=False)
    db.session.add_all([live, retired])
    db.session.commit()

    def add(slug, municipality, profile_id):
        db.session.add(
            Property(
                source_email_id=f"scope_{slug}",
                title=f"{slug} listing",
                municipality=municipality,
                search_profile_id=profile_id,
                listing_status="active",
                price=200000,
                area=1000,
            )
        )

    add("live_a", "Gijón", live.id)
    add("live_b", "Gijón", live.id)
    add("retired_a", "Gijon", retired.id)
    add("retired_b", "Gijon", retired.id)
    add("retired_c", "Gijon", retired.id)
    add("orphan", "Avilés", None)
    db.session.commit()
    return {"live": live.id, "retired": retired.id}


def _page(client, query=""):
    response = client.get(f"/municipalities{query}", follow_redirects=False)
    # A template error is a redirect with a flash, and the page it lands on
    # carries none of the markup below either.
    assert response.status_code == 200, f"/municipalities{query} did not render"
    return response.get_data(as_text=True)


def _rows(body):
    """{municipality: (claimed listings, drill-down href)}."""
    rows = {}
    for block in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        link = _LINK.search(block)
        if not link:
            continue
        cells = _END_CELL.findall(block)
        listings = re.sub(r"<[^>]+>", "", cells[4]).strip()
        rows[link.group(2).strip()] = (
            int(listings),
            htmlmod.unescape(link.group(1)),
        )
    return rows


def _total(client, href):
    response = client.get(href, follow_redirects=False)
    assert response.status_code == 200, f"{href} did not render"
    found = _FOUND.search(response.get_data(as_text=True))
    assert found, f"{href} printed no result count"
    return int(found.group(1))


def _scope_line(body):
    match = re.search(r'id="municipalities-scope".*?</div>', body, re.S)
    assert match, "the page rendered no scope line"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(0))).strip()


class TestTheFilterNarrowsWhatTheNumbersAreMadeOf:
    def test_the_number_and_the_link_narrow_together(self, client, world):
        """The #417 contract, under the new axis."""
        wide = _rows(_page(client))["Gijón"]
        narrow = _rows(_page(client, f"?profile_id={world['live']}"))["Gijón"]

        assert wide[0] == 5
        assert narrow[0] == 2
        assert _total(client, wide[1]) == 5
        assert _total(client, narrow[1]) == 2
        # The link narrowed for free, because the filter is on the rows
        # entering `build_rows`: `row["scope"]` is tallied from them.
        assert f"profile_id={world['retired']}" in wide[1]
        assert f"profile_id={world['retired']}" not in narrow[1]

    def test_a_selection_that_reaches_nothing_empties_the_table(self, client, world):
        """An explicit id that matches nothing is answered, not fallen back
        from -- the rule `services/profile_selection.py` already applies to
        `/properties`, reaching this page through the same parser."""
        body = _page(client, "?profile_id=999999")

        assert _rows(body) == {}

    def test_the_unassigned_rows_are_selectable_on_their_own(self, client, world):
        rows = _rows(_page(client, "?profile_id=unassigned"))

        assert set(rows) == {"Avilés"}
        assert rows["Avilés"][0] == 1


class TestAllIsNarrowerThanTheBarePageHere:
    """The asymmetry a reader cannot infer, pinned as a fact.

    On `/properties`, adding `?profile_id=all` to a bare URL changes nothing.
    Here it removes every retired and hidden subscription -- 311 of 772
    listings on production, 2026-08-19. The token is not redefined, because
    four other surfaces read it; what the page does instead is say which of
    the two populations is on screen.
    """

    def test_all_removes_the_retired_subscription(self, client, world):
        bare = _rows(_page(client))
        narrowed = _rows(_page(client, "?profile_id=all"))

        assert bare["Gijón"][0] == 5
        assert narrowed["Gijón"][0] == 2
        assert "Avilés" in bare, "the unassigned listing is in the page's own default"
        assert "Avilés" not in narrowed, "`all` never implies the unassigned rows"

    def test_the_scope_line_stops_claiming_every_stored_listing(self, client, world):
        assert "every stored listing" in _scope_line(_page(client))

        narrowed = _scope_line(_page(client, "?profile_id=all"))
        assert "every stored listing" not in narrowed, (
            "the leading phrase is the page's whole claim and it is false here"
        )
        assert "only the subscriptions selected" in narrowed
        # And the composition says which population that is.
        assert "1 live (2)" in narrowed
        assert "0 retired (0)" in narrowed


class TestTheSelectionSurvivesEveryLinkOnThePage:
    """A link that forgets the selection does not fail; it silently widens."""

    def _hrefs(self, body):
        return [
            htmlmod.unescape(href)
            for href in re.findall(r'href="(/municipalities\?[^"]*)"', body)
        ]

    def test_every_in_page_link_carries_it(self, client, world):
        body = _page(client, f"?profile_id={world['live']}&archived=on&favorites=on")
        hrefs = self._hrefs(body)

        # The sort headers and the two toggles, minus the menu's own options,
        # which exist precisely to change the selection.
        carriers = [
            href
            for href in hrefs
            if "sort=" in href and "profile_id=" not in href.split("sort=")[0]
        ]
        assert carriers, "no sort links rendered"
        for href in hrefs:
            if "profile_id=all" in href or href.endswith("archived=on&favorites=on"):
                continue  # a menu option, deliberately replacing the selection
            if "sort=" not in href:
                continue
            assert f"profile_id={world['live']}" in href, href

    def test_following_a_sort_link_keeps_the_narrowed_table(self, client, world):
        body = _page(client, f"?profile_id={world['live']}")
        sort_href = next(
            htmlmod.unescape(href)
            for href in re.findall(r'href="(/municipalities\?[^"]*)"', body)
            if "sort=price_per_m2" in href
        )

        response = client.get(sort_href)
        assert response.status_code == 200
        assert _rows(response.get_data(as_text=True))["Gijón"][0] == 2


class TestTheMenuReachesEveryPopulationThePageCounts:
    """A control that cannot reach what the page discloses is the defect this
    ticket removes, one axis along.

    `/properties` drops a hidden subscription from its menu entirely -- that
    is what hiding means there, and its own population drops it too. This
    page's population does not: it compares municipalities over every stored
    listing, and its Scope line counts the hidden ones. So the menu lists them
    (`_profile_dropdown_options(..., include_hidden=True)`), while `all` stays
    "active and not hidden" exactly as it is everywhere else.
    """

    @pytest.fixture
    def hidden_pair(self, app):
        """Both kinds, because they reach the menu by different paths.

        A hidden *active* subscription arrives in the `profiles` argument
        (`list_profiles(active_only=True, include_hidden=True)`); a hidden
        *retired* one can only arrive through the archive query, which is the
        one `include_hidden` governs. A test built on the active one alone
        stays green with the flag turned off -- measured, not supposed: that
        is exactly what the first version of this test did.
        """
        live_hidden = SearchProfile(
            name="Solares Norte", is_active=True, is_hidden=True
        )
        retired_hidden = SearchProfile(name="Quesada", is_active=False, is_hidden=True)
        db.session.add_all([live_hidden, retired_hidden])
        db.session.commit()
        for slug, municipality, profile in (
            ("hidden_live", "Cudillero", live_hidden),
            ("hidden_retired", "Colunga", retired_hidden),
        ):
            db.session.add(
                Property(
                    source_email_id=f"scope_{slug}",
                    title=f"{slug} listing",
                    municipality=municipality,
                    search_profile_id=profile.id,
                    listing_status="active",
                    price=1,
                    area=1,
                )
            )
        db.session.commit()
        return {"live": live_hidden.id, "retired": retired_hidden.id}

    def test_both_kinds_of_hidden_subscription_are_offered_here(
        self, client, hidden_pair
    ):
        menu = re.search(
            r'id="municipalities-scope-control".*?</ul>', _page(client), re.S
        )
        assert menu, "the control did not render"

        assert "Solares Norte" in menu.group(0)
        assert "Quesada" in menu.group(0), (
            "a hidden retired subscription reaches the menu only through the "
            "archive query, which is what `include_hidden` governs"
        )

    def test_a_hidden_subscription_is_reachable_and_still_outside_all(
        self, client, hidden_pair
    ):
        body = _page(client)
        assert {"Cudillero", "Colunga"} <= set(_rows(body)), (
            "the page's own default counts both"
        )

        for kind, municipality in (("live", "Cudillero"), ("retired", "Colunga")):
            picked = _rows(_page(client, f"?profile_id={hidden_pair[kind]}"))
            assert set(picked) == {municipality}

        # While `all` still excludes them, the way it does on every other page.
        narrowed = _rows(_page(client, "?profile_id=all"))
        assert "Cudillero" not in narrowed and "Colunga" not in narrowed


class TestTheMenuCountsWhatPickingWouldShow:
    def test_a_subscription_not_selected_still_shows_its_own_count(self, client, world):
        """Counted off the query with the page's other filters applied and the
        subscription filter not yet -- `/properties` counts `unassigned_count`
        in exactly that order and for exactly this reason. Counted afterwards,
        every unselected subscription would read 0 and the menu would offer
        options that look empty.
        """
        body = _page(client, f"?profile_id={world['live']}")
        menu = re.search(r'id="municipalities-scope-control".*?</ul>', body, re.S)
        assert menu, "the control did not render"
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", menu.group(0)))

        assert "Legacy Lands 3" in text, (
            "the retired subscription is not on screen and still holds 3 listings"
        )
        assert "Land at Norte 2" in text
