"""The subscription's criteria: pass / fail / unknown, in both languages.

One matrix through the Python reader AND the SQL expressions (the
`advertiser.py` contract) — a count that disagrees with the verdicts under
it is a third wrong number. Then the surfaces: the default view hides only
measured fails and NEVER a favorited or reviewed row; the disclosure line
counts what was hidden; the map and the CSV read the same parameter (#445's
rule: a filter one surface keeps and another drops is the regression).
"""

import csv
import io

from datetime import date

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import subscription_criteria
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _mk(profile_id, **overrides):
    values = dict(
        source_email_id=f"crit:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=100000,
        search_profile_id=profile_id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


# (area, area_type, plot_area, expected_state) — the one matrix both
# languages run.
MATRIX = [
    (200, "built", 800, "pass"),
    (200, "built", 650, "fail"),  # plot measurably short
    (120, "built", 800, "fail"),  # house measurably short
    (200, "built", None, "unknown"),  # plot never measured
    (None, "built", 800, "unknown"),  # house never measured
    (200, None, 800, "pass"),  # NULL area_type reads as built
    (800, "plot", None, "unknown"),  # bare land: area IS the plot, house unknown
    (650, "plot", None, "fail"),  # bare land, plot short
    (650, "plot", 900, "pass_or_unknown_house"),  # plot_area wins over area
    # Zero plot_area on bare land is a BLANK, so `area` answers — both
    # languages (the implementation review's 650/plot/0 reproduction).
    (650, "plot", 0, "fail"),
    # Case and whitespace must not split the two languages: the Python
    # reader lowercases, and " PLOT " read as built in SQL alone turned a
    # measured fail into unknown (the gate review's reproduction).
    (650, "PLOT", None, "fail"),
    (650, " Plot ", None, "fail"),
    # A TAB survives SQL's trim() (btrim strips spaces only — the SEPE
    # lesson), so both languages read a tab-polluted type as not-plot and
    # answer unknown together; Python matching SQL, not the other way.
    (650, "PLOT\t", None, "unknown"),
    # AT the bound, both criteria. The matrix carried 120/200/650/800/900 and
    # never 150 or 700 — the two numbers CRITERIA is built from — so `>=`
    # could be mutated to `>` in the SQL twin and the whole suite stayed
    # green (#502 review). Reachable the day it merged: 13 production rows
    # carry `area = 150` and 3 bare-land rows carry `area = 700`.
    (150, "built", 800, "pass"),  # house exactly at the bound passes
    (200, "built", 700, "pass"),  # plot exactly at the bound passes
    (700, "plot", None, "pass_or_unknown_house"),  # bare land at the bound
    # One under the bound on each, so the pair pins the boundary from both
    # sides — an off-by-one that moved the comparison would break one of the
    # two whichever direction it moved.
    (149, "built", 800, "fail"),
    (200, "built", 699, "fail"),
    # A built row whose area clears the PLOT bound while its plot is
    # unmeasured. `passing_expression`'s bare-land branch is guarded by
    # `is_plot`; without that guard this reads the BUILT surface as the
    # parcel and calls it a pass. Deleting the guard left the full suite at
    # 4211 passed — 43 production rows are `area_type='built' AND area >=
    # 700`, and migration 025 gave every existing row a NULL plot_area.
    (800, "built", None, "unknown"),
    (0, "built", 800, "unknown"),  # zero is a blank, never a tiny house
    (200, "built", 0, "unknown"),  # zero plot is a blank too
]


class TestTheTwoReadingsAgree:
    @pytest.mark.parametrize("area, area_type, plot, expected", MATRIX)
    def test_python_and_sql_answer_alike(
        self, app, profile_row, area, area_type, plot, expected
    ):
        prop = _mk(profile_row.id, area=area, area_type=area_type, plot_area=plot)
        verdict = subscription_criteria.read_verdict(prop, CRITERIA)

        if expected == "pass_or_unknown_house":
            # A bare-land row with a stated plot passes the plot bound but
            # can never answer the house bound — unknown, both languages.
            expected = "unknown"
        assert verdict["state"] == expected, (
            f"python said {verdict['state']} for area={area}/{area_type}, plot={plot}"
        )

        fails = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.failing_expression(Property, CRITERIA)
            )
        }
        passes = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.passing_expression(Property, CRITERIA)
            )
        }
        assert (prop.id in fails) == (expected == "fail"), "SQL fail disagrees"
        assert (prop.id in passes) == (expected == "pass"), "SQL pass disagrees"
        # unknown is ~fail AND ~pass, and both expressions are definite per
        # row — the NULL third value would silently eat rows here.
        unknowns = {
            p.id
            for p in Property.query.filter(
                ~subscription_criteria.failing_expression(Property, CRITERIA),
                ~subscription_criteria.passing_expression(Property, CRITERIA),
            )
        }
        assert (prop.id in unknowns) == (expected == "unknown"), "SQL unknown disagrees"

    def test_no_criteria_and_malformed_criteria_read_as_none(self, app):
        clean = SearchProfile(name="No criteria", is_active=True)
        broken = SearchProfile(
            name="Broken", is_active=True, criteria={"min_house_m2": "big"}
        )
        negative = SearchProfile(
            name="Negative", is_active=True, criteria={"min_plot_m2": -5}
        )
        # A typo key must reject the WHOLE block — half-applying the half
        # that parsed hid listings on the strength of a misspelling; and a
        # bound float() cannot represent must not 500 the page (both from
        # the implementation review).
        typo = SearchProfile(
            name="Typo",
            is_active=True,
            criteria={"min_house_m2": 150, "min_plto_m2": 700},
        )
        overflow = SearchProfile(
            name="Overflow", is_active=True, criteria={"min_house_m2": 10**400}
        )
        infinite = SearchProfile(
            name="Infinite",
            is_active=True,
            criteria={"min_house_m2": float("inf")},
        )
        db.session.add_all([clean, broken, negative, typo, overflow, infinite])
        db.session.commit()
        assert subscription_criteria.read_criteria(clean) is None
        assert subscription_criteria.read_criteria(broken) is None
        assert subscription_criteria.read_criteria(negative) is None
        assert subscription_criteria.read_criteria(typo) is None
        assert subscription_criteria.read_criteria(overflow) is None
        assert subscription_criteria.read_criteria(infinite) is None

    def test_the_owner_judgement_is_never_hidden(self, app, profile_row):
        failing = _mk(profile_row.id, area=100, area_type="built")
        favorite = _mk(profile_row.id, area=100, area_type="built", is_favorite=True)
        reviewed = _mk(profile_row.id, area=100, area_type="built")
        reviewed.owner_verdict = "interested"
        db.session.commit()
        hidden = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.hidden_by_default_expression(Property, CRITERIA)
            )
        }
        assert failing.id in hidden
        assert favorite.id not in hidden
        assert reviewed.id not in hidden


class TestTheSurfaces:
    @pytest.fixture
    def rows(self, app, profile_row):
        passing = _mk(
            profile_row.id,
            title="Passing house",
            area=200,
            area_type="built",
            plot_area=900,
        )
        failing = _mk(profile_row.id, title="Failing tiny", area=100, area_type="built")
        unknown = _mk(profile_row.id, title="Unknown plot", area=200, area_type="built")
        return {"pass": passing, "fail": failing, "unknown": unknown}

    def test_the_default_view_hides_only_measured_fails(self, client, rows):
        html = client.get("/properties").data.decode()
        assert "Passing house" in html
        assert "Unknown plot" in html
        assert "Failing tiny" not in html
        assert "Criteria: 1 failing hidden" in html

    def test_criteria_all_shows_everything_and_no_disclosure(self, client, rows):
        html = client.get("/properties?criteria=all").data.decode()
        assert "Failing tiny" in html
        assert "failing hidden" not in html

    @pytest.mark.parametrize(
        "mode, visible, hidden",
        [
            ("pass", ["Passing house"], ["Failing tiny", "Unknown plot"]),
            ("fail", ["Failing tiny"], ["Passing house", "Unknown plot"]),
            ("unknown", ["Unknown plot"], ["Passing house", "Failing tiny"]),
        ],
    )
    def test_each_verdict_mode_selects_its_rows(
        self, client, rows, mode, visible, hidden
    ):
        html = client.get(f"/properties?criteria={mode}").data.decode()
        for title in visible:
            assert title in html, f"{mode} lost {title}"
        for title in hidden:
            assert title not in html, f"{mode} leaked {title}"

    def test_the_map_reads_the_same_parameter(self, client, rows):
        for prop in rows.values():
            prop.location_lat = 43.0
            prop.location_lon = -9.0
        db.session.commit()
        default_map = client.get("/map").data.decode()
        assert "Failing tiny" not in default_map
        wide_map = client.get("/map?criteria=all").data.decode()
        assert "Failing tiny" in wide_map

    def test_the_csv_reads_the_same_parameter(self, client, rows):
        body = client.get("/properties/export.csv").data.decode()
        titles = [row[2] for row in csv.reader(io.StringIO(body))]
        assert "Passing house" in " ".join(titles)
        assert "Failing tiny" not in " ".join(titles)
        wide = client.get("/properties/export.csv?criteria=all").data.decode()
        assert "Failing tiny" in wide

    def test_the_scope_total_describes_what_clearing_lands_on(self, client, rows):
        """The gate review's finding: under criteria=fail the clear link
        resets criteria, so its disclosed total must be the DEFAULT view's
        count (what clearing lands on), never the fail-filtered one."""
        import re

        # Narrow by search so the filter bar is genuinely active, in fail
        # mode: the page shows 1 (the failing row), clearing lands on the
        # default view of 2 (pass + unknown).
        html = client.get("/properties?criteria=fail&search=Failing").data.decode()
        note = re.search(r"filter-bar-narrowing-note.*?</span>", html, re.S)
        assert note is not None, "the narrowing note must render"
        text = note.group(0)
        assert "of 2" in text, (
            f"the scope total must be the default view's 2, got: {text!r}"
        )

    def test_the_narrowing_note_never_claims_more_than_its_own_scope(
        self, client, rows
    ):
        """ "Filters: 4 of 2 shown" — rendered literally (#502 review).

        The scope is counted under the DEFAULT criteria reading, because that
        is what clearing lands on; the page under the CURRENT one. Under
        `criteria=all` the page holds rows the scope hides, so `N of M` stops
        being a subset claim. `i18n` spells it "Filters: %s of %s shown", so
        4-of-2 is not a phrasing being read uncharitably.
        """
        import re

        def _pair(query):
            html = client.get(query).data.decode()
            note = re.search(r"filter-bar-narrowing-note.*?</span>", html, re.S)
            if note is None:
                return None
            # The phrase itself, not every digit in the block: the icon class
            # is `fas fa-filter me-1`, so a bare `\d+` scan reads the `1` out
            # of `me-1` first and compares 1 <= 3 — an assertion that passes
            # on the very inversion it is written for. Measured: the first
            # version of this test stayed green under the mutation.
            found = re.search(r"(\d+)\s+of\s+(\d+)", note.group(0))
            assert found is not None, f"unreadable note: {note.group(0)!r}"
            return int(found.group(1)), int(found.group(2))

        # Positive control first: in a genuine narrowing the note must render,
        # or the assertion below is satisfied by a line that never appears.
        narrowed = _pair("/properties?search=Passing")
        assert narrowed is not None, "the narrowing note must render at all"
        assert narrowed[0] <= narrowed[1]

        widened = _pair("/properties?criteria=all&search=n")
        if widened is not None:
            assert widened[0] <= widened[1], (
                f"the note claims to show {widened[0]} of {widened[1]} — a set "
                "bigger than the set it says it is part of"
            )

    def test_the_unassigned_link_lands_on_the_rows_it_advertises(
        self, client, app, profile_row
    ):
        """The link said "N listings with no subscription (show them)" and
        landed on a page that is always empty under criteria=pass/fail/unknown
        (#502 review): the count is taken before the criteria filter, and every
        criteria expression leads with `search_profile_id IS NOT NULL`.

        A row with no subscription has no subscription criteria, so the mode
        is dropped from that one link rather than carried over to state a
        filter that cannot match.
        """
        import re

        _mk(None, title="Orphan tiny", area=100, area_type="built")
        for mode in ("fail", "pass", "unknown"):
            html = client.get(f"/properties?criteria={mode}").data.decode()
            link = re.search(r'id="unassigned-count-link"\s+href="([^"]+)"', html)
            if link is None:
                continue
            href = link.group(1).replace("&amp;", "&")
            assert "criteria=" not in href, (
                f"the unassigned link carries the criteria mode ({mode}), and "
                f"the page it lands on can never match: {href}"
            )
            landed = client.get(href).data.decode()
            assert "Orphan tiny" in landed, (
                f"the link advertised the row and landed on a page without it: {href}"
            )

    def test_an_unassigned_listing_is_never_hidden_by_anybodys_criteria(
        self, client, app, profile_row
    ):
        """The review's NULL reproduction: `search_profile_id == pid` on an
        unassigned row is NULL, and `~NULL` used to drop it from its own
        page."""
        orphan = _mk(None, title="Orphan tiny", area=100, area_type="built")
        html = client.get("/properties?profile_id=unassigned").data.decode()
        assert "Orphan tiny" in html
        assert orphan.search_profile_id is None

    def test_unknown_mode_never_claims_a_criteria_less_subscriptions_row(
        self, client, app, rows
    ):
        """The gate review's leak: unknown is ~fail AND ~pass, and a row of
        a subscription with NO criteria answered both negations TRUE — a
        verdict it never had (its reading is no_criteria)."""
        bare = SearchProfile(name="Bare", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="No criteria here", area=100, area_type="built")
        html = client.get("/properties?criteria=unknown").data.decode()
        assert "Unknown plot" in html
        assert "No criteria here" not in html

    def test_without_criteria_the_control_is_absent_and_nothing_hides(
        self, client, app
    ):
        bare = SearchProfile(name="Plain", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="Tiny but shown", area=50, area_type="built")
        html = client.get("/properties").data.decode()
        assert "Tiny but shown" in html
        assert 'name="criteria"' not in html


class TestAnOwnerActionIsNeverHidden:
    """The HIGH finding of the #502 review, pinned.

    A listing carrying an outstanding action but no verdict was hidden by the
    criteria default, while the overdue count — built off the profile
    selection with no criteria clause — went on advertising it. The bare page
    read "1 overdue", its own link landed on "0 properties found", and that
    page re-rendered the same link. A loop with no way out.

    The exemption is the third of a set: favorited, reviewed, and now
    *acted on* — one idea, that the owner has touched this row, so the
    subscription's blanket criteria stop deciding whether they see it.
    """

    def test_a_row_with_an_open_action_survives_the_default_hide(self, app):
        prop = _mk(None, area=100, area_type="built")
        prop.next_action = "call the architect"
        prop.next_action_due_on = date(2020, 1, 1)
        db.session.commit()

        hidden = (
            db.session.query(Property.id)
            .filter(
                subscription_criteria.hidden_by_default_expression(Property, CRITERIA)
            )
            .all()
        )
        assert prop.id not in [row[0] for row in hidden], (
            "a listing the owner left an action on was hidden by the criteria "
            "default, while the overdue count still advertised it"
        )

    def test_the_same_row_without_an_action_is_still_hidden(self, app):
        """The negative control: without it the test above passes on a hide
        that never fired, which is the shape of an assertion that cannot
        fail."""
        prop = _mk(None, area=100, area_type="built")
        db.session.commit()

        hidden = (
            db.session.query(Property.id)
            .filter(
                subscription_criteria.hidden_by_default_expression(Property, CRITERIA)
            )
            .all()
        )
        assert prop.id in [row[0] for row in hidden]

    def test_a_blank_action_is_not_an_action(self, app):
        """Whitespace in `next_action` must not buy an exemption: the column
        is free text and `""` is what an emptied form field stores."""
        for blank in ("", "   ", "\t"):
            prop = _mk(None, area=100, area_type="built")
            prop.next_action = blank
            db.session.commit()
            hidden = (
                db.session.query(Property.id)
                .filter(
                    subscription_criteria.hidden_by_default_expression(
                        Property, CRITERIA
                    )
                )
                .all()
            )
            assert prop.id in [row[0] for row in hidden], (
                f"a blank next_action ({blank!r}) bought an exemption"
            )
