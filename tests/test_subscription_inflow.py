"""`/profiles` says when each subscription's listings arrived, and how many of
them the owner cared about.

The owner's ask (2026-11-14): one total per subscription could not show which
searches still bring listings in, nor which bring the ones worth opening. So
the page counts, per subscription, what was added in the last 7 / 30 / 90
calendar days and in all time, and how many of those are a favorite or marked
Interested (services/subscription_inflow.py).

What must hold:

* A window is whole calendar days including today; the first instant of its
  first day is inside, the instant before is not.
* "Of interest" is a favorite or an Interested verdict, counted once when a
  row is both, and nothing else -- not `waiting`, not `rejected`.
* Every stored row counts, withdrawn and rejected ones included, and a row
  with no date is in "all time" only.
* **Every printed count is the number of rows its link opens.** The counts
  and the `added_from` filter share one clause; this test follows the links.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import subscription_inflow
from tests import setup_test_environment

TODAY = date(2026, 11, 14)
# The first day of each window: 7, 30 and 90 calendar days including TODAY.
START_7, START_30, START_90 = date(2026, 11, 8), date(2026, 10, 16), date(2026, 8, 17)


def _row(profile_id, key, created, **kw):
    return Property(
        source_email_id=f"inflow_{key}",
        title=f"Row-{key}",
        municipality="Castrillon",
        property_category="land",
        property_subtype="plot",
        price=40000,
        search_profile_id=profile_id,
        listing_status=kw.pop("listing_status", "active"),
        created_at=created,
        **kw,
    )


@pytest.fixture
def world(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(subscription_inflow, "utc_today", lambda: TODAY)
    with app.app_context():
        db.create_all()
        coast = SearchProfile(
            name="Coast houses",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        inland = SearchProfile(
            name="Inland plots",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        quiet = SearchProfile(
            name="Quiet feed",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([coast, inland, quiet])
        db.session.commit()
        rows = [
            # Coast: the window edges, and each way of being of interest.
            _row(coast.id, "today_fav", datetime(2026, 11, 14, 9), is_favorite=True),
            _row(coast.id, "edge7_in", datetime(2026, 11, 8, 0, 0, 0)),
            _row(
                coast.id,
                "edge7_out",
                datetime(2026, 11, 7, 23, 59, 59),
                owner_verdict="interested",
            ),
            # Both a favorite and Interested: one row of interest, not two.
            _row(
                coast.id,
                "month_both",
                datetime(2026, 10, 20, 12),
                is_favorite=True,
                owner_verdict="interested",
            ),
            _row(
                coast.id,
                "month_waiting",
                datetime(2026, 10, 20),
                owner_verdict="waiting",
            ),
            # Rejected and withdrawn rows are still what the feed delivered.
            _row(
                coast.id,
                "quarter_rejected",
                datetime(2026, 9, 1),
                owner_verdict="rejected",
            ),
            _row(
                coast.id,
                "quarter_removed",
                datetime(2026, 9, 2),
                listing_status="removed",
            ),
            _row(coast.id, "old_fav", datetime(2025, 1, 1), is_favorite=True),
            _row(coast.id, "undated", datetime(2026, 11, 14)),
            # Inland: only old rows, none of interest.
            _row(inland.id, "inland_old_a", datetime(2025, 3, 1)),
            _row(inland.id, "inland_old_b", datetime(2025, 3, 2)),
            # No subscription at all.
            _row(None, "orphan_recent", datetime(2026, 11, 13), is_favorite=True),
        ]
        db.session.add_all(rows)
        db.session.commit()
        Property.query.filter_by(source_email_id="inflow_undated").update(
            {"created_at": None}
        )
        db.session.commit()
        yield {
            "app": app,
            "client": app.test_client(),
            "coast": coast.id,
            "inland": inland.id,
            "quiet": quiet.id,
        }
        db.drop_all()


def test_the_windows_are_calendar_days_including_today():
    assert subscription_inflow.periods(TODAY) == [
        {"days": 7, "start": START_7},
        {"days": 30, "start": START_30},
        {"days": 90, "start": START_90},
    ]


class TestTheCounts:
    def _read(self, world):
        return subscription_inflow.read(db.session, Property, TODAY)

    def test_added_per_window(self, world):
        coast = self._read(world)[world["coast"]]
        # today_fav + edge7_in; edge7_out is one second too early.
        assert coast[7]["added"] == 2
        # ... + edge7_out, month_both, month_waiting.
        assert coast[30]["added"] == 5
        # ... + quarter_rejected, quarter_removed.
        assert coast[90]["added"] == 7
        # ... + old_fav and the undated row, which is in no window.
        assert coast["total"]["added"] == 9

    def test_of_interest_is_a_favorite_or_interested_counted_once(self, world):
        coast = self._read(world)[world["coast"]]
        assert coast[7]["interest"] == 1  # today_fav
        assert coast[30]["interest"] == 3  # + edge7_out, month_both (once)
        assert coast[90]["interest"] == 3  # waiting and rejected are not interest
        assert coast["total"]["interest"] == 4  # + old_fav

    def test_a_feed_that_went_quiet_reads_as_one(self, world):
        inland = self._read(world)[world["inland"]]
        assert inland["total"] == {"added": 2, "interest": 0}
        assert [inland[days]["added"] for days in (7, 30, 90)] == [0, 0, 0]

    def test_listings_with_no_subscription_are_their_own_bucket(self, world):
        assert self._read(world)[None][7] == {"added": 1, "interest": 1}

    def test_a_subscription_with_no_listing_has_no_entry(self, world):
        assert world["quiet"] not in self._read(world)


def _inflow_section(body: str) -> str:
    match = re.search(r'<section[^>]*id="subscription-inflow".*?</section>', body, re.S)
    assert match, "/profiles drew no inflow section"
    return match.group(0)


def _inflow_rows(body: str) -> dict[str, list[dict]]:
    """`{profile ref: [cell, ...]}`, each cell its numbers and its link."""
    rows = {}
    for ref, html in re.findall(
        r'<tr data-inflow-profile="([^"]+)">(.*?)</tr>', _inflow_section(body), re.S
    ):
        cells = []
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)[1:]:
            link = re.search(
                r'data-role="inflow-added" href="([^"]+)">(\d+)</a>', cell, re.S
            )
            interest = re.search(r'data-role="inflow-interest">(\d+)<', cell)
            cells.append(
                {
                    "added": int(link.group(2)) if link else 0,
                    "href": unescape(link.group(1)) if link else None,
                    "interest": int(interest.group(1)) if interest else None,
                }
            )
        rows[ref] = cells
    return rows


class TestThePage:
    def _body(self, world, query=""):
        response = world["client"].get(f"/profiles{query}")
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def test_the_cells_carry_the_counts(self, world):
        rows = _inflow_rows(self._body(world))
        coast = rows[str(world["coast"])]
        assert [cell["added"] for cell in coast] == [2, 5, 7, 9]
        assert [cell["interest"] for cell in coast] == [1, 3, 3, 4]
        inland = rows[str(world["inland"])]
        assert [cell["added"] for cell in inland] == [0, 0, 0, 2]
        # A window nothing arrived in draws a plain zero, no link and no
        # "0 of interest" beside it.
        assert inland[0]["href"] is None and inland[0]["interest"] is None
        assert [cell["added"] for cell in rows["unassigned"]] == [1, 1, 1, 1]

    def test_a_subscription_with_no_listing_is_not_listed(self, world):
        assert str(world["quiet"]) not in _inflow_rows(self._body(world))

    def test_every_count_is_the_number_of_rows_its_link_opens(self, world):
        """The promise a counted link makes. Followed, never read: the
        rejected row, the withdrawn row and the window edges are all in the
        fixture, so a link that left one hide in place, or a count that cut
        at another instant than the filter, opens a different number."""
        followed = 0
        for ref, cells in _inflow_rows(self._body(world)).items():
            for cell in cells:
                if cell["href"] is None:
                    continue
                landing = world["client"].get(cell["href"] + "&per_page=100")
                assert landing.status_code == 200
                listed = set(
                    re.findall(
                        r'href="/properties/(\d+)"', landing.get_data(as_text=True)
                    )
                )
                assert len(listed) == cell["added"], (
                    f"subscription {ref}: the cell says {cell['added']} and "
                    f"{cell['href']} opens {len(listed)}"
                )
                followed += 1
        assert followed == 4 + 1 + 4  # coast, inland (all time), unassigned

    def test_the_period_links_name_the_first_day_of_the_window(self, world):
        coast = _inflow_rows(self._body(world))[str(world["coast"])]
        assert f"added_from={START_7.isoformat()}" in coast[0]["href"]
        assert f"added_from={START_30.isoformat()}" in coast[1]["href"]
        assert f"added_from={START_90.isoformat()}" in coast[2]["href"]
        assert "added_from" not in coast[3]["href"]

    def test_the_total_in_the_management_table_opens_every_stored_row(self, world):
        """The Properties column prints the same total; its link has to state
        every hide a bare page applies, or 9 opens 7."""
        body = self._body(world)
        row = re.search(
            rf'<tr data-profile-id="{world["coast"]}">(.*?)</tr>', body, re.S
        ).group(1)
        link = re.search(r'href="(/properties\?[^"]+)">\s*(\d+)\s*</a>', row)
        assert link and int(link.group(2)) == 9
        landing = world["client"].get(unescape(link.group(1)) + "&per_page=100")
        listed = set(
            re.findall(r'href="/properties/(\d+)"', landing.get_data(as_text=True))
        )
        assert len(listed) == 9

    def test_the_technical_view_draws_it_too(self, world):
        rows = _inflow_rows(self._body(world, "?view=technical"))
        assert [cell["added"] for cell in rows[str(world["coast"])]] == [2, 5, 7, 9]

    def test_the_headers_say_which_days(self, world):
        section = _inflow_section(self._body(world))
        assert "Last 7 days" in section and "Last 90 days" in section
        assert START_7.strftime("%d %b %y") in section
