"""The date filter keeps the listings added inside the days the reader named.

The owner's ask (2026-11-14): the list had no way to select by date. The date
is `created_at` -- when the listing entered this app, the value the row prints
under "Added" and the default sort reads -- because it is the one date every
source carries. A listing imported by link has no `email_date`, so a filter on
that column would have been a date filter for mail only.

What must hold, and why each case is here:

* Both bounds are inclusive whole days: `added_from=D` keeps the first instant
  of D, `added_to=D` keeps the last instant of D and drops the first of D+1.
* A row with no date matches neither bound: an unknown date is not inside a
  range (#98).
* The reading does not depend on where the row came from or on `email_date`.
* A value that names no day applies no filter, reports no narrowing and is not
  drawn as an applied condition.
* The filter rides the page's own links, the export and the map (#439/#445);
  `GET /api/properties` is covered by the sweep in
  `tests/test_api_properties_reads_the_pages_filters.py`.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from services import listing_attribute_filters as laf
from tests import setup_test_environment

# The asked range in every test below: 1 to 7 November 2026, inclusive.
FROM, TO = "2026-11-01", "2026-11-07"

ROWS = {
    "day_before": datetime(2026, 10, 31, 23, 59, 59),
    "first_instant": datetime(2026, 11, 1, 0, 0, 0),
    "middle": datetime(2026, 11, 4, 12, 30, 0),
    "last_instant": datetime(2026, 11, 7, 23, 59, 59),
    "day_after": datetime(2026, 11, 8, 0, 0, 0),
}
INSIDE = {"first_instant", "middle", "last_instant"}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for position, (key, created) in enumerate(ROWS.items()):
            # Alternating doors: an alert email (with an email date far
            # outside the asked range) and a link import (with none). The
            # filter must read neither.
            by_mail = position % 2 == 0
            db.session.add(
                Property(
                    source_email_id=(
                        f"added_{key}" if by_mail else f"manual:fotocasa:added_{key}"
                    ),
                    title=f"Row-{key}",
                    municipality="Castrillon",
                    property_category="land",
                    property_subtype="plot",
                    price=40000,
                    location_lat=43.5,
                    location_lon=-6.5,
                    search_profile_id=profile.id,
                    listing_status="active",
                    created_at=created,
                    email_date=datetime(2019, 1, 1) if by_mail else None,
                )
            )
        db.session.add(
            Property(
                source_email_id="added_undated",
                title="Row-undated",
                municipality="Castrillon",
                property_category="land",
                property_subtype="plot",
                price=40000,
                location_lat=43.5,
                location_lon=-6.5,
                search_profile_id=profile.id,
                listing_status="active",
            )
        )
        db.session.commit()
        # The column default fills a date on insert; the undated row is made
        # undated afterwards, the way a legacy row is.
        Property.query.filter_by(source_email_id="added_undated").update(
            {"created_at": None}
        )
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _shown(body: str) -> set[str]:
    return set(re.findall(r"Row-(\w+)", body))


def _page(client, query: str) -> str:
    response = client.get(f"/properties?profile_id=all&{query}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _context(app, client, query: str):
    seen = []

    def record(sender, template, context, **extra):
        if template.name == "properties.html":
            seen.append(context)

    template_rendered.connect(record, app)
    try:
        assert client.get(f"/properties?profile_id=all&{query}").status_code == 200
    finally:
        template_rendered.disconnect(record, app)
    assert seen, "properties.html did not render"
    return seen[-1]


class TestTheBounds:
    def test_from_keeps_the_first_instant_of_the_day(self, client):
        assert _shown(_page(client, f"added_from={FROM}")) == INSIDE | {"day_after"}

    def test_to_keeps_the_whole_last_day_and_nothing_after(self, client):
        assert _shown(_page(client, f"added_to={TO}")) == INSIDE | {"day_before"}

    def test_both_bounds_keep_exactly_the_named_days(self, client):
        assert _shown(_page(client, f"added_from={FROM}&added_to={TO}")) == INSIDE

    def test_one_day_is_a_range(self, client):
        body = _page(client, "added_from=2026-11-04&added_to=2026-11-04")
        assert _shown(body) == {"middle"}

    def test_a_row_with_no_date_matches_neither_bound(self, client):
        assert "undated" in _shown(_page(client, "per_page=100"))
        assert "undated" not in _shown(_page(client, "added_from=1990-01-01"))
        assert "undated" not in _shown(_page(client, "added_to=2100-01-01"))


class TestUnreadableValues:
    @pytest.mark.parametrize(
        "value", ["banana", "20261101", "2026-13-01", "2026-11-31", "2026-W45-1"]
    )
    def test_it_filters_nothing_and_reports_no_narrowing(self, app, client, value):
        context = _context(app, client, f"added_from={value}")
        assert context["pagination"].total == len(ROWS) + 1
        # Identity contract: an unapplied filter is not a narrowing.
        assert context["filter_bar_scope_total"] is None
        # And it is not drawn as an applied condition.
        assert context["current_filters"]["added_from"] == ""

    def test_a_real_value_is_reported_as_a_narrowing(self, app, client):
        context = _context(app, client, f"added_from={FROM}&added_to={TO}")
        assert context["pagination"].total == len(INSIDE)
        assert context["filter_bar_scope_total"] == len(ROWS) + 1

    def test_the_reading_is_strict(self):
        assert laf.read_added_day("2026-11-01") == date(2026, 11, 1)
        assert laf.read_added_day(" 2026-11-01 ") == date(2026, 11, 1)
        for raw in (None, "", "banana", "20261101", "2026-11-1", "2026-02-30"):
            assert laf.read_added_day(raw) is None

    def test_the_last_representable_day_does_not_raise(self, app):
        """`added_to=9999-12-31` has no following day to cut at."""
        with app.app_context():
            query = Property.query
            assert laf.filter_by_added_to(query, Property, "9999-12-31") is query
            assert laf.filter_by_added_from(query, Property, "9999-12-31") is not query


class TestTheFilterTravels:
    QUERY = f"added_from={FROM}&added_to={TO}"

    def test_the_sort_headers_carry_both_bounds(self, client):
        body = _page(client, self.QUERY)
        sort_link = re.search(r'href="(/properties\?[^"]*sort=price[^"]*)"', body)
        assert sort_link, "the page drew no price sort header"
        href = unescape(sort_link.group(1))
        assert f"added_from={FROM}" in href and f"added_to={TO}" in href

    def test_the_export_link_carries_both_bounds(self, client):
        body = _page(client, self.QUERY)
        export = re.search(r'href="(/properties/export\.csv[^"]*)"', body)
        assert export, "the page drew no Export CSV link"
        href = unescape(export.group(1))
        assert f"added_from={FROM}" in href and f"added_to={TO}" in href

    def test_the_export_applies_it(self, client):
        response = client.get(f"/properties/export.csv?profile_id=all&{self.QUERY}")
        assert response.status_code == 200
        assert _shown(response.get_data(as_text=True)) == INSIDE

    def test_the_map_applies_it(self, client):
        body = client.get(f"/map?profile_id=all&{self.QUERY}").get_data(as_text=True)
        match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
        assert match, "could not find the marker payload on the map page"
        assert set(re.findall(r"Row-(\w+)", unescape(match.group(1)))) == INSIDE


class TestTheControl:
    def test_the_inputs_keep_what_was_applied(self, client):
        body = _page(client, f"added_from={FROM}&added_to={TO}")
        lower = re.search(r'<input[^>]*name="added_from"[^>]*>', body, re.DOTALL)
        upper = re.search(r'<input[^>]*name="added_to"[^>]*>', body, re.DOTALL)
        assert lower and upper, "the filter form has no date inputs"
        assert 'type="date"' in lower.group(0) and f'value="{FROM}"' in lower.group(0)
        assert 'type="date"' in upper.group(0) and f'value="{TO}"' in upper.group(0)

    def test_each_bound_is_a_removable_condition(self, client):
        """An applied condition stays visible outside the closed panel, and
        removing one bound keeps the other (docs/design/README.md)."""
        body = _page(client, f"added_from={FROM}&added_to={TO}")
        chips = re.search(
            r'id="active-filter-chips">(.*?)</div>', body, re.DOTALL
        ).group(1)
        hrefs = [unescape(h) for h in re.findall(r'href="([^"]+)"', chips)]
        without_from = [h for h in hrefs if "added_from" not in h and "added_to" in h]
        without_to = [h for h in hrefs if "added_to" not in h and "added_from" in h]
        assert len(without_from) == 1 and len(without_to) == 1
        assert _shown(client.get(without_from[0]).get_data(as_text=True)) == INSIDE | {
            "day_before"
        }
        assert FROM in chips and TO in chips
