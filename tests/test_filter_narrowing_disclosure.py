"""The count line says when the filter bar narrowed it, and by how much.

Measured 2026-08-21 against production: "All subscriptions" showed
"23 properties found" while the same selection without the filter bar holds
511 -- a sea-view filter rode along in the chip's own link (the page's link
contract carries every filter on purpose, see `base_args`), the chips beside
the count kept their unfiltered badges (60 on one of them), and nothing on
the page said the other 488 rows were filtered out rather than missing. The
owner read it as a broken subscription selection. That is #98's shape at the
level of the page's own total: a narrowed count presented as the whole one.

So the count line carries a disclosure -- "Filters: 23 of 511 shown
(clear filters)" -- whose baseline is the same subscription selection plus
the toolbar switches (Favorites, Hide removed), which is exactly the set the
clear link lands on. Pinned here:

* the numbers, by value, because a note that renders `None of None` passes a
  presence check (the cadastre card lesson);
* that the clear link keeps the subscription selection and the toolbar
  switches and drops only the filter bar, and that following it really shows
  the baseline count;
* that no note renders without a filter, on an unknown filter value (nothing
  was applied, so there is no narrowing to describe), or when the filter hid
  nothing -- and that in each of those cases the page really rendered, since
  the error path of `routes/main_routes.py` also draws a page with no note
  (the test_listing_search_by_url lesson).
"""

import html
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        norte = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        bogdan = SearchProfile(
            name="Bogdan",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([norte, bogdan])
        db.session.commit()
        db.session.add_all(
            [
                Property(
                    source_email_id="sea-yes",
                    title="Plot with a confirmed sea view",
                    municipality="Cudillero",
                    search_profile_id=norte.id,
                    listing_status="active",
                    is_favorite=True,
                    enrichment={"environment": {"sea_view": "yes"}},
                ),
                Property(
                    source_email_id="inland",
                    title="Plot inland",
                    municipality="Cudillero",
                    search_profile_id=norte.id,
                    listing_status="active",
                    is_favorite=True,
                ),
                Property(
                    source_email_id="sea-likely",
                    title="Plot with a likely sea view",
                    municipality="Cudillero",
                    search_profile_id=norte.id,
                    listing_status="active",
                    enrichment={"environment": {"sea_view": "likely"}},
                ),
                Property(
                    source_email_id="bogdan-house",
                    title="House in Bogdan",
                    municipality="Gijon",
                    search_profile_id=bogdan.id,
                    listing_status="active",
                ),
                # Hidden by the default Hide removed switch: part of the
                # baseline only when that switch is off, never a thing the
                # filter-bar disclosure counts on its own.
                Property(
                    source_email_id="sea-removed",
                    title="Removed plot with sea view",
                    municipality="Cudillero",
                    search_profile_id=norte.id,
                    listing_status="removed",
                    enrichment={"environment": {"sea_view": "yes"}},
                ),
            ]
        )
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _clear_link_href(body: str) -> str:
    match = re.search(r'id="clear-filters-link"\s+href="([^"]+)"', body)
    assert match, "the narrowing note must carry the clear-filters link"
    return html.unescape(match.group(1))


class TestTheNoteRenders:
    def test_a_narrowing_filter_is_disclosed_with_both_numbers(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "sea_view": "likely"}
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # 2 sea-view rows of the 4 live listings across both subscriptions
        # (the removed one is out of both numbers: Hide removed is on).
        assert "2 properties found" in body
        assert 'id="filter-bar-narrowing-note"' in body
        assert "Filters: 2 of 4 shown" in body

    def test_the_baseline_is_the_current_subscription_selection(self, client, app):
        with app.app_context():
            norte_id = SearchProfile.query.filter_by(name="Land at Norte").one().id
        response = client.get(
            "/properties", query_string={"profile_id": norte_id, "sea_view": "likely"}
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Norte holds 3 live rows; Bogdan's house is outside the selection
        # and must not pad the baseline.
        assert "Filters: 2 of 3 shown" in body

    def test_the_baseline_keeps_the_toolbar_switches(self, client):
        # Favorites is a toolbar switch, not a filter-bar field: with it on,
        # the baseline is the favorites themselves, so the note reads
        # "1 of 2" and not "1 of 4".
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "favorites": "on",
                "sea_view": "likely",
            },
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Filters: 1 of 2 shown" in body

    def test_hide_removed_off_widens_both_numbers(self, client):
        # `mode` marks the request as coming from the filter form, so an
        # absent hide_removed is the box unticked (utils/listing_status_scope).
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "mode": "combined",
                "sea_view": "likely",
            },
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # The removed sea-view row is back in both the filtered set and the
        # baseline: 3 of the 5 stored listings.
        assert "Filters: 3 of 5 shown" in body


class TestTheClearLink:
    def test_it_drops_the_filter_and_keeps_the_selection(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "sea_view": "likely"}
        )
        href = _clear_link_href(response.get_data(as_text=True))
        assert "sea_view" not in href
        assert "profile_id=all" in href

    def test_following_it_lands_on_the_baseline_count(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "sea_view": "likely"}
        )
        href = _clear_link_href(response.get_data(as_text=True))
        cleared = client.get(href)
        assert cleared.status_code == 200
        body = cleared.get_data(as_text=True)
        # The number the note promised, now as the page's own total -- and
        # the rows the filter hid are really back.
        assert "4 properties found" in body
        assert "Plot inland" in body
        assert 'id="filter-bar-narrowing-note"' not in body

    def test_it_keeps_the_favorites_switch(self, client):
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "favorites": "on",
                "sea_view": "likely",
            },
        )
        href = _clear_link_href(response.get_data(as_text=True))
        assert "favorites=on" in href
        cleared = client.get(href)
        assert "2 properties found" in cleared.get_data(as_text=True)

    def test_it_drops_every_filter_the_page_applies(self, app, client):
        """The class, not the example: the link is built from the record of
        the request (`_clear_filters_url`), so every key of `current_filters`
        that is a filter must be absent from it, whichever filters tomorrow
        adds — the hand-written list it replaces was the repository's most
        frequent stale copy (utils/listing_filters.py)."""
        from flask import template_rendered
        from urllib.parse import parse_qs, urlparse

        from utils.listing_filters import NON_FILTERS

        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            response = client.get(
                "/properties",
                query_string={
                    "profile_id": "all",
                    "sea_view": "likely",
                    "search": "Plot",
                    "similar": "70",
                    "favorites": "on",
                },
            )
        finally:
            template_rendered.disconnect(record, app)
        assert response.status_code == 200 and seen
        href = _clear_link_href(response.get_data(as_text=True))
        carried = set(parse_qs(urlparse(href).query).keys())
        not_filters = set(NON_FILTERS) | {
            "favorites",
            "hide_removed",
            "sort_by",
            "active_mode",
        }
        leaked = sorted(
            key
            for key in seen[-1]["current_filters"]
            if key not in not_filters and key in carried
        )
        assert not leaked, f"the clear link still carries: {leaked}"
        assert "profile_id=all" in href and "favorites=on" in href
        assert "page=1" in href


class TestNoNoteWithoutANarrowing:
    def test_a_bare_page_carries_no_note_and_really_rendered(self, client):
        response = client.get("/properties", query_string={"profile_id": "all"})
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # The page rendered its rows -- the error path also draws a page
        # with no note, and it would pass a bare absence check.
        assert "4 properties found" in body
        assert "Plot inland" in body
        assert 'id="filter-bar-narrowing-note"' not in body

    def test_an_unknown_filter_value_is_not_a_narrowing(self, client):
        # `sea_view=banana` applies nothing, so there is no narrowing to
        # describe -- a note here would claim a filter that never ran.
        response = client.get(
            "/properties", query_string={"profile_id": "all", "sea_view": "banana"}
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "4 properties found" in body
        assert 'id="filter-bar-narrowing-note"' not in body

    def test_a_filter_that_hides_nothing_earns_no_line(self, client, app):
        with app.app_context():
            norte_id = SearchProfile.query.filter_by(name="Land at Norte").one().id
        response = client.get(
            "/properties",
            query_string={"profile_id": norte_id, "municipality": "Cudillero"},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "3 properties found" in body
        assert 'id="filter-bar-narrowing-note"' not in body
