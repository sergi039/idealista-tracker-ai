"""Both review filters survive every hop, together (#430).

Two separate tests, one per parameter, would pass over code that keeps one and
drops the other -- which is exactly what `templates/properties.html` did to the
`source` and `advertiser` filters before this feature was written: they are
applied by the query and absent from `base_args`, so paging or sorting widens
the page silently. So this file carries `verdict=rejected&action=overdue`
**together** through the list, the pagination link, the sort links, the map and
CSV links, `properties/export.csv` itself, and both API serializers.

The second half is the midnight case. `overdue` is a due date compared against
today, and `today` here is Madrid's. If the query, the badge, the count and the
two serializers each computed their own, they would disagree for the minutes
between Madrid's midnight and UTC's -- a defect that appears once a day, at the
hour nobody is looking, and reads as a caching bug.
"""

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

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
def profile(app):
    row = SearchProfile(name="Asturias", is_active=True, is_default=True)
    db.session.add(row)
    db.session.commit()
    return row


LONG_AGO = date(2026, 1, 1)


@pytest.fixture
def rows(profile):
    """One row in every corner of the two-way split."""
    made = {}
    for slug, verdict, action, due in [
        ("rejected-and-late", "rejected", "chase the agency", LONG_AGO),
        ("rejected-only", "rejected", None, None),
        ("late-only", None, "ask for the RC", LONG_AGO),
        ("interested-and-late", "interested", "call the architect", LONG_AGO),
        ("untouched", None, None, None),
    ]:
        row = Property(
            source_email_id=slug,
            title=f"Plot {slug}",
            municipality="Castrillón",
            search_profile_id=profile.id,
            owner_verdict=verdict,
            next_action=action,
            next_action_due_on=due,
        )
        db.session.add(row)
        made[slug] = row
    db.session.commit()
    return made


BOTH = "verdict=rejected&action=overdue"


def _rendered(response):
    """The body, having checked the page really rendered.

    `/properties` catches any exception, flashes and re-renders at the same
    200 with no rows -- so a status check alone would pass through a template
    that raised.
    """
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "An error occurred while loading properties" not in body
    return body


class TestBothFiltersTogether:
    def test_the_pair_selects_the_one_row_in_both_corners(self, client, rows):
        body = _rendered(client.get(f"/properties?{BOTH}"))
        assert "Plot rejected-and-late" in body
        # Each of these satisfies one half of the pair and not the other.
        assert "Plot rejected-only" not in body
        assert "Plot late-only" not in body
        assert "Plot interested-and-late" not in body

    def test_both_survive_the_pagination_links(self, client, rows):
        body = _rendered(client.get(f"/properties?{BOTH}&per_page=10"))
        # The links are built from one dict; if either key is missing from it,
        # the other is still there and this catches the asymmetry.
        assert "verdict=rejected" in body
        assert "action=overdue" in body

    def test_both_survive_a_sort_link(self, client, rows):
        body = _rendered(client.get(f"/properties?{BOTH}&sort=price&order=asc"))
        for link in _links(body, "/properties?"):
            if "sort=" in link:
                assert "verdict=rejected" in link, link
                assert "action=overdue" in link, link

    def test_both_survive_the_map_and_csv_links(self, client, rows):
        body = _rendered(client.get(f"/properties?{BOTH}"))
        for prefix in ("/map?", "/properties/export.csv?"):
            found = _links(body, prefix)
            assert found, f"no {prefix} link on the page"
            for link in found:
                assert "verdict=rejected" in link, link
                assert "action=overdue" in link, link

    def test_both_survive_the_view_and_mode_links(self, client, rows):
        body = _rendered(client.get(f"/properties?{BOTH}"))
        for link in _links(body, "/properties?"):
            if "view_type=" in link or "mode=" in link:
                assert "verdict=rejected" in link, link
                assert "action=overdue" in link, link

    def test_the_csv_export_applies_the_pair(self, client, rows):
        response = client.get(f"/properties/export.csv?{BOTH}")
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert "Plot rejected-and-late" in text
        assert "Plot interested-and-late" not in text
        header = text.splitlines()[0]
        assert "Owner Verdict" in header
        assert "Next Action State" in header

    def test_both_api_serializers_apply_the_pair_and_carry_the_fields(
        self, client, rows, profile
    ):
        for query in (
            f"/api/properties?profile_id={profile.id}&{BOTH}",
            f"/api/properties?profile_id={profile.id}&{BOTH}&full=true",
        ):
            payload = json.loads(client.get(query).get_data(as_text=True))
            titles = [p["title"] for p in payload["properties"]]
            assert titles == ["Plot rejected-and-late"], query
            row = payload["properties"][0]
            # The compact response is hand-built and is the DEFAULT one, so a
            # field added to `to_dict` alone would be missing exactly where
            # most consumers look.
            assert row["owner_verdict"] == "rejected", query
            assert row["next_action_state"] == "overdue", query


def _links(body, prefix):
    """Every href on the page starting with `prefix`, unescaped."""
    import re

    return [
        match.replace("&amp;", "&")
        for match in re.findall(r'href="([^"]+)"', body)
        if match.startswith(prefix)
    ]


class TestOneDateForTheWholeRequest:
    """The filter, the badge, the count and both serializers, on one day."""

    def test_a_row_due_today_is_not_late_at_madrid_midnight(self, client, profile):
        """23:59:30 in Madrid on the due date. In UTC it is already tomorrow."""
        due = date(2026, 6, 20)
        db.session.add(
            Property(
                source_email_id="due-today",
                title="Plot due today",
                search_profile_id=profile.id,
                owner_verdict="waiting",
                next_action="condiciones de edificabilidad",
                next_action_due_on=due,
            )
        )
        db.session.commit()

        # Madrid is UTC+2 in June: 23:59 local is 21:59 UTC, and a naive
        # `datetime.utcnow().date()` would still say the 20th. The case that
        # bites is the other one -- so freeze Madrid at 00:30 on the 21st and
        # assert every surface agrees it is now late, rather than one of them
        # reading a UTC date of the 20th.
        madrid_now = datetime(2026, 6, 21, 0, 30)

        with patch.object(owner_review, "today", return_value=madrid_now.date()):
            body = _rendered(client.get("/properties?action=overdue"))
            assert "Plot due today" in body

            payload = json.loads(
                client.get(
                    f"/api/properties?profile_id={profile.id}&action=overdue"
                ).get_data(as_text=True)
            )
            assert [p["title"] for p in payload["properties"]] == ["Plot due today"]
            assert payload["properties"][0]["next_action_state"] == "overdue"

            payload = json.loads(
                client.get(
                    f"/api/properties?profile_id={profile.id}&action=overdue&full=true"
                ).get_data(as_text=True)
            )
            assert payload["properties"][0]["next_action_state"] == "overdue"

            csv_text = client.get("/properties/export.csv?action=overdue").get_data(
                as_text=True
            )
            assert "Plot due today" in csv_text

    def test_the_same_day_reads_as_pending_everywhere(self, client, profile):
        due = date(2026, 6, 20)
        db.session.add(
            Property(
                source_email_id="due-today",
                title="Plot due today",
                search_profile_id=profile.id,
                owner_verdict="waiting",
                next_action="condiciones",
                next_action_due_on=due,
            )
        )
        db.session.commit()

        with patch.object(owner_review, "today", return_value=due):
            body = _rendered(client.get("/properties?action=overdue"))
            assert "Plot due today" not in body
            body = _rendered(client.get("/properties?action=pending"))
            assert "Plot due today" in body

    def test_the_view_asks_for_the_date_once(self, client, rows):
        """Not once per row, and not once per surface within the request."""
        with patch.object(owner_review, "today", wraps=owner_review.today) as spy:
            _rendered(client.get(f"/properties?{BOTH}"))
        # One call for the request. More would mean a surface computing its own
        # -- the disagreement this whole date-threading exists to prevent.
        assert spy.call_count == 1

    def test_the_badge_and_the_dropdown_count_agree(self, client, rows):
        body = _rendered(client.get("/properties"))
        # Three rows are overdue in the fixture; the option beside the filter
        # says so, and the badges under it mark the same three.
        assert body.count("fa-hourglass-end") >= 3
        assert "Overdue (3)" in body


class TestTheOverdueLink:
    def test_it_is_offered_when_something_is_late(self, client, rows):
        body = _rendered(client.get("/properties"))
        assert 'id="overdue-count-link"' in body
        assert "3 overdue" in body

    def test_it_is_absent_when_nothing_is_late(self, client, profile):
        db.session.add(
            Property(
                source_email_id="calm",
                title="Plot calm",
                search_profile_id=profile.id,
                owner_verdict="interested",
            )
        )
        db.session.commit()
        body = _rendered(client.get("/properties"))
        # A standing "0 overdue" would be a line about nothing.
        assert 'id="overdue-count-link"' not in body

    def test_the_link_carries_the_filters_already_applied(self, client, rows):
        body = _rendered(client.get("/properties?verdict=rejected"))
        link = [
            href
            for href in _links(body, "/properties?")
            if "action=overdue" in href and "page=1" in href
        ]
        assert link, "the overdue link is missing"
        assert any("verdict=rejected" in href for href in link)


class TestTheDueDateNeverLandsWithoutAnAction:
    def test_a_date_with_a_blank_action_is_refused_by_the_route(self, client, profile):
        row = Property(
            source_email_id="dangling",
            title="Plot",
            search_profile_id=profile.id,
        )
        db.session.add(row)
        db.session.commit()

        client.post(
            f"/properties/{row.id}/review",
            data={"verdict": "waiting", "next_action": "  ", "due_on": "2026-09-20"},
        )
        db.session.expire_all()
        stored = db.session.get(Property, row.id)
        assert stored.next_action_due_on is None
        assert stored.owner_verdict is None

    def test_clearing_the_action_clears_its_date(self, client, profile):
        row = Property(
            source_email_id="clear-action",
            title="Plot",
            search_profile_id=profile.id,
            owner_verdict="waiting",
            next_action="ask",
            next_action_due_on=date(2026, 9, 20),
        )
        db.session.add(row)
        db.session.commit()

        owner_review.set_review(row, decision="waiting", action=None)
        assert row.next_action is None
        # A date with nothing due is a reminder about nothing -- the database
        # says so too (ck_properties_due_needs_action).
        assert row.next_action_due_on is None


def test_a_row_untouched_by_this_feature_still_renders(client, profile):
    """Every existing row is `undecided` with no action, and must be ordinary."""
    db.session.add(
        Property(
            source_email_id="legacy",
            title="Plot legacy",
            search_profile_id=profile.id,
            created_at=datetime.utcnow() - timedelta(days=400),
        )
    )
    db.session.commit()
    body = _rendered(client.get("/properties"))
    assert "Plot legacy" in body
    # No badge: `undecided` is most of the table, and a badge on most of the
    # table marks nothing.
    assert "Not decided yet" not in body
