"""A listing the owner turned down is not offered back to them (2026-09-07).

The owner reported property 1405: `owner_verdict = 'rejected'`, sitting in the
ranked suggestions with a taste score of 55. The similarity reading had set a
rejected row aside since 2026-09-03, but that was the only surface that knew:
`services/taste_service.py` reads `owner_verdict` to collect the signals it
TRAINS on and never to decide what to show, so every other ranking went on
offering the rows the owner had already refused.

The rule is `subscription_criteria`'s, one column over: a bare listing page
withholds them, the count of what was withheld renders beside the result count,
and the Verdict filter is the way back. What this file pins:

* the two languages agree — `hidden_by_rejection` (Python, for a row's own
  page) and `rejected_hidden_expression` (SQL, for every query) run the same
  matrix, because a count that disagrees with the badge beside it is a third
  wrong number rather than a disclosure;
* the two exemptions, each with a precedent: a FAVORITED row is never hidden
  (the star is the owner's own act, and `favorite_similarity` already answers
  `reference` for one before it looks at the verdict), and neither is a row
  carrying an OUTSTANDING ACTION (`open_action_expression`'s own docstring:
  a reminder the page hides is the defect that predicate was written for);
* every surface hides it — the list, BOTH map branches, the CSV and the JSON
  API — because a filter one surface keeps and another drops is the regression
  this repository has already paid for (#445), and there is no single insertion
  point: three refuters killed that claim, and the four chains are hand-written;
* the way back really works: `verdict=rejected` and `verdict=all` lift the hide,
  the dropdown keeps its `rejected (N)` option even though the page holds none
  of them, and the reveal link states the lifted value
  (`CLEARED_NOT_ABSENT`) rather than merely dropping the parameter;
* an unrecognised spelling NARROWS. Once absence hides, `?verdict=banana` must
  not read as "show everything".
"""

import re
from html import unescape

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import owner_review
from tests import setup_test_environment


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
def world(app):
    """One subscription, five rows, one of each state that matters."""
    profile = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(profile)
    db.session.commit()

    def mk(slug, **kwargs):
        row = Property(
            source_email_id=f"rej:{slug}",
            title=f"Casa {slug}",
            municipality="Malpica de Bergantiños",
            search_profile_id=profile.id,
            price=200000,
            area=200,
            # Coordinates, because the map can only withhold a row it could
            # otherwise have plotted -- its count is about markers, not rows.
            location_lat=43.32,
            location_lon=-8.82,
            location_accuracy="precise",
            **kwargs,
        )
        db.session.add(row)
        db.session.commit()
        return row

    rows = {
        "rejected": mk("rejected", owner_verdict="rejected"),
        "rejected_favorite": mk(
            "rejected_favorite", owner_verdict="rejected", is_favorite=True
        ),
        "rejected_actioned": mk(
            "rejected_actioned",
            owner_verdict="rejected",
            next_action="Call the agency",
        ),
        "interested": mk("interested", owner_verdict="interested"),
        "undecided": mk("undecided"),
    }
    return {"pid": profile.id, "rows": rows, "ids": {k: v.id for k, v in rows.items()}}


def _shown(body):
    return {int(m) for m in re.findall(r"/properties/(\d+)", body)}


def _count(body):
    m = re.search(r"<strong>(\d+) properties found</strong>", body)
    return int(m.group(1)) if m else None


class TestTheTwoLanguagesAgree:
    """One matrix through both readings. A count that disagrees with the row
    beside it is a third wrong number, not a disclosure."""

    @pytest.mark.parametrize(
        "key, hidden, why",
        [
            ("rejected", True, "the plain case"),
            ("rejected_favorite", False, "the star is the owner's own act"),
            ("rejected_actioned", False, "a reminder the page hides is the defect"),
            ("interested", False, "not a refusal"),
            ("undecided", False, "most of the table"),
        ],
    )
    def test_python_and_sql_say_the_same(self, app, world, key, hidden, why):
        row = world["rows"][key]

        assert owner_review.hidden_by_rejection(row) is hidden, why

        in_sql = (
            Property.query.filter(
                Property.id == row.id,
                owner_review.rejected_hidden_expression(Property),
            ).first()
            is not None
        )
        assert in_sql is hidden, f"SQL disagrees with Python: {why}"


class TestTheReading:
    @pytest.mark.parametrize(
        "raw, value, recognised",
        [
            ("", "", True),
            (None, "", True),
            ("all", "all", True),
            ("rejected", "rejected", True),
            ("interested", "interested", True),
            ("  ALL  ", "all", True),
            ("banana", "", False),
        ],
    )
    def test_the_vocabulary(self, app, raw, value, recognised):
        assert owner_review.read_decision_filter(raw) == (value, recognised)

    def test_an_unrecognised_spelling_narrows(self, app, world):
        """Once absence hides, a typo must not widen the page."""
        query, hidden = owner_review.apply_rejected_hide(
            Property.query, Property, "banana", count_hidden=True
        )

        assert hidden == 1
        assert world["ids"]["rejected"] not in {row.id for row in query.all()}

    def test_lifting_reports_no_count_rather_than_zero(self, app, world):
        """`None`, never 0: a caller must not render "0 hidden" where the
        answer is "nothing was hidden from you"."""
        for raw in ("all", "rejected"):
            _query, hidden = owner_review.apply_rejected_hide(
                Property.query, Property, raw, count_hidden=True
            )
            assert hidden is None, raw


class TestEverySurface:
    """#445: a filter one surface keeps and another drops is the regression.
    There is no single insertion point — three refuters killed that claim —
    so each of the four hand-written chains is asserted here."""

    def test_the_list_hides_it_and_says_so(self, client, world):
        body = client.get(f"/properties?profile_id={world['pid']}").get_data(
            as_text=True
        )

        shown = _shown(body)
        assert world["ids"]["rejected"] not in shown
        assert world["ids"]["rejected_favorite"] in shown
        assert world["ids"]["rejected_actioned"] in shown
        assert _count(body) == 4
        assert 'id="rejected-hidden-coverage"' in body
        assert "Rejected: 1 hidden" in body

    def test_the_map_hides_it(self, client, world):
        body = client.get(f"/map?profile_id={world['pid']}").get_data(as_text=True)

        assert "map-rejected-hidden-note" in body or "Rejected: 1 hidden" in body
        assert f'"id": {world["ids"]["rejected"]}' not in body

    def test_the_csv_hides_it(self, client, world):
        body = client.get(f"/properties/export.csv?profile_id={world['pid']}").get_data(
            as_text=True
        )

        assert "Casa rejected_favorite" in body
        assert "Casa rejected," not in body and "Casa rejected\r" not in body

    def test_the_api_hides_it_and_discloses(self, client, world):
        payload = client.get(f"/api/properties?profile_id={world['pid']}").get_json()

        ids = {row["id"] for row in payload["properties"]}
        assert world["ids"]["rejected"] not in ids
        assert payload["scope"]["rejected_hidden_by_default"] == 1
        assert payload["scope"]["verdict_applied"] == "default"
        # `notes` lives inside `scope`, beside the four facts it explains.
        assert any("verdict:" in note for note in payload["scope"].get("notes") or [])

    def test_the_rows_own_page_still_shows_it(self, client, world):
        response = client.get(f"/properties/{world['ids']['rejected']}")

        assert response.status_code == 200
        assert "Casa rejected" in response.get_data(as_text=True)


class TestTheWayBack:
    @pytest.mark.parametrize("value", ["all", "rejected"])
    def test_naming_the_verdict_lifts_the_hide(self, client, world, value):
        body = client.get(
            f"/properties?profile_id={world['pid']}&verdict={value}"
        ).get_data(as_text=True)

        assert world["ids"]["rejected"] in _shown(body)
        assert "Rejected: 1 hidden" not in body

    def test_the_dropdown_keeps_the_option_that_is_the_way_back(self, client, world):
        """The crux. Counted with the hide in force, `rejected` reads 0,
        `owner_review.decision_options` drops a zero-count state, and the one
        control this design offers as the way back disappears from the page it
        is the way back from."""
        body = client.get(f"/properties?profile_id={world['pid']}").get_data(
            as_text=True
        )

        select = body[body.index('id="verdict"') :]
        select = select[: select.index("</select>")]
        assert 'value="rejected"' in select
        # THREE, not one: the option counts every rejected row, because its own
        # dimension is lifted -- otherwise it would read 0 and vanish. The page
        # itself hid only ONE of them (the other two are exempt), so these are
        # two different numbers and both are right.
        assert re.search(r"Rejected[^<]*\(3\)", select), select[:400]
        assert 'value="all"' in select

    def test_the_map_reveal_link_states_the_lifted_value(self, client, world):
        """Dropping the parameter would re-issue the hide — the `/map?focus`
        loop `CLEARED_NOT_ABSENT` exists for, one filter over."""
        body = client.get(f"/map?profile_id={world['pid']}").get_data(as_text=True)

        m = re.search(r'href="([^"]*verdict=all[^"]*)"', body)
        assert m, "the map offers no way to see what it withheld"


class TestTheCountsStayHonest:
    def test_the_chip_opens_what_it_promises(self, client, world):
        """#530's invariant, under the new hide: the number on the chip IS the
        page its own href opens. The href is HTML-escaped in the markup, so it
        is unescaped before being followed -- following it as written asks for
        a literal `&amp;hide_removed` and lands somewhere else entirely."""
        body = client.get("/properties?profile_id=all").get_data(as_text=True)

        anchor = re.search(
            r'<a[^>]*href="([^"]*profile_id=' + str(world["pid"]) + r'[^"]*)"'
            r"[^>]*>(.*?)</a>",
            body,
            re.S,
        )
        assert anchor, "the subscription chip did not render"
        badge = re.search(r">\s*(\d+)\s*<", anchor.group(2))
        assert badge, f"the chip carries no count: {anchor.group(2)[:200]}"

        opened = _count(client.get(unescape(anchor.group(1))).get_data(as_text=True))
        assert opened == int(badge.group(1)) == 4

    def test_clearing_the_filters_really_reveals(self, client, world):
        """`CLEARED_NOT_ABSENT` must carry `verdict`, or a link that promises
        to show a hidden row re-issues the hide."""
        from utils.listing_filters import CLEARED_NOT_ABSENT

        assert CLEARED_NOT_ABSENT.get("verdict") == "all"
