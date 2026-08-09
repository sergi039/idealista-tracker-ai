"""Issue #104: multi-select subscriptions on /properties.

Before this change `profile_id` held exactly one value: a single id, the
`all` sentinel, or nothing at all. The owner wants the third option -- tick
several subscriptions in one dropdown -- without the filter bar growing a
second control.

What these tests pin, and why each one is here:

* **The state is explicit.** `auto | all | selected(ids)` are three distinct
  states, not two states and an inference. An empty tick list must *not*
  quietly become "all": the form therefore always posts something explicit,
  and that is asserted on the rendered markup, not assumed.
* **`all` means all *active* profiles.** The dropdown lists only active ones,
  so "all" showing rows from a retired subscription would show more than the
  user can see. An inactive profile stays reachable by explicit id.
* **The selection survives every transition.** Column sorting, both
  pagination directions, the CSV export, `/map`, the cards/list toggle and
  the three scoring modes each rebuild the URL, and any one of them dropping
  a value silently changes *which* listings are on screen. Every link is
  checked by parsing its own href with `parse_qs` -- a substring search would
  pass on `profile_id=[6, 8]`, which is exactly how `url_for` fails when a
  list is stringified instead of repeated.
* **Profile-specific travel data is hidden for a multi-profile selection.**
  Custom travel target ids belong to one profile; a union of two profiles'
  targets would label a column with a target the row was never measured
  against. The page says why instead of guessing.

The fixture is deliberately discriminating: three active profiles plus one
retired one, interleaved in time so both pagination pages hold rows from both
selected subscriptions. `test_fixture_can_tell_the_implementations_apart`
fails loudly if a later edit makes the data so uniform that a broken
implementation (first id only / every profile / active only) would still
satisfy the assertions.
"""

import csv as csv_module
import html
import io
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app, db
from models import Property, SearchProfile
from services.profile_selection import (
    TRAVEL_NOTICE,
    ProfileSelectionState,
    parse_profile_selection,
    resolve_profile_selection,
)
from tests import setup_test_environment

# `/properties` clamps per_page to a minimum of 10, so the union has to be
# larger than that for pagination to split at all.
PER_PAGE = 10


def _query_params(href):
    """Query parameters of an href, parsed rather than string-matched.

    `"profile_id=6" in href` passes for `profile_id=6` and equally for
    `profile_id=[6, 8]` or `profile_id=68`. Parsing is the only way to prove
    `url_for` repeated the parameter instead of stringifying the list.
    """
    return parse_qs(urlparse(html.unescape(href)).query, keep_blank_values=True)


def _anchor_hrefs(body, needle):
    """href of every `<a>` whose opening tag contains `needle`."""
    hrefs = []
    for tag in re.findall(r"<a\b[^>]*>", body):
        if needle not in tag:
            continue
        match = re.search(r'href="([^"]*)"', tag)
        if match:
            hrefs.append(match.group(1))
    return hrefs


def _anchor_href(body, needle):
    hrefs = _anchor_hrefs(body, needle)
    assert len(hrefs) == 1, f"expected exactly one <a> matching {needle!r}, got {hrefs}"
    return hrefs[0]


def _pagination_hrefs(body):
    """The Prev/Next hrefs, keyed by label (the label is the link text)."""
    found = {}
    for href, label in re.findall(
        r'<a[^>]*href="([^"]*)"[^>]*>\s*(Prev|Next)\s*</a>', body
    ):
        found[label] = href
    return found


def _listing_ids_in_order(body):
    """Property ids in the order the page renders them, de-duplicated.

    Both views link a row to `/properties/<id>` more than once (title and the
    details button), so first occurrence wins and the result is the visible
    row order.
    """
    seen = set()
    order = []
    for match in re.finditer(r'href="/properties/(\d+)"', body):
        listing_id = int(match.group(1))
        if listing_id not in seen:
            seen.add(listing_id)
            order.append(listing_id)
    return order


def _marker_ids(body):
    """Property ids of the markers `/map` handed to Leaflet."""
    match = re.search(r"const markers = (\[.*?\]);", body, re.S)
    assert match, "the map page no longer emits a `const markers = [...]` literal"
    return [int(marker["id"]) for marker in json.loads(match.group(1))]


def _csv_ids_in_order(text):
    reader = csv_module.reader(io.StringIO(text))
    rows = list(reader)
    header = rows[0]
    id_col = header.index("ID")
    return [int(row[id_col]) for row in rows[1:]]


def _args(query):
    """A `request.args`-shaped MultiDict built from a raw query string."""
    return MultiDict(
        [
            (key, value)
            for key, values in parse_qs(query, keep_blank_values=True).items()
            for value in values
        ]
    )


def _resolve(query, active_profile_ids, auto=None):
    return resolve_profile_selection(
        parse_profile_selection(_args(query)), active_profile_ids, auto_profile_id=auto
    )


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
def subscriptions(app):
    """Three active subscriptions plus a retired one.

    * `alpha` and `beta` are the pair the tests select together. Their rows
      are interleaved in time, so a correct union spans both pagination pages
      and neither profile can be satisfied by accident.
    * `gamma` is active and holds rows that must never appear in an
      alpha+beta selection -- it is what separates "the union" from "no
      filter at all".
    * `zeta` is inactive: `all` must skip it, an explicit id must reach it.

    Each profile carries its own custom travel target, which is the concrete
    reason a union cannot show profile-specific travel data: the target ids
    belong to different profiles.
    """
    from services.search_profile_service import TRAVEL_PRESET_DEFS

    base = datetime(2026, 1, 1, 9, 0, 0)

    def _targets(slug, label):
        # Presets off: the card view renders only the first four targets, so
        # six enabled presets would hide the custom target this fixture is
        # about and the travel assertions would prove nothing.
        return {
            "presets": {
                key: {"enabled": False, "mode": "driving"} for key in TRAVEL_PRESET_DEFS
            },
            "custom": [
                {
                    "id": f"{slug}-target",
                    "name": label,
                    "lat": 38.3,
                    "lon": -0.5,
                    "mode": "driving",
                }
            ],
        }

    with app.app_context():
        alpha = SearchProfile(
            name="Alpha subscription",
            is_active=True,
            is_default=True,
            travel_targets=_targets("alpha", "AlphaOfficeTarget"),
        )
        beta = SearchProfile(
            name="Beta subscription",
            is_active=True,
            is_default=False,
            travel_targets=_targets("beta", "BetaOfficeTarget"),
        )
        gamma = SearchProfile(
            name="Gamma subscription",
            is_active=True,
            is_default=False,
            travel_targets=_targets("gamma", "GammaOfficeTarget"),
        )
        zeta = SearchProfile(
            name="Zeta retired subscription",
            is_active=False,
            is_default=False,
            travel_targets=_targets("zeta", "ZetaOfficeTarget"),
        )
        db.session.add_all([alpha, beta, gamma, zeta])
        db.session.commit()

        created = {"alpha": [], "beta": [], "gamma": [], "zeta": [], "orphan": []}
        clock = [0]

        def _make(slug, profile_id, index):
            clock[0] += 1
            prop = Property(
                source_email_id=f"issue104_{slug}_{index}",
                title=f"{slug.capitalize()}Listing{index:02d}UniqueTitle",
                municipality=f"{slug.capitalize()}ville",
                search_profile_id=profile_id,
                listing_status="active",
                price=100000 + clock[0] * 1000,
                area=500 + clock[0] * 7,
                score_total=40 + clock[0],
                score_investment=90 - clock[0],
                score_lifestyle=30 + clock[0] * 2,
                location_lat=38.0 + clock[0] * 0.01,
                location_lon=-0.9 + clock[0] * 0.01,
                created_at=base + timedelta(hours=clock[0]),
            )
            db.session.add(prop)
            db.session.flush()
            created[slug].append(prop.id)

        # Interleaved on purpose: with alpha entirely newer than beta, a page
        # of ten could be pure alpha and still look like a working union.
        for index in range(6):
            _make("alpha", alpha.id, index)
            _make("beta", beta.id, index)
        for index in range(3):
            _make("gamma", gamma.id, index)
        for index in range(2):
            _make("zeta", zeta.id, index)
        # Listings with no subscription at all. #110 gives ingestion two
        # legitimate ways to persist one: an email carrying several different
        # search links, and a recognised email whose profile lookup lost a
        # concurrent write.
        for index in range(2):
            _make("orphan", None, index)

        db.session.commit()

        return {
            "alpha_id": alpha.id,
            "beta_id": beta.id,
            "gamma_id": gamma.id,
            "zeta_id": zeta.id,
            "alpha_props": list(created["alpha"]),
            "beta_props": list(created["beta"]),
            "gamma_props": list(created["gamma"]),
            "zeta_props": list(created["zeta"]),
            "orphan_props": list(created["orphan"]),
        }


@pytest.fixture
def pair(subscriptions):
    """The `?profile_id=<alpha>&profile_id=<beta>` query and its expectations.

    `paged_query` pins `per_page` to the minimum the route allows; the default
    of 25 would fit the whole union on one page and quietly skip every
    pagination assertion.
    """
    alpha, beta = subscriptions["alpha_id"], subscriptions["beta_id"]
    query = f"profile_id={alpha}&profile_id={beta}"
    return {
        "query": query,
        "paged_query": f"{query}&per_page={PER_PAGE}",
        "ids": [str(alpha), str(beta)],
        "union": set(subscriptions["alpha_props"]) | set(subscriptions["beta_props"]),
    }


class TestFixtureStrength:
    def test_fixture_can_tell_the_implementations_apart(self, subscriptions):
        """A fixture that a broken implementation also satisfies proves nothing.

        Three wrong answers have to be distinguishable from the right one:
        keeping only the first id, ignoring the filter entirely, and treating
        the selection as "every active profile".
        """
        alpha = set(subscriptions["alpha_props"])
        beta = set(subscriptions["beta_props"])
        gamma = set(subscriptions["gamma_props"])
        zeta = set(subscriptions["zeta_props"])
        orphan = set(subscriptions["orphan_props"])
        union = alpha | beta

        assert alpha and beta and gamma and zeta and orphan
        assert not alpha & beta, "the two selected profiles must not share rows"
        assert union != alpha, "first-id-only would pass"
        assert union != beta, "last-id-only would pass"
        assert union != union | gamma, "every-active-profile would pass"
        assert union != union | gamma | zeta | orphan, "no-filter-at-all would pass"
        assert union != union | orphan, "including unassigned rows would pass"
        assert len(union) > PER_PAGE, "the union must not fit on a single page"

    def test_both_pagination_pages_hold_rows_from_both_profiles(
        self, client, subscriptions, pair
    ):
        """Interleaving is load-bearing: a page that happens to be all-alpha
        cannot distinguish a working union from a dropped second id."""
        alpha = set(subscriptions["alpha_props"])
        beta = set(subscriptions["beta_props"])
        for page in (1, 2):
            body = client.get(
                f"/properties?{pair['paged_query']}&page={page}&view_type=list"
            ).get_data(as_text=True)
            shown = set(_listing_ids_in_order(body))
            assert shown & alpha, f"page {page} has no alpha row"
            assert shown & beta, f"page {page} has no beta row"


class TestPropertiesPageShowsTheUnion:
    def test_two_profiles_show_exactly_their_union(self, client, subscriptions, pair):
        shown = set()
        for page in (1, 2):
            body = client.get(
                f"/properties?{pair['paged_query']}&page={page}&view_type=list"
            ).get_data(as_text=True)
            shown |= set(_listing_ids_in_order(body))
        assert shown == pair["union"]

    def test_the_other_active_profile_is_excluded(self, client, subscriptions, pair):
        body = client.get(f"/properties?{pair['query']}&per_page=100").get_data(
            as_text=True
        )
        shown = set(_listing_ids_in_order(body))
        assert shown == pair["union"]
        assert not shown & set(subscriptions["gamma_props"])

    def test_repeating_the_same_id_is_deduplicated(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        body = client.get(
            f"/properties?profile_id={alpha}&profile_id={alpha}&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(subscriptions["alpha_props"])

    def test_all_covers_every_active_profile(self, client, subscriptions):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        shown = set(_listing_ids_in_order(body))
        expected = (
            set(subscriptions["alpha_props"])
            | set(subscriptions["beta_props"])
            | set(subscriptions["gamma_props"])
        )
        assert shown == expected

    def test_all_does_not_reach_an_inactive_profile(self, client, subscriptions):
        """The dropdown lists active profiles only, so "all" must not show
        rows the user has no way to select."""
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        shown = set(_listing_ids_in_order(body))
        assert not shown & set(subscriptions["zeta_props"])

    def test_an_inactive_profile_is_reachable_by_explicit_id(
        self, client, subscriptions
    ):
        zeta = subscriptions["zeta_id"]
        body = client.get(f"/properties?profile_id={zeta}&per_page=100").get_data(
            as_text=True
        )
        assert set(_listing_ids_in_order(body)) == set(subscriptions["zeta_props"])

    def test_an_active_and_an_inactive_id_can_be_selected_together(
        self, client, subscriptions
    ):
        alpha, zeta = subscriptions["alpha_id"], subscriptions["zeta_id"]
        body = client.get(
            f"/properties?profile_id={alpha}&profile_id={zeta}&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(
            subscriptions["alpha_props"]
        ) | set(subscriptions["zeta_props"])

    def test_an_unknown_id_shows_nothing_rather_than_another_profile(
        self, client, subscriptions
    ):
        """Falling back to the auto-selected profile here would answer a
        question the user did not ask, and look like a working filter."""
        unknown = (
            max(
                subscriptions[key]
                for key in ("alpha_id", "beta_id", "gamma_id", "zeta_id")
            )
            + 5000
        )
        body = client.get(f"/properties?profile_id={unknown}").get_data(as_text=True)
        assert _listing_ids_in_order(body) == []

    @pytest.mark.parametrize("raw", ["0", "-1", "99999999999999999999"])
    def test_an_impossible_id_renders_an_empty_page(self, client, subscriptions, raw):
        """This is the only route that reaches the empty-`IN ()` branch of
        `apply_profile_filter`; the parser unit tests stop short of the
        database."""
        resp = client.get(f"/properties?profile_id={raw}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert _listing_ids_in_order(body) == []
        # And the emptiness is sticky -- a sort link must not slide the page
        # back to the auto-selected profile.
        for href in _anchor_hrefs(body, "lands-th-link") or [
            _anchor_href(body, 'id="mode-combined-btn"')
        ]:
            assert _query_params(href).get("profile_id") == ["0"]

    def test_the_button_does_not_claim_all_profiles_on_an_empty_page(
        self, client, subscriptions
    ):
        body = client.get("/properties?profile_id=0").get_data(as_text=True)
        button = re.search(
            r'id="profile-select-dropdown"[^>]*>(.*?)</button>', body, re.S
        )
        assert button
        assert "All subscriptions" not in button.group(1)
        assert "0 selected" in button.group(1)


class TestNoSubscriptionOption:
    """Listings with `search_profile_id IS NULL` get their own dropdown entry.

    They are not a profile, so `all` does not cover them and no id reaches
    them -- without a dedicated option they would be unreachable from every
    view, which is not theoretical: #110 gives ingestion two legitimate ways
    to persist one (an email carrying several different search links, and a
    recognised email whose profile lookup lost a concurrent write).
    """

    def test_all_still_does_not_include_them(self, client, subscriptions):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        shown = set(_listing_ids_in_order(body))
        assert not shown & set(subscriptions["orphan_props"])

    def test_the_option_shows_exactly_the_unassigned_rows(self, client, subscriptions):
        body = client.get("/properties?profile_id=unassigned&per_page=100").get_data(
            as_text=True
        )
        assert set(_listing_ids_in_order(body)) == set(subscriptions["orphan_props"])

    def test_it_combines_with_a_profile(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        body = client.get(
            f"/properties?profile_id={alpha}&profile_id=unassigned&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(
            subscriptions["alpha_props"]
        ) | set(subscriptions["orphan_props"])

    def test_the_dropdown_offers_it_beside_the_profiles(self, client, subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        checkbox = re.search(
            r'<input[^>]*name="profile_id"[^>]*value="unassigned"[^>]*>', body
        )
        assert checkbox, "the dropdown needs a No subscription entry"
        assert "checked" not in checkbox.group(0), "all profiles must not tick it"

    def test_the_dropdown_ticks_it_when_it_is_selected(self, client, subscriptions):
        body = client.get("/properties?profile_id=unassigned").get_data(as_text=True)
        checkbox = re.search(
            r'<input[^>]*name="profile_id"[^>]*value="unassigned"[^>]*>', body
        )
        assert checkbox and "checked" in checkbox.group(0)

    def test_the_choice_survives_every_link(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        expected = [str(alpha), "unassigned"]
        body = client.get(
            f"/properties?profile_id={alpha}&profile_id=unassigned&view_type=list"
        ).get_data(as_text=True)
        hrefs = (
            _anchor_hrefs(body, "lands-th-link")
            + [_anchor_href(body, "Download filtered data as CSV")]
            + [_anchor_href(body, "View properties on map")]
            + [_anchor_href(body, 'id="mode-investment-btn"')]
            + [_anchor_href(body, 'id="view-cards-btn"')]
        )
        assert len(hrefs) >= 8
        for href in hrefs:
            assert _query_params(href).get("profile_id") == expected, href

    def test_the_map_and_the_csv_agree_with_the_page(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        expected = set(subscriptions["alpha_props"]) | set(
            subscriptions["orphan_props"]
        )
        query = f"profile_id={alpha}&profile_id=unassigned"

        markers = _marker_ids(client.get(f"/map?{query}").get_data(as_text=True))
        assert set(markers) == expected

        csv_ids = _csv_ids_in_order(
            client.get(f"/properties/export.csv?{query}").get_data(as_text=True)
        )
        assert set(csv_ids) == expected

    def test_it_hides_profile_specific_travel(self, client, subscriptions):
        """An unassigned row has no profile, so there is no profile-specific
        travel configuration to show -- with or without a profile alongside."""
        alpha = subscriptions["alpha_id"]
        alone = client.get("/properties?profile_id=unassigned").get_data(as_text=True)
        assert TRAVEL_NOTICE in alone
        assert "Recalculate travel" not in alone

        mixed = client.get(
            f"/properties?profile_id={alpha}&profile_id=unassigned"
        ).get_data(as_text=True)
        assert TRAVEL_NOTICE in mixed
        assert "AlphaOfficeTarget" not in mixed
        assert "Recalculate travel" not in mixed

    def test_the_button_counts_it_as_one_entry(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        body = client.get(
            f"/properties?profile_id={alpha}&profile_id=unassigned"
        ).get_data(as_text=True)
        button = re.search(
            r'id="profile-select-dropdown"[^>]*>(.*?)</button>', body, re.S
        )
        assert button and "2 selected" in button.group(1)


class TestExplicitlyEmptySelectionSurvivesApply:
    """`?profile_id=0` is a deliberate empty selection, and pressing Apply for
    an unrelated filter must not widen it.

    Nothing is ticked in that state, so the form falls back to its hidden
    `profile_id` -- which therefore has to carry the impossible marker rather
    than `all`, or the page silently jumps from "nothing" to every active
    profile.
    """

    def test_the_hidden_fallback_carries_the_impossible_marker(
        self, client, subscriptions
    ):
        body = client.get("/properties?profile_id=0").get_data(as_text=True)
        hidden = re.search(
            r'<input[^>]*type="hidden"[^>]*name="profile_id"[^>]*>', body
        )
        assert hidden, "the form still needs an explicit fallback"
        assert 'value="0"' in hidden.group(0), hidden.group(0)

    def test_replaying_that_submit_keeps_the_page_empty(self, client, subscriptions):
        """What the browser sends when the user changes another filter and
        presses Apply without touching the dropdown."""
        body = client.get("/properties?profile_id=0").get_data(as_text=True)
        hidden = re.search(
            r'<input[^>]*type="hidden"[^>]*name="profile_id"[^>]*value="([^"]*)"', body
        )
        assert hidden
        resubmitted = client.get(
            f"/properties?profile_id={hidden.group(1)}&favorites=on&per_page=100"
        ).get_data(as_text=True)
        assert _listing_ids_in_order(resubmitted) == []

    def test_a_normal_state_still_falls_back_to_all(self, client, subscriptions):
        for query in ("profile_id=all", f"profile_id={subscriptions['alpha_id']}", ""):
            body = client.get(f"/properties?{query}").get_data(as_text=True)
            hidden = re.search(
                r'<input[^>]*type="hidden"[^>]*name="profile_id"[^>]*>', body
            )
            assert hidden and 'value="all"' in hidden.group(0), query


class TestTruncationIsNotSilent:
    """The parser caps the id list before it reaches `IN (...)`. Dropping the
    overflow without a word would quietly narrow the page, the map, the export
    and every link at once."""

    def test_the_page_says_so_when_it_truncates(self, client, subscriptions):
        from services.profile_selection import MAX_SELECTED_PROFILE_IDS

        query = "&".join(
            f"profile_id={n}" for n in range(1, MAX_SELECTED_PROFILE_IDS + 12)
        )
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        assert "profile-selection-truncated" in body
        assert str(MAX_SELECTED_PROFILE_IDS) in body

    def test_a_normal_selection_says_nothing(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        assert "profile-selection-truncated" not in body


class TestFilterBarKeepsOneControl:
    def test_the_profile_control_is_a_single_dropdown(self, client, subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert body.count('id="profile-select-dropdown"') == 1
        assert '<select name="profile_id"' not in body

    def test_the_dropdown_lists_the_live_and_the_archived(self, client, subscriptions):
        """Amended 2026-08-09: a retired subscription is offered as archive.

        It used to be left out of the dropdown unless an id named it, which
        made its listings unreachable from the one surface that is supposed to
        hold everything. `all` still means the live subscriptions only -- that
        is tested elsewhere -- so offering the archived one narrows nothing by
        itself.
        """
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert body.count('id="profile-select-menu"') == 1
        checkbox_values = re.findall(
            r'<input[^>]*name="profile_id"[^>]*value="(\d+)"', body
        )
        assert sorted(int(v) for v in checkbox_values) == sorted(
            [
                subscriptions["alpha_id"],
                subscriptions["beta_id"],
                subscriptions["gamma_id"],
                subscriptions["zeta_id"],
            ]
        )
        # The archived one is labelled as such rather than sitting among the
        # live subscriptions unmarked.
        zeta_label = re.search(
            rf'for="profile-option-{subscriptions["zeta_id"]}"(.*?)</label>',
            body,
            re.S,
        )
        assert zeta_label and "Archive" in zeta_label.group(1)

    def test_the_form_always_posts_something_explicit(self, client, subscriptions):
        """Unticking every box must not submit an empty selection that the
        server would read as "no parameter at all" (which means auto)."""
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert re.search(
            r'<input[^>]*type="hidden"[^>]*name="profile_id"[^>]*value="all"', body
        ), "the filter form needs an explicit all-profiles fallback value"

    def test_button_reads_all_profiles_when_nothing_is_narrowed(
        self, client, subscriptions
    ):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        button = re.search(
            r'id="profile-select-dropdown"[^>]*>(.*?)</button>', body, re.S
        )
        assert button, "the dropdown toggle must be a button"
        assert "All subscriptions" in button.group(1)

    def test_button_counts_the_selected_profiles(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        button = re.search(
            r'id="profile-select-dropdown"[^>]*>(.*?)</button>', body, re.S
        )
        assert button
        assert "2 selected" in button.group(1)

    def test_a_selected_inactive_profile_still_gets_a_checkbox(
        self, client, subscriptions
    ):
        """Otherwise the page's own script -- which recomputes the state from
        the checkboxes -- reads the selection as "nothing ticked", and the
        next Apply silently widens the view to every active profile."""
        zeta = subscriptions["zeta_id"]
        body = client.get(f"/properties?profile_id={zeta}").get_data(as_text=True)
        checkbox = re.search(
            rf'<input[^>]*name="profile_id"[^>]*value="{zeta}"[^>]*>', body
        )
        assert checkbox, "the selected inactive profile has no checkbox to untick"
        assert "checked" in checkbox.group(0)
        assert "Archive" in body

    def test_an_unrenderable_selection_is_not_lost_on_the_next_submit(
        self, client, subscriptions
    ):
        """Replay what the browser posts when the user presses Apply without
        touching the dropdown: every ticked box, plus the form's explicit
        `all` fallback. The inactive profile has to survive it."""
        zeta = subscriptions["zeta_id"]
        body = client.get(f"/properties?profile_id={zeta}").get_data(as_text=True)
        ticked = re.findall(
            r'<input[^>]*name="profile_id"[^>]*value="(\d+)"[^>]*checked', body
        )
        assert ticked == [str(zeta)]
        resubmitted = client.get(
            "/properties?profile_id=all"
            + "".join(f"&profile_id={value}" for value in ticked)
            + "&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(resubmitted)) == set(
            subscriptions["zeta_props"]
        )

    def test_checkboxes_reflect_the_current_selection(
        self, client, subscriptions, pair
    ):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        checked = set()
        for tag in re.findall(r"<input\b[^>]*>", body):
            if 'name="profile_id"' not in tag or "checked" not in tag:
                continue
            value = re.search(r'value="(\d+)"', tag)
            if value:
                checked.add(int(value.group(1)))
        assert checked == {subscriptions["alpha_id"], subscriptions["beta_id"]}


class TestSelectionSurvivesEveryLink:
    """Every `url_for` on the page has to carry both ids, as two parameters."""

    def _assert_ids(self, href, expected_ids):
        params = _query_params(href)
        assert params.get("profile_id") == expected_ids, (
            f"{href!r} carries profile_id={params.get('profile_id')!r}, "
            f"expected {expected_ids!r}"
        )

    def test_column_sort_links(self, client, pair):
        body = client.get(f"/properties?{pair['query']}&view_type=list").get_data(
            as_text=True
        )
        hrefs = _anchor_hrefs(body, "lands-th-link")
        assert len(hrefs) >= 5, f"expected the sortable column headers, got {hrefs}"
        for href in hrefs:
            self._assert_ids(href, pair["ids"])

    def test_both_pagination_directions(self, client, pair):
        first = client.get(
            f"/properties?{pair['paged_query']}&view_type=list"
        ).get_data(as_text=True)
        next_href = _pagination_hrefs(first).get("Next")
        assert next_href, "the union spans two pages, so Next must render"
        self._assert_ids(next_href, pair["ids"])

        second = client.get(
            f"/properties?{pair['paged_query']}&view_type=list&page=2"
        ).get_data(as_text=True)
        prev_href = _pagination_hrefs(second).get("Prev")
        assert prev_href, "page two must offer Prev"
        self._assert_ids(prev_href, pair["ids"])

    def test_csv_export_link(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        self._assert_ids(
            _anchor_href(body, "Download filtered data as CSV"), pair["ids"]
        )

    def test_map_button(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        self._assert_ids(_anchor_href(body, "View properties on map"), pair["ids"])

    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_per_listing_map_links(self, client, pair, view_type):
        body = client.get(
            f"/properties?{pair['query']}&view_type={view_type}"
        ).get_data(as_text=True)
        hrefs = _anchor_hrefs(body, "focus=")
        assert hrefs, f"no per-listing map link in the {view_type} view"
        for href in hrefs:
            self._assert_ids(href, pair["ids"])

    def test_cards_list_toggle(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        for element_id in ("view-list-btn", "view-cards-btn"):
            self._assert_ids(_anchor_href(body, f'id="{element_id}"'), pair["ids"])

    def test_scoring_mode_buttons(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        for element_id in (
            "mode-combined-btn",
            "mode-investment-btn",
            "mode-lifestyle-btn",
        ):
            self._assert_ids(_anchor_href(body, f'id="{element_id}"'), pair["ids"])

    def test_map_page_link_back_to_the_list(self, client, pair):
        body = client.get(f"/map?{pair['query']}").get_data(as_text=True)
        self._assert_ids(_anchor_href(body, 'id="map-list-view-link"'), pair["ids"])


class TestTheUnionIsStableAcrossTransitions:
    def _page_ids(self, client, query):
        """Every listing id the page shows, in order, across all its pages."""
        order = []
        page = 1
        while True:
            body = client.get(f"/properties?{query}&page={page}").get_data(as_text=True)
            ids = _listing_ids_in_order(body)
            order.extend(ids)
            if "Next" not in _pagination_hrefs(body):
                break
            page += 1
            assert page < 10, "pagination did not terminate"
        return order

    def test_sorting_reorders_without_changing_the_set(self, client, pair):
        by_date = self._page_ids(
            client, f"{pair['paged_query']}&sort=created_at&order=desc"
        )
        by_price = self._page_ids(client, f"{pair['paged_query']}&sort=price&order=asc")
        assert set(by_date) == pair["union"]
        assert set(by_price) == pair["union"]
        assert by_date != by_price, "the fixture must make the two orders differ"

    def test_the_two_pagination_pages_partition_the_union(self, client, pair):
        first = _listing_ids_in_order(
            client.get(f"/properties?{pair['paged_query']}&page=1").get_data(
                as_text=True
            )
        )
        second = _listing_ids_in_order(
            client.get(f"/properties?{pair['paged_query']}&page=2").get_data(
                as_text=True
            )
        )
        assert len(first) == PER_PAGE
        assert second, "page two must not be empty"
        assert not set(first) & set(second), "a row appeared on both pages"
        assert set(first) | set(second) == pair["union"]

    def test_csv_export_matches_the_page_order_exactly(self, client, pair):
        """The CSV link is taken from the page, so this exercises the real
        href rather than a hand-built URL. The page is paginated and the
        export is not, so this also pins that the two agree end to end."""
        body = client.get(f"/properties?{pair['paged_query']}").get_data(as_text=True)
        csv_href = html.unescape(_anchor_href(body, "Download filtered data as CSV"))
        resp = client.get(csv_href)
        assert resp.status_code == 200
        csv_ids = _csv_ids_in_order(resp.get_data(as_text=True))
        assert set(csv_ids) == pair["union"]
        assert csv_ids == self._page_ids(client, pair["paged_query"])

    def test_csv_export_keeps_the_union_under_a_non_default_sort(self, client, pair):
        query = f"{pair['paged_query']}&sort=price&order=asc"
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        csv_href = html.unescape(_anchor_href(body, "Download filtered data as CSV"))
        csv_ids = _csv_ids_in_order(client.get(csv_href).get_data(as_text=True))
        assert csv_ids == self._page_ids(client, query)

    def test_map_shows_the_same_union(self, client, pair):
        body = client.get(f"/map?{pair['query']}").get_data(as_text=True)
        assert set(_marker_ids(body)) == pair["union"]

    def test_map_link_from_the_page_lands_on_the_same_union(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        map_href = html.unescape(_anchor_href(body, "View properties on map"))
        markers = _marker_ids(client.get(map_href).get_data(as_text=True))
        assert set(markers) == pair["union"]

    def test_round_trip_through_map_and_back_keeps_the_ids(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        map_href = html.unescape(_anchor_href(body, "View properties on map"))
        map_body = client.get(map_href).get_data(as_text=True)
        back_href = _anchor_href(map_body, 'id="map-list-view-link"')
        assert _query_params(back_href).get("profile_id") == pair["ids"]
        back_body = client.get(html.unescape(back_href)).get_data(as_text=True)
        assert set(_listing_ids_in_order(back_body)) == pair["union"]
        assert (
            _query_params(_anchor_href(back_body, "View properties on map")).get(
                "profile_id"
            )
            == pair["ids"]
        )


class TestProfileSpecificTravelNeedsOneProfile:
    def test_one_profile_shows_its_targets_and_the_recalculate_button(
        self, client, subscriptions
    ):
        alpha = subscriptions["alpha_id"]
        body = client.get(f"/properties?profile_id={alpha}").get_data(as_text=True)
        assert "AlphaOfficeTarget" in body
        assert "Recalculate travel" in body
        assert TRAVEL_NOTICE not in body

    def test_two_profiles_hide_the_targets_and_explain_why(self, client, pair):
        body = client.get(f"/properties?{pair['query']}").get_data(as_text=True)
        assert "AlphaOfficeTarget" not in body
        assert "BetaOfficeTarget" not in body
        assert "Recalculate travel" not in body
        assert TRAVEL_NOTICE in body

    def test_all_profiles_hides_the_targets_too(self, client, subscriptions):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert "AlphaOfficeTarget" not in body
        assert "Recalculate travel" not in body
        assert TRAVEL_NOTICE in body

    def test_the_map_follows_the_same_rule(self, client, subscriptions, pair):
        alpha = subscriptions["alpha_id"]
        single = client.get(f"/map?profile_id={alpha}").get_data(as_text=True)
        assert "AlphaOfficeTarget" in single
        assert TRAVEL_NOTICE not in single

        multi = client.get(f"/map?{pair['query']}").get_data(as_text=True)
        assert "AlphaOfficeTarget" not in multi
        assert "BetaOfficeTarget" not in multi
        assert TRAVEL_NOTICE in multi

    def test_the_csv_drops_the_profile_travel_columns_for_a_union(
        self, client, subscriptions, pair
    ):
        """Paired with the single-profile case below on purpose: a negative
        assertion alone also passes against an export that never emits travel
        columns at all."""
        alpha = subscriptions["alpha_id"]
        single = client.get(f"/properties/export.csv?profile_id={alpha}")
        assert single.status_code == 200
        single_header = next(
            csv_module.reader(io.StringIO(single.get_data(as_text=True)))
        )
        assert [c for c in single_header if c.startswith("travel_")], (
            "the single-profile export must carry the profile's travel columns"
        )

        resp = client.get(f"/properties/export.csv?{pair['query']}")
        assert resp.status_code == 200
        header = next(csv_module.reader(io.StringIO(resp.get_data(as_text=True))))
        assert not [column for column in header if column.startswith("travel_")]


class TestTheTravelNoticeDoesNotDependOnTheActiveList:
    """The explanation has to appear whenever the data is withheld.

    Selecting inactive profiles by id is supported on purpose, and when every
    profile happens to be inactive the active list is empty. Gating the notice
    on that list meant the page hid the travel data *and* the reason for it,
    which is the failure mode this whole issue is about, one level up.
    """

    @pytest.fixture
    def only_inactive_profiles(self, app):
        """Two inactive profiles, one of them the default.

        The default flag matters: `get_default_profile(create=True)` would
        otherwise mint a fresh *active* profile and the active list would not
        be empty at all.
        """
        with app.app_context():
            first = SearchProfile(
                name="Retired north",
                is_active=False,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            second = SearchProfile(
                name="Retired south",
                is_active=False,
                is_default=False,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add_all([first, second])
            db.session.commit()
            for index, profile in enumerate((first, second)):
                db.session.add(
                    Property(
                        source_email_id=f"issue104_inactive_{index}",
                        title=f"RetiredListing{index}UniqueTitle",
                        search_profile_id=profile.id,
                        listing_status="active",
                        location_lat=38.1 + index,
                        location_lon=-0.6 - index,
                    )
                )
            db.session.commit()
            return {"first_id": first.id, "second_id": second.id}

    def test_the_active_list_really_is_empty(self, client, only_inactive_profiles):
        """Fixture strength: if a default profile got auto-created the page
        would have an active profile and the defect could not reproduce."""
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert "No active subscriptions yet." in body

    def test_the_page_explains_itself(self, client, only_inactive_profiles):
        query = (
            f"profile_id={only_inactive_profiles['first_id']}"
            f"&profile_id={only_inactive_profiles['second_id']}"
        )
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        assert "RetiredListing0UniqueTitle" in body
        assert "RetiredListing1UniqueTitle" in body
        assert "Recalculate travel" not in body
        assert TRAVEL_NOTICE in body

    def test_the_map_explains_itself(self, client, only_inactive_profiles):
        query = (
            f"profile_id={only_inactive_profiles['first_id']}"
            f"&profile_id={only_inactive_profiles['second_id']}"
        )
        body = client.get(f"/map?{query}").get_data(as_text=True)
        assert len(_marker_ids(body)) == 2
        assert TRAVEL_NOTICE in body

    def test_a_single_inactive_profile_still_shows_its_travel_data(
        self, client, only_inactive_profiles
    ):
        """The counterpart: one profile is one profile, active or not."""
        body = client.get(
            f"/properties?profile_id={only_inactive_profiles['first_id']}"
        ).get_data(as_text=True)
        assert "Recalculate travel" in body
        assert TRAVEL_NOTICE not in body

    def test_an_install_with_nothing_to_select_stays_quiet(self, client, app):
        """The guard being replaced did have a job: a page that resolved no
        profile at all must not advise picking one."""
        body = client.get("/properties").get_data(as_text=True)
        assert TRAVEL_NOTICE not in body


class TestOldLinksKeepWorking:
    def test_a_single_profile_bookmark(self, client, subscriptions):
        alpha = subscriptions["alpha_id"]
        body = client.get(f"/properties?profile_id={alpha}&per_page=100").get_data(
            as_text=True
        )
        assert set(_listing_ids_in_order(body)) == set(subscriptions["alpha_props"])

    def test_the_all_sentinel(self, client, subscriptions):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        assert set(_listing_ids_in_order(body)) == (
            set(subscriptions["alpha_props"])
            | set(subscriptions["beta_props"])
            | set(subscriptions["gamma_props"])
        )

    def test_an_empty_profile_id_still_means_all(self, client, subscriptions):
        body = client.get("/properties?profile_id=&per_page=100").get_data(as_text=True)
        shown = set(_listing_ids_in_order(body))
        assert shown == (
            set(subscriptions["alpha_props"])
            | set(subscriptions["beta_props"])
            | set(subscriptions["gamma_props"])
        )

    def test_a_bare_properties_url_shows_every_live_subscription(
        self, client, subscriptions
    ):
        """Amended 2026-08-09: no `profile_id` means all of them.

        It used to resolve to the single richest profile, so the one surface
        opened on one saved search and hid the other -- the owner had to know
        the dropdown existed to see their own listings.
        """
        body = client.get("/properties?per_page=100").get_data(as_text=True)
        shown = set(_listing_ids_in_order(body))
        assert shown == (
            set(subscriptions["alpha_props"])
            | set(subscriptions["beta_props"])
            | set(subscriptions["gamma_props"])
        )
        assert not shown & set(subscriptions["zeta_props"]), (
            "an archived subscription is not part of 'all'"
        )

    def test_a_malformed_profile_id_falls_back_to_auto(self, client, subscriptions):
        body = client.get("/properties?profile_id=not-a-number&per_page=100").get_data(
            as_text=True
        )
        assert set(_listing_ids_in_order(body)) == (
            set(subscriptions["alpha_props"])
            | set(subscriptions["beta_props"])
            | set(subscriptions["gamma_props"])
        )

    def test_a_malformed_value_next_to_a_real_one_keeps_the_real_one(
        self, client, subscriptions
    ):
        beta = subscriptions["beta_id"]
        body = client.get(
            f"/properties?profile_id=not-a-number&profile_id={beta}&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(subscriptions["beta_props"])

    def test_ticked_boxes_win_over_the_forms_all_fallback(self, client, subscriptions):
        """The form always posts `profile_id=all` plus whatever is ticked, so
        the ticked ids have to be the answer -- otherwise the dropdown could
        never narrow anything without JavaScript."""
        alpha = subscriptions["alpha_id"]
        body = client.get(
            f"/properties?profile_id=all&profile_id={alpha}&per_page=100"
        ).get_data(as_text=True)
        assert set(_listing_ids_in_order(body)) == set(subscriptions["alpha_props"])


class TestProfileSelectionModule:
    """Unit coverage for services/profile_selection.py itself."""

    def _parse(self, query):
        return parse_profile_selection(_args(query))

    def test_absent_parameter_is_auto(self):
        assert self._parse("").state is ProfileSelectionState.AUTO

    @pytest.mark.parametrize(
        "query",
        ["profile_id=", "profile_id=all", "profile_id=ALL", "profile_id=%20all"],
    )
    def test_all_sentinel_and_empty_value_mean_all(self, query):
        selection = self._parse(query)
        assert selection.state is ProfileSelectionState.ALL
        assert selection.ids == ()

    def test_a_single_id_is_a_selection(self):
        selection = self._parse("profile_id=7")
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == (7,)

    def test_repeated_ids_keep_their_order(self):
        assert self._parse("profile_id=8&profile_id=6").ids == (8, 6)

    def test_repeated_ids_are_deduplicated(self):
        assert self._parse("profile_id=6&profile_id=8&profile_id=6").ids == (6, 8)

    def test_unparseable_value_alone_is_auto(self):
        assert self._parse("profile_id=abc").state is ProfileSelectionState.AUTO

    def test_unparseable_value_is_dropped_next_to_a_real_id(self):
        assert self._parse("profile_id=abc&profile_id=6").ids == (6,)

    def test_explicit_ids_win_over_the_all_token(self):
        selection = self._parse("profile_id=all&profile_id=6")
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == (6,)

    @pytest.mark.parametrize("raw", ["0", "-1", "99999999999999999999"])
    def test_impossible_numeric_ids_select_nothing_instead_of_falling_back(self, raw):
        """`0`, a negative id and an out-of-range one cannot exist. Dropping
        them back to `auto` would silently show another profile's listings,
        and passing them to the query would break a 32-bit integer column."""
        selection = self._parse(f"profile_id={raw}")
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == ()

    def test_a_number_too_long_for_int_is_still_a_number(self):
        """Python's `int()` refuses decimal strings past 4300 digits. Letting
        that raise into the unparseable-text branch would fall back to auto --
        the one thing a numeric input must never do."""
        selection = self._parse("profile_id=" + "9" * 5000)
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == ()

    def test_the_id_list_is_capped_and_says_so(self):
        from services.profile_selection import MAX_SELECTED_PROFILE_IDS

        query = "&".join(
            f"profile_id={n}" for n in range(1, MAX_SELECTED_PROFILE_IDS + 25)
        )
        selection = self._parse(query)
        assert len(selection.ids) == MAX_SELECTED_PROFILE_IDS
        assert selection.truncated is True
        assert self._parse("profile_id=6&profile_id=8").truncated is False

    def test_the_unassigned_token_is_its_own_choice(self):
        selection = self._parse("profile_id=unassigned")
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == ()
        assert selection.include_unassigned is True

    def test_unassigned_combines_with_ids_and_beats_the_all_token(self):
        selection = self._parse("profile_id=all&profile_id=6&profile_id=UNASSIGNED")
        assert selection.state is ProfileSelectionState.SELECTED
        assert selection.ids == (6,)
        assert selection.include_unassigned is True

    def test_all_never_implies_unassigned(self):
        selection = self._parse("profile_id=all")
        assert selection.include_unassigned is False
        assert _resolve("profile_id=all", [3, 5]).include_unassigned is False

    def test_resolve_keeps_unassigned_out_of_single_profile_context(self):
        """One profile plus the unassigned rows is still not one profile, so
        profile-specific travel has to stay hidden."""
        resolved = _resolve("profile_id=3&profile_id=unassigned", [3, 5])
        assert resolved.filter_ids == (3,)
        assert resolved.include_unassigned is True
        assert resolved.single_id is None
        assert resolved.withholds_profile_travel is True
        assert resolved.link_values == (3, "unassigned")

    @pytest.mark.parametrize(
        "query,active,auto,expected",
        [
            # Several profiles, none of them active: the explanation must not
            # depend on the active list being non-empty.
            ("profile_id=98&profile_id=99", [], None, True),
            ("profile_id=98&profile_id=99", [3, 5], None, True),
            ("profile_id=all", [3, 5], None, True),
            ("profile_id=unassigned", [], None, True),
            # One profile, active or not: its travel data is shown.
            ("profile_id=99", [], None, False),
            ("profile_id=3", [3, 5], None, False),
            ("profile_id=all", [3], None, False),
            ("", [3, 5], 3, False),
            # Nothing on screen, or nothing to select at all.
            ("profile_id=0", [3, 5], None, False),
            ("profile_id=all", [], None, False),
            ("", [], None, False),
        ],
    )
    def test_withholds_profile_travel(self, query, active, auto, expected):
        assert _resolve(query, active, auto=auto).withholds_profile_travel is expected

    def test_unassigned_alone_is_not_an_empty_selection(self):
        resolved = _resolve("profile_id=unassigned", [3, 5])
        assert resolved.matches_nothing is False
        assert resolved.form_fallback_value == "all"

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("profile_id=all", "all"),
            ("profile_id=3", "all"),
            ("", "all"),
            # Nothing ticked here means "the empty selection you asked for",
            # not "everything".
            ("profile_id=0", "0"),
        ],
    )
    def test_form_fallback_value(self, query, expected):
        assert _resolve(query, [3, 5], auto=3).form_fallback_value == expected

    def test_resolve_all_uses_active_profiles_only(self):
        resolved = _resolve("profile_id=all", [3, 5])
        assert resolved.filter_ids == (3, 5)
        assert resolved.link_values == ("all",)
        assert resolved.single_id is None

    def test_resolve_all_with_one_active_profile_is_single(self):
        assert _resolve("profile_id=all", [3]).single_id == 3

    def test_resolve_keeps_an_inactive_id(self):
        resolved = _resolve("profile_id=99", [3, 5])
        assert resolved.filter_ids == (99,)
        assert resolved.single_id == 99

    def test_resolve_auto_pins_the_resolved_profile(self):
        resolved = _resolve("", [3, 5], auto=5)
        assert resolved.filter_ids == (5,)
        assert resolved.link_values == (5,)
        assert resolved.single_id == 5

    def test_resolve_auto_without_any_profile_does_not_filter(self):
        resolved = _resolve("", [])
        assert resolved.filter_ids is None
        assert resolved.link_values == ()
        assert resolved.single_id is None

    @pytest.mark.parametrize(
        "query,active,auto",
        [
            ("profile_id=all", [3, 5], None),
            ("profile_id=3&profile_id=5", [3, 5], None),
            ("profile_id=99", [3, 5], None),
            ("", [3, 5], 5),
            ("profile_id=0", [3, 5], None),
            ("profile_id=unassigned", [3, 5], None),
            ("profile_id=3&profile_id=unassigned", [3, 5], None),
        ],
    )
    def test_link_values_round_trip_back_to_the_same_state(self, query, active, auto):
        """Every link the page renders is re-parsed on the next request, so a
        state that does not survive its own serialisation is a silent bug."""
        resolved = _resolve(query, active, auto=auto)
        round_tripped = parse_profile_selection(
            MultiDict([("profile_id", str(value)) for value in resolved.link_values])
        )
        reresolved = resolve_profile_selection(
            round_tripped, active, auto_profile_id=auto
        )
        assert reresolved.filter_ids == resolved.filter_ids
        assert reresolved.link_values == resolved.link_values
        assert reresolved.include_unassigned == resolved.include_unassigned

    @pytest.mark.parametrize(
        "query,active,auto,expected",
        [
            ("profile_id=all", [3, 5], None, "All subscriptions"),
            ("profile_id=3", [3, 5], None, "1 selected"),
            ("profile_id=3&profile_id=5", [3, 5], None, "2 selected"),
            ("", [3, 5], 5, "1 selected"),
            ("", [], None, "All subscriptions"),
            # An explicit selection that matched nothing shows an empty page,
            # so the toggle must not read "All subscriptions".
            ("profile_id=0", [3, 5], None, "0 selected"),
        ],
    )
    def test_button_label(self, query, active, auto, expected):
        assert _resolve(query, active, auto=auto).label == expected


class TestLegacyWrapperStillAgrees:
    """`SearchProfileService.parse_profile_selection` stays as the
    compatibility surface until the integrator collapses it (issue #104
    scope note). Until then the two parsers must not drift on the single-value
    inputs the old one supports."""

    @pytest.mark.parametrize(
        "raw,legacy_state,legacy_id",
        [
            (None, "auto", None),
            ("", "all", None),
            ("all", "all", None),
            ("7", "specific", 7),
            ("abc", "auto", None),
        ],
    )
    def test_single_value_inputs_agree(self, raw, legacy_state, legacy_id):
        from services.search_profile_service import SearchProfileService

        args = MultiDict() if raw is None else MultiDict([("profile_id", raw)])
        state, profile_id = SearchProfileService.parse_profile_selection(args)
        assert (state, profile_id) == (legacy_state, legacy_id)

        selection = parse_profile_selection(args)
        mapping = {
            "auto": ProfileSelectionState.AUTO,
            "all": ProfileSelectionState.ALL,
            "specific": ProfileSelectionState.SELECTED,
        }
        assert selection.state is mapping[legacy_state]
        if legacy_state == "specific":
            assert selection.ids == (legacy_id,)
