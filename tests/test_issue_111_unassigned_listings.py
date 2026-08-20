"""Issue #111: listings with no subscription are reachable and honestly counted.

Since #102 a listing can legitimately be stored with `search_profile_id IS
NULL` -- an email linking to several different saved searches, an email whose
search URL was read but could not be resolved, or a deleted profile detaching
its rows (`ondelete="SET NULL"`). #104/#112 gave those rows a way *onto* the
page (the "No subscription" entry in the subscription dropdown). This file
pins what was left over:

* **Reachable where the selection names them.** Both views list every row the
  "No subscription" entry claims, and neither offers a control to file one by
  hand. The issue asked for that control; #130 -- an owner decision taken
  later the same day -- made ingestion the only writer of
  `Property.search_profile_id` and deleted the override route, so the
  contract is now the opposite one and
  `tests/test_subscription_assignment_is_automatic.py` is what pins it.
* **Honest counting.** `profile_id=all` means every *active profile*, so the
  page's total silently excluded rows no selection could reach. The page now
  says whether its total covers them, and the number it discloses has to be
  the number the "show them" link lands on -- which is why the count is
  computed under the filters currently applied, not globally.

The fixture is built to tell a filter-aware count from a global one:
`Hiddenville` holds an unassigned row that the municipality filter excludes,
so an implementation that counts `search_profile_id IS NULL` across the whole
table reports 3 where the destination page shows 2.
`TestFixtureStrength` fails loudly if a later edit flattens that.
"""

import html
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# The municipality that the "narrowing filter" assertions select. One
# unassigned row lives outside it on purpose.
ORPHAN_TOWN = "Orphanville"
HIDDEN_TOWN = "Hiddenville"


def _query_params(href):
    """Query parameters of an href, parsed rather than string-matched."""
    return parse_qs(urlparse(html.unescape(href)).query, keep_blank_values=True)


def _anchor_href(body, needle):
    """href of the single `<a>` whose opening tag contains `needle`."""
    hrefs = []
    for tag in re.findall(r"<a\b[^>]*>", body):
        if needle in tag:
            match = re.search(r'href="([^"]*)"', tag)
            if match:
                hrefs.append(match.group(1))
    assert len(hrefs) == 1, f"expected exactly one <a> matching {needle!r}, got {hrefs}"
    return hrefs[0]


def _listing_ids_in_order(body):
    """Property ids in the order the page renders them, de-duplicated."""
    seen = set()
    order = []
    for match in re.finditer(r'href="/properties/(\d+)"', body):
        listing_id = int(match.group(1))
        if listing_id not in seen:
            seen.add(listing_id)
            order.append(listing_id)
    return order


def _unassigned_note(body):
    """Text of the disclosure next to the total, or None when absent."""
    match = re.search(r'<span id="unassigned-count-note"[^>]*>(.*?)</span>', body, re.S)
    if not match:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def _assign_forms(body):
    """`{property_id: form_markup}` for every per-row assign form on the page."""
    forms = {}
    for match in re.finditer(
        r'<form\b[^>]*action="/properties/(\d+)/profile"[^>]*>(.*?)</form>', body, re.S
    ):
        forms[int(match.group(1))] = match.group(0)
    return forms


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
def listings(app):
    """Two active subscriptions with rows, plus three rows with none at all.

    Two of the unassigned rows sit in `Orphanville` and one in `Hiddenville`,
    so a `municipality=Orphanville` view has to disclose 2 while the whole
    table holds 3. A count computed globally passes every other assertion in
    this file and fails that one.
    """
    base = datetime(2026, 3, 1, 9, 0, 0)

    with app.app_context():
        alpha = SearchProfile(
            name="Alpha subscription", is_active=True, is_default=True
        )
        beta = SearchProfile(name="Beta subscription", is_active=True, is_default=False)
        db.session.add_all([alpha, beta])
        db.session.commit()

        created = {"alpha": [], "beta": [], "orphan": [], "hidden_orphan": []}
        clock = [0]

        def _make(slug, profile_id, municipality, sea_view=None):
            clock[0] += 1
            prop = Property(
                source_email_id=f"issue111_{slug}_{clock[0]}",
                title=f"{slug.capitalize()}Listing{clock[0]:02d}UniqueTitle",
                municipality=municipality,
                search_profile_id=profile_id,
                listing_status="active",
                price=200000 + clock[0] * 1000,
                area=400 + clock[0] * 5,
                score_total=50 + clock[0],
                created_at=base + timedelta(hours=clock[0]),
                enrichment=(
                    {"environment": {"sea_view": sea_view}} if sea_view else None
                ),
            )
            db.session.add(prop)
            db.session.flush()
            created[slug].append(prop.id)

        for _ in range(3):
            _make("alpha", alpha.id, "Alphaville")
        for _ in range(2):
            _make("beta", beta.id, "Betaville")
        # The two in-town orphans carry a confirmed sea view and the out-of-town
        # one does not, so `sea_view=yes` narrows the unassigned bucket to the
        # same subset `municipality=Orphanville` does. That gives the count a
        # second, independently-added filter to follow -- see
        # `test_the_count_follows_the_other_filters`.
        for _ in range(2):
            _make("orphan", None, ORPHAN_TOWN, sea_view="yes")
        _make("hidden_orphan", None, HIDDEN_TOWN)

        db.session.commit()

        return {
            "alpha_id": alpha.id,
            "beta_id": beta.id,
            "alpha_props": list(created["alpha"]),
            "beta_props": list(created["beta"]),
            # Every row with no subscription, and the subset a
            # `municipality=Orphanville` view keeps.
            "orphan_props": list(created["orphan"]) + list(created["hidden_orphan"]),
            "orphan_props_in_town": list(created["orphan"]),
        }


class TestFixtureStrength:
    def test_the_fixture_can_tell_a_wrong_implementation_apart(self, listings):
        alpha = set(listings["alpha_props"])
        beta = set(listings["beta_props"])
        orphans = set(listings["orphan_props"])
        in_town = set(listings["orphan_props_in_town"])

        assert alpha and beta and orphans
        assert not orphans & (alpha | beta), "unassigned rows must not be in a profile"
        assert in_town < orphans, (
            "one unassigned row has to fall outside the narrowing filter, or a "
            "globally-computed count would pass the filtered assertions too"
        )
        assert len(orphans) != len(in_town)

    def test_every_orphan_really_has_no_profile(self, app, listings):
        with app.app_context():
            rows = Property.query.filter(Property.search_profile_id.is_(None)).all()
            assert {row.id for row in rows} == set(listings["orphan_props"])


class TestReachableWhereTheySit:
    """AC1: the "No subscription" selection reaches exactly the rows it names.

    The issue also asked for a way to assign one of those rows by hand (AC2).
    That is deliberately not built: #130, an owner decision taken later the
    same day, made ingestion the only writer of a listing's subscription and
    deleted the override route. So the second test below pins the absence of
    the control rather than its presence, and the route itself is pinned in
    `tests/test_subscription_assignment_is_automatic.py`.
    """

    @pytest.mark.parametrize("view_type", ["list", "cards"])
    def test_the_selection_lists_every_unassigned_row(
        self, client, listings, view_type
    ):
        body = client.get(
            f"/properties?profile_id=unassigned&view_type={view_type}&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(listings["orphan_props"]), (
            "the selection has to reach the listings it is named after"
        )

    @pytest.mark.parametrize("view_type", ["list", "cards"])
    def test_no_row_offers_to_file_itself_under_a_subscription(
        self, client, listings, view_type
    ):
        """Matched on the POST target rather than on wording: a control that
        came back under a new label would still post to `/properties/<id>/
        profile`, and that is the thing #130 removed."""
        body = client.get(
            f"/properties?profile_id=unassigned&view_type={view_type}&per_page=100"
        ).get_data(as_text=True)
        assert _listing_ids_in_order(body), "the fixture rows should be on screen"
        assert _assign_forms(body) == {}


class TestHonestCounting:
    """AC3: the total says whether it covers the unassigned listings."""

    @pytest.mark.parametrize("query", ["", "?profile_id=all"])
    def test_a_selection_that_excludes_them_says_so(self, client, listings, query):
        expected = len(listings["orphan_props"])
        body = client.get(f"/properties{query}").get_data(as_text=True)
        note = _unassigned_note(body)
        assert note, "the page must disclose the rows its total leaves out"
        assert str(expected) in note
        assert "no subscription" in note.lower()

        href = _anchor_href(body, 'id="unassigned-count-link"')
        assert _query_params(href).get("profile_id") == ["unassigned"]

    def test_following_the_link_shows_exactly_the_disclosed_rows(
        self, client, listings
    ):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        href = html.unescape(_anchor_href(body, 'id="unassigned-count-link"'))
        landed = client.get(f"{href}&per_page=100").get_data(as_text=True)
        assert set(_listing_ids_in_order(landed)) == set(listings["orphan_props"])

    @pytest.mark.parametrize(
        "param,value",
        [
            ("municipality", ORPHAN_TOWN),
            # `sea_view` arrived in #141, after this disclosure was written, and
            # is applied to the query a few lines above the count. A filter
            # added *below* the count instead would leave the note advertising
            # rows the link cannot reach, silently and only under that filter --
            # so the contract is checked against more than the one filter it
            # was designed with.
            ("sea_view", "yes"),
        ],
    )
    def test_the_count_follows_the_other_filters(self, client, listings, param, value):
        """A globally-computed count would advertise 3 and land on 2."""
        narrowed = len(listings["orphan_props_in_town"])
        assert narrowed < len(listings["orphan_props"])

        body = client.get(
            f"/properties?profile_id=all&{param}={value}&per_page=100"
        ).get_data(as_text=True)
        note = _unassigned_note(body)
        assert note and str(narrowed) in note
        assert str(len(listings["orphan_props"])) not in note

        href = html.unescape(_anchor_href(body, 'id="unassigned-count-link"'))
        params = _query_params(href)
        assert params.get("profile_id") == ["unassigned"]
        assert params.get(param) == [value], (
            f"the disclosure link dropped {param}, so it lands on a wider set "
            "than the number it advertises"
        )

        landed = client.get(f"{href}&per_page=100").get_data(as_text=True)
        assert set(_listing_ids_in_order(landed)) == set(
            listings["orphan_props_in_town"]
        )

    def test_selecting_them_says_the_total_includes_them(self, client, listings):
        body = client.get("/properties?profile_id=unassigned&per_page=100").get_data(
            as_text=True
        )
        note = _unassigned_note(body)
        assert note and "includes" in note.lower()
        assert str(len(listings["orphan_props"])) in note
        assert "not shown" not in note.lower()

    def test_no_unassigned_rows_means_no_disclosure(self, app, client, listings):
        with app.app_context():
            Property.query.filter(Property.search_profile_id.is_(None)).delete(
                synchronize_session=False
            )
            db.session.commit()

        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert _unassigned_note(body) is None
        assert 'id="unassigned-count-link"' not in body

    def test_it_still_discloses_when_no_profile_is_active(self, app, client, listings):
        """`all` over an empty active set shows nothing at all -- which is
        exactly when a silently-excluded bucket is hardest to notice."""
        with app.app_context():
            for profile in SearchProfile.query.all():
                profile.is_active = False
            db.session.commit()

        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        assert _listing_ids_in_order(body) == []
        note = _unassigned_note(body)
        assert note and str(len(listings["orphan_props"])) in note


class TestProfilesPageCountsThem:
    def test_the_page_shows_a_no_subscription_row(self, client, listings):
        body = client.get("/profiles").get_data(as_text=True)
        row = re.search(r'<tr id="profiles-unassigned-row".*?</tr>', body, re.S)
        assert row, "/profiles must account for the listings that have no profile"
        markup = row.group(0)
        assert "No subscription" in markup
        assert ">Edit<" not in markup, "it is not a profile: nothing to edit"

        href = _anchor_href(markup, "profile_id=unassigned")
        assert _query_params(href).get("profile_id") == ["unassigned"]
        count = re.search(
            r'href="[^"]*profile_id=unassigned[^"]*"[^>]*>\s*(\d+)\s*</a>', markup
        )
        assert count and int(count.group(1)) == len(listings["orphan_props"])

    def test_the_per_profile_counts_still_name_real_profiles(self, client, listings):
        body = client.get("/profiles").get_data(as_text=True)
        for profile_key, listing_key in (
            ("alpha_id", "alpha_props"),
            ("beta_id", "beta_props"),
        ):
            # The id has to end at a boundary: `profile_id=1[^"]*` would also
            # match `profile_id=10`. The link carries more than the id now --
            # it states `hide_removed` too, because this count is unfiltered
            # and the page it opens would otherwise apply its own default.
            match = re.search(
                r'href="[^"]*profile_id=%d(?:&[^"]*)?"[^>]*>\s*(\d+)\s*</a>'
                % listings[profile_key],
                body,
            )
            assert match, f"no count cell for profile {listings[profile_key]}"
            assert int(match.group(1)) == len(listings[listing_key])
