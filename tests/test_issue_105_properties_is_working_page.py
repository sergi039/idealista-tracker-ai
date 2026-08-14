"""Issue #105: `/properties` is the working page, `/lands` is the archive.

The 2026-08-07 decision made `/lands` the working UI. On 2026-08-08 the owner
reversed it after checking where the data actually is: `lands` stopped growing
on 2026-02-18 (168 rows), every fresh listing lands in `properties`, and 77 of
the 182 fresh rows are houses -- which the `Land` model cannot represent at all.

These tests pin the reversal on the real routes through the Flask test client:

* `/` must resolve to `/properties`, not `/lands`;
* the navbar must point at `/properties` and label `/lands` an archive;
* the cards/list toggle and the combined/investment/lifestyle modes -- the two
  things the owner actually used on `/lands` -- must work on `/properties`;
* sea view filters on a real four-state verdict, and `unknown` never passes for
  a match. The beach sort stays dead: it needs Google Distance Matrix and per
  #98 no row holds a travel time, while sea view needs no paid API at all;
* and, most importantly, no control may *claim* to filter by sea view or by
  beach travel time. `Property` has no beach target and no sea-view field of
  its own, and per #98 not one of the 350 rows holds a single travel time. A
  control rendered as working over empty data is the failure mode this file
  exists to prevent.
"""

import html
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app import create_app, db
from models import Land, Property, SearchProfile
from tests import setup_test_environment


def _query_params(href):
    """Query parameters of an href, parsed rather than string-matched.

    A substring check would pass on a wrongly serialised link (`profile_id`
    concatenated into another value, HTML-escaped separators, a repeated
    parameter collapsed into `[6, 8]`), which is exactly the failure mode
    these navigation tests exist to catch.
    """
    return parse_qs(urlparse(html.unescape(href)).query, keep_blank_values=True)


def _hrefs_containing(body, needle):
    return [href for href in re.findall(r'href="([^"]+)"', body) if needle in href]


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
def scored_properties(app):
    """One profile, two properties whose investment/lifestyle ranking is the
    exact inverse of each other, so a mode switch has to reorder the page."""
    from datetime import datetime

    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        # InvestorPick wins on investment, LifestylePick wins on lifestyle,
        # and their combined scores put InvestorPick first.
        investor = Property(
            source_email_id="issue105_investor",
            title="InvestorPickUniqueTitle",
            municipality="Cudillero",
            search_profile_id=profile.id,
            listing_status="active",
            property_category="land",
            property_subtype="plot",
            price=90000,
            area=1000,
            score_total=80,
            score_investment=90,
            score_lifestyle=40,
            created_at=datetime(2026, 8, 1, 10, 0, 0),
            ai_analysis={
                "rental_market_analysis": {"investment_rating": "EXCELLENT - strong"}
            },
        )
        lifestyle = Property(
            source_email_id="issue105_lifestyle",
            title="LifestylePickUniqueTitle",
            municipality="Llanes",
            search_profile_id=profile.id,
            listing_status="active",
            property_category="housing",
            property_subtype="house",
            price=250000,
            area=180,
            score_total=70,
            score_investment=30,
            score_lifestyle=95,
            created_at=datetime(2026, 8, 8, 10, 0, 0),
            ai_analysis={
                "rental_market_analysis": {"investment_rating": "MODERATE - average"}
            },
        )
        db.session.add_all([investor, lifestyle])
        db.session.commit()

        return {"profile_id": profile.id}


def _order(body, first, second):
    """True when `first` appears before `second` in the rendered page."""
    assert first in body, f"{first!r} missing from the page"
    assert second in body, f"{second!r} missing from the page"
    return body.index(first) < body.index(second)


class TestRootGoesToProperties:
    def test_root_redirects_to_properties(self, client):
        resp = client.get("/")
        assert resp.status_code in (301, 302, 308)
        assert resp.headers["Location"].endswith("/properties")

    def test_root_follows_through_to_the_properties_page(
        self, client, scored_properties
    ):
        resp = client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "InvestorPickUniqueTitle" in body

    def test_default_page_still_shows_freshest_first(self, client, scored_properties):
        """The owner must see fresh listings without touching a query
        parameter -- so a bare /properties keeps sorting by date, and the
        newly added mode default must not silently switch it to score."""
        body = client.get("/properties").get_data(as_text=True)
        assert _order(body, "LifestylePickUniqueTitle", "InvestorPickUniqueTitle")


class TestNavbar:
    def test_navbar_points_at_properties(self, client, scored_properties):
        body = client.get("/properties").get_data(as_text=True)
        nav = body.split("</nav>", 1)[0]
        assert 'href="/properties"' in nav

    def test_navbar_offers_no_second_listing_page(self, client, scored_properties):
        """Superseded on 2026-08-09: one surface, not two.

        `/lands` used to hang in the navbar as "Lands (archive)". The archived
        rows are mirrored into `properties` under their own subscription, so
        the archive is a filter on the working page now -- a second link would
        put the owner back on the page they asked to have removed.
        """
        body = client.get("/properties").get_data(as_text=True)
        nav = body.split("</nav>", 1)[0]
        assert 'href="/lands"' not in nav


class TestViewTypeToggle:
    def test_cards_view_renders_cards(self, client, scored_properties):
        body = client.get("/properties?view_type=cards").get_data(as_text=True)
        assert 'id="properties-cards-view"' in body
        assert "InvestorPickUniqueTitle" in body

    def test_list_view_renders_the_table(self, client, scored_properties):
        body = client.get("/properties?view_type=list").get_data(as_text=True)
        assert 'id="properties-list-view"' in body
        assert "properties-list-table" in body
        assert "InvestorPickUniqueTitle" in body

    def test_both_toggle_controls_are_on_the_page(self, client, scored_properties):
        body = client.get("/properties").get_data(as_text=True)
        assert 'id="view-cards-btn"' in body
        assert 'id="view-list-btn"' in body

    def test_unknown_view_type_falls_back_instead_of_blanking_the_page(
        self, client, scored_properties
    ):
        resp = client.get("/properties?view_type=bogus")
        assert resp.status_code == 200
        assert "InvestorPickUniqueTitle" in resp.get_data(as_text=True)

    def test_both_views_render_a_real_travel_time(self, client, app):
        """The travel badges are shared by the table and the new cards, so a
        stored duration has to surface in both -- an empty cards view would
        otherwise look like "no travel data" instead of a template bug."""
        with app.app_context():
            profile = SearchProfile(
                name="Travelled",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(profile)
            db.session.commit()
            db.session.add(
                Property(
                    source_email_id="issue105_travel",
                    title="TravelledPropertyUniqueTitle",
                    search_profile_id=profile.id,
                    listing_status="active",
                    travel={
                        "origin": {"lat": 43.65, "lon": -7.84},
                        "targets": {
                            "airport": {
                                "status": "ok",
                                "mode": "driving",
                                "duration_min": 42,
                                "distance_km": 51.3,
                            }
                        },
                    },
                )
            )
            db.session.commit()
            profile_id = profile.id

        for view_type in ("cards", "list"):
            body = client.get(
                f"/properties?profile_id={profile_id}&view_type={view_type}"
            ).get_data(as_text=True)
            assert "TravelledPropertyUniqueTitle" in body
            assert "42m" in body, f"travel time missing from the {view_type} view"


class TestScoringModes:
    def test_investment_mode_ranks_by_investment_score(self, client, scored_properties):
        body = client.get("/properties?mode=investment").get_data(as_text=True)
        assert _order(body, "InvestorPickUniqueTitle", "LifestylePickUniqueTitle")

    def test_lifestyle_mode_ranks_by_lifestyle_score(self, client, scored_properties):
        body = client.get("/properties?mode=lifestyle").get_data(as_text=True)
        assert _order(body, "LifestylePickUniqueTitle", "InvestorPickUniqueTitle")

    def test_combined_mode_ranks_by_total_score(self, client, scored_properties):
        body = client.get("/properties?mode=combined").get_data(as_text=True)
        assert _order(body, "InvestorPickUniqueTitle", "LifestylePickUniqueTitle")

    def test_explicit_score_sorts_are_honoured(self, client, scored_properties):
        body = client.get("/properties?sort=score_lifestyle&order=desc").get_data(
            as_text=True
        )
        assert _order(body, "LifestylePickUniqueTitle", "InvestorPickUniqueTitle")

    def test_mode_buttons_are_rendered(self, client, scored_properties):
        body = client.get("/properties").get_data(as_text=True)
        for button_id in (
            "mode-combined-btn",
            "mode-investment-btn",
            "mode-lifestyle-btn",
        ):
            assert f'id="{button_id}"' in body

    def test_unknown_mode_falls_back_to_combined(self, client, scored_properties):
        resp = client.get("/properties?mode=bogus")
        assert resp.status_code == 200
        assert "InvestorPickUniqueTitle" in resp.get_data(as_text=True)


class TestInvestmentRatingFilter:
    def test_filter_keeps_only_matching_ratings(self, client, scored_properties):
        body = client.get("/properties?inv_metr=EXCELLENT").get_data(as_text=True)
        assert "InvestorPickUniqueTitle" in body
        assert "LifestylePickUniqueTitle" not in body

    def test_filter_is_offered_in_the_form(self, client, scored_properties):
        body = client.get("/properties").get_data(as_text=True)
        assert 'name="inv_metr"' in body


@pytest.fixture
def sea_view_properties(app):
    """One property per verdict, plus a mirrored legacy row.

    The legacy row matters most: its `true` came from the keyword pass over a
    truncated email body, so it must read as `likely` and never as `yes`.
    """
    from datetime import datetime

    with app.app_context():
        profile = SearchProfile(
            name="Coast", is_active=True, is_default=True, travel_targets={}
        )
        db.session.add(profile)
        db.session.commit()

        def _row(title, enrichment, day):
            return Property(
                source_email_id=f"seaview_{title}",
                title=title,
                municipality="Cudillero",
                search_profile_id=profile.id,
                listing_status="active",
                property_category="land",
                price=100000,
                area=1000,
                created_at=datetime(2026, 8, day, 10, 0, 0),
                enrichment=enrichment,
            )

        def _verdict(state, source):
            return {
                "environment": {
                    "sea_view": state,
                    "sea_view_detail": {"source": source, "reason": "test"},
                }
            }

        rows = [
            _row("SeaYesTitle", _verdict("yes", "text+geometry"), 1),
            _row("SeaLikelyTitle", _verdict("likely", "geometry"), 2),
            _row("SeaNoTitle", _verdict("no", "geometry"), 3),
            _row("SeaUnknownTitle", _verdict("unknown", "none"), 4),
            _row(
                "SeaLegacyTrueTitle",
                {"legacy_land": {"environment": {"sea_view": True}}},
                5,
            ),
            _row(
                "SeaLegacyFalseTitle",
                {"legacy_land": {"environment": {"sea_view": False}}},
                6,
            ),
        ]
        db.session.add_all(rows)
        db.session.commit()
        return {"profile_id": profile.id}


class TestSeaViewFilter:
    """Sea view is a four-state verdict now, and the filter must respect that.

    `unknown` is the state that matters: it means the estimate could not be
    computed -- an approximate coordinate, or a source that refused -- and it
    must never be quietly counted as either a match or a negative.
    """

    def test_the_control_is_a_working_three_way_select(
        self, client, sea_view_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        match = re.search(r"<select[^>]*name=\"sea_view\"[^>]*>", body)
        assert match, "the sea-view control should be a select"
        assert "disabled" not in match.group(0)
        for value in ('value=""', 'value="yes"', 'value="likely"'):
            assert value in body

    def test_confirmed_keeps_only_the_corroborated_row(
        self, client, sea_view_properties
    ):
        body = client.get("/properties?sea_view=yes").get_data(as_text=True)
        assert "SeaYesTitle" in body
        for title in (
            "SeaLikelyTitle",
            "SeaNoTitle",
            "SeaUnknownTitle",
            "SeaLegacyTrueTitle",
        ):
            assert title not in body

    def test_likely_keeps_confirmed_and_likely_and_nothing_else(
        self, client, sea_view_properties
    ):
        body = client.get("/properties?sea_view=likely").get_data(as_text=True)
        assert "SeaYesTitle" in body
        assert "SeaLikelyTitle" in body
        assert "SeaNoTitle" not in body
        assert "SeaUnknownTitle" not in body

    def test_unknown_is_never_treated_as_a_match(self, client, sea_view_properties):
        """The whole reason the state exists: an uncomputable estimate is not a
        quiet yes and not a quiet no."""
        for value in ("yes", "likely"):
            body = client.get(f"/properties?sea_view={value}").get_data(as_text=True)
            assert "SeaUnknownTitle" not in body
            assert "SeaLegacyFalseTitle" not in body

    def test_a_mirrored_legacy_flag_is_likely_and_never_confirmed(
        self, client, sea_view_properties
    ):
        confirmed = client.get("/properties?sea_view=yes").get_data(as_text=True)
        plausible = client.get("/properties?sea_view=likely").get_data(as_text=True)
        assert "SeaLegacyTrueTitle" not in confirmed
        assert "SeaLegacyTrueTitle" in plausible

    def test_a_stale_lands_bookmark_still_selects_something_sensible(
        self, client, sea_view_properties
    ):
        """`sea_view=on` is what the retired /lands page sent. Reading it as
        "confirmed or likely" beats both silently ignoring it and pretending it
        means `yes`."""
        body = client.get("/properties?sea_view=on").get_data(as_text=True)
        assert "SeaYesTitle" in body
        assert "SeaLikelyTitle" in body
        assert "SeaNoTitle" not in body

    def test_the_filter_survives_paging_and_sorting_links(
        self, client, sea_view_properties
    ):
        body = client.get("/properties?sea_view=likely&sort=price").get_data(
            as_text=True
        )
        hrefs = _hrefs_containing(body, "/properties?")
        assert hrefs, "the page should link back to itself"
        assert any(
            _query_params(href).get("sea_view") == ["likely"] for href in hrefs
        ), "an in-page link dropped the sea-view filter"

    def test_the_csv_export_carries_the_filter(self, client, sea_view_properties):
        body = client.get("/properties?sea_view=yes").get_data(as_text=True)
        export = _hrefs_containing(body, "export")
        assert export
        assert _query_params(export[0]).get("sea_view") == ["yes"]

    def test_a_positive_verdict_is_visible_on_the_row(
        self, client, sea_view_properties
    ):
        """A filter you cannot see the effect of is not much of a filter."""
        body = client.get("/properties?sea_view=likely").get_data(as_text=True)
        assert "sea_view_state" not in body, "a missing translation key leaked"
        assert body.count("fa-water") >= 2


class TestBeachSortIsHonestAboutItsData:
    """The #98 placeholder retired with issue #271: rows hold measured beach
    times now, the option is selectable, and the sort really sorts (pinned in
    tests/test_beach_sort_enabled.py). What survives here is the honesty
    half: rows *without* a measurement must sort last, never pretending to a
    beach distance nobody measured."""

    def test_beach_sort_is_offered_as_an_enabled_option(
        self, client, scored_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        matches = re.findall(
            r"<option[^>]*value=\"travel_time_nearest_beach\"[^>]*>", body
        )
        assert matches, "the beach sort option must be offered"
        for match in matches:
            assert "disabled" not in match

    def test_unmeasured_rows_sort_last_not_arbitrarily(self, client, scored_properties):
        """These fixtures carry no beach data at all, so under the beach sort
        every row is in the nulls-last tail and the id tiebreaker keeps the
        order stable — the page must not invent a beach order for them."""
        body = client.get(
            "/properties?sort=travel_time_nearest_beach&order=asc"
        ).get_data(as_text=True)
        assert _order(body, "InvestorPickUniqueTitle", "LifestylePickUniqueTitle"), (
            "both rows are unmeasured: the id tiebreaker, not an invented "
            "beach distance, decides their order"
        )


class TestSubscriptionContextSurvivesMapNavigation:
    """Jumping between the list and the map must not change which
    subscription you are looking at.

    Both pages auto-select a profile when none is given, and they do it by
    *different* rules -- /properties takes the richest active profile, /map
    the one with the most mappable rows. So a link that drops the selection
    does not merely lose a filter: it silently swaps the data set, and on the
    map the focused listing may not even be loaded.

    The links forward whatever selection is in play rather than interpreting
    it, so the `auto | all | selected(ids)` model coming with #104 -- where
    `profile_id` repeats -- passes through unchanged.
    """

    @pytest.fixture
    def mappable_properties(self, app):
        """Two profiles whose auto-selection rules disagree.

        `listed` is the default and has properties, so /properties picks it.
        `mapped` has more rows with coordinates, so /map picks that one
        instead. Any link that drops the selection therefore lands the user
        on the other subscription -- which is the defect under test, not a
        contrived setup.
        """
        with app.app_context():
            listed = SearchProfile(
                name="Listed default",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            mapped = SearchProfile(
                name="Mostly mapped",
                is_active=True,
                is_default=False,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add_all([listed, mapped])
            db.session.commit()

            def _make(slug, profile_id, coords):
                prop = Property(
                    source_email_id=f"issue105_nav_{slug}",
                    title=f"{slug}UniqueTitle",
                    search_profile_id=profile_id,
                    listing_status="active",
                    location_lat=coords[0] if coords else None,
                    location_lon=coords[1] if coords else None,
                )
                db.session.add(prop)
                db.session.commit()
                return prop.id

            listed_flat = _make("ListedFlat", listed.id, None)
            listed_pinned = _make("ListedPinned", listed.id, (43.50, -6.50))
            mapped_first = _make("MappedFirst", mapped.id, (43.55, -6.55))
            mapped_second = _make("MappedSecond", mapped.id, (43.60, -6.60))

            return {
                "listed_profile_id": listed.id,
                "mapped_profile_id": mapped.id,
                "listed_flat_id": listed_flat,
                "listed_pinned_id": listed_pinned,
                "mapped_ids": [mapped_first, mapped_second],
            }

    def _map_links(self, client, query):
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        return [_query_params(href) for href in _hrefs_containing(body, "focus=")]

    def _list_view_link(self, client, query):
        body = client.get(f"/map?{query}").get_data(as_text=True)
        match = re.search(r'<a id="map-list-view-link"[^>]*href="([^"]+)"', body)
        if match is None:
            match = re.search(r'href="([^"]+)"[^>]*id="map-list-view-link"', body)
        assert match, "the map needs a stable link back to the list view"
        return _query_params(match.group(1))

    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_card_map_link_keeps_the_selected_profile(
        self, client, mappable_properties, view_type
    ):
        profile_id = mappable_properties["mapped_profile_id"]
        links = self._map_links(
            client, f"profile_id={profile_id}&view_type={view_type}"
        )
        assert links, f"no map link rendered in the {view_type} view"

        focused = sorted(params["focus"][0] for params in links)
        assert focused == sorted(str(i) for i in mappable_properties["mapped_ids"])
        for params in links:
            assert params.get("profile_id") == [str(profile_id)]

    @pytest.mark.parametrize("view_type", ["cards", "list"])
    def test_card_map_link_keeps_an_explicit_all(
        self, client, mappable_properties, view_type
    ):
        links = self._map_links(client, f"profile_id=all&view_type={view_type}")
        assert links
        for params in links:
            assert params.get("profile_id") == ["all"]

    def test_card_map_link_carries_the_auto_resolved_selection(
        self, client, mappable_properties
    ):
        """No profile_id in the URL means every live subscription since
        2026-08-09, and the map must be told that -- left to itself it
        resolves a single profile of its own, and the focused listing may not
        even be loaded there."""
        links = self._map_links(client, "view_type=cards")
        assert links, "both subscriptions have mappable rows"
        for params in links:
            assert params.get("profile_id") == ["all"], (
                "the map link dropped the selection the list resolved"
            )

    def test_map_list_link_keeps_a_specific_profile(self, client, mappable_properties):
        profile_id = mappable_properties["mapped_profile_id"]
        params = self._list_view_link(client, f"profile_id={profile_id}")
        assert params.get("profile_id") == [str(profile_id)]

    def test_map_list_link_keeps_an_explicit_all(self, client, mappable_properties):
        params = self._list_view_link(client, "profile_id=all")
        assert params.get("profile_id") == ["all"]

    def test_map_list_link_carries_the_auto_resolved_profile(
        self, client, mappable_properties
    ):
        """/map auto-selects the profile with mappable rows; /properties would
        auto-select the default one instead. Without the parameter the user
        comes back to a different set of listings than the map showed."""
        params = self._list_view_link(client, "")
        assert params.get("profile_id") == [
            str(mappable_properties["mapped_profile_id"])
        ]

    def test_map_list_link_passes_a_repeated_profile_id_through(
        self, client, mappable_properties
    ):
        """#104 turns `profile_id` into a repeated parameter. The link only
        forwards what arrived, so it must survive as two parameters rather
        than being collapsed into one value or a stringified list."""
        first = mappable_properties["mapped_profile_id"]
        second = mappable_properties["listed_profile_id"]
        params = self._list_view_link(client, f"profile_id={first}&profile_id={second}")
        assert params.get("profile_id") == [str(first), str(second)]


class TestCsvExportMatchesThePageOrder:
    """Export CSV has to hand back the rows in the order shown on screen.

    The page offers "Inv. Metr." as a sort and puts `sort=investment_metrics`
    into the export link. If the export's allow-list does not know that value
    it silently reorders by date: same rows, different order, no error.
    """

    @pytest.fixture
    def rated_properties(self, app):
        from datetime import datetime

        with app.app_context():
            profile = SearchProfile(
                name="Rated",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(profile)
            db.session.commit()

            # Every sortable field disagrees with the date order on purpose:
            # a silent fallback to created_at would otherwise be invisible
            # because the rows happened to line up.
            rows = [
                # slug, rating, created, title, price, area, total, inv, life
                (
                    "excellent",
                    "EXCELLENT - strong",
                    datetime(2026, 8, 1),
                    "ZuluRatedUniqueTitle",
                    300000,
                    50,
                    40,
                    90,
                    20,
                ),
                (
                    "good",
                    "GOOD - steady",
                    datetime(2026, 8, 4),
                    "AlphaRatedUniqueTitle",
                    100000,
                    300,
                    90,
                    30,
                    80,
                ),
                (
                    "moderate",
                    "MODERATE - thin",
                    datetime(2026, 8, 8),
                    "MikeRatedUniqueTitle",
                    200000,
                    150,
                    65,
                    60,
                    55,
                ),
            ]
            ids = {}
            for slug, rating, created, title, price, area, total, inv, life in rows:
                prop = Property(
                    source_email_id=f"issue105_csv_{slug}",
                    title=title,
                    search_profile_id=profile.id,
                    listing_status="active",
                    created_at=created,
                    price=price,
                    area=area,
                    score_total=total,
                    score_investment=inv,
                    score_lifestyle=life,
                    ai_analysis={
                        "rental_market_analysis": {"investment_rating": rating}
                    },
                )
                db.session.add(prop)
                db.session.commit()
                ids[slug] = prop.id

            return {"profile_id": profile.id, "ids": ids}

    def _page_ids(self, client, query):
        body = client.get(f"/properties?{query}&view_type=list").get_data(as_text=True)
        return re.findall(r'data-property-id="(\d+)"', body)

    def _csv_ids(self, client, query):
        import csv
        import io

        text = client.get(f"/properties/export.csv?{query}").get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(text)))
        id_column = rows[0].index("ID")
        return [row[id_column] for row in rows[1:]]

    def test_export_keeps_the_investment_rating_order(self, client, rated_properties):
        query = "sort=investment_metrics&order=desc"
        expected = [
            str(rated_properties["ids"][slug])
            for slug in ("excellent", "good", "moderate")
        ]
        assert self._page_ids(client, query) == expected
        assert self._csv_ids(client, query) == expected

    def test_export_keeps_the_ascending_rating_order(self, client, rated_properties):
        query = "sort=investment_metrics&order=asc"
        expected = [
            str(rated_properties["ids"][slug])
            for slug in ("moderate", "good", "excellent")
        ]
        assert self._page_ids(client, query) == expected
        assert self._csv_ids(client, query) == expected

    def test_export_and_page_agree_on_the_score_sorts_too(
        self, client, rated_properties
    ):
        for sort_by in (
            "title",
            "created_at",
            "price",
            "area",
            "score_total",
            "score_investment",
            "score_lifestyle",
        ):
            query = f"sort={sort_by}&order=desc"
            page_ids = self._page_ids(client, query)
            # Guard the guard: every one of these sorts must differ from a
            # plain date sort, or the comparison below proves nothing.
            date_ids = self._page_ids(client, "sort=created_at&order=desc")
            if sort_by != "created_at":
                assert page_ids != date_ids, (
                    f"fixture is too weak: sort={sort_by} matches the date order"
                )
            assert page_ids == self._csv_ids(client, query), (
                f"export disagrees with the page for sort={sort_by}"
            )


class TestPropertiesPageSurvivesAFailure:
    def test_error_fallback_still_renders_the_page(self, client, monkeypatch):
        """The route swallows any failure and re-renders with an empty filter
        set. That fallback has to survive the new view-state plumbing rather
        than turning a handled error into a 500."""
        from services.search_profile_service import SearchProfileService

        def boom(*args, **kwargs):
            raise RuntimeError("profile lookup exploded")

        monkeypatch.setattr(SearchProfileService, "list_profiles", boom)

        resp = client.get("/properties")
        assert resp.status_code == 200


class TestLandsFoldsIntoTheOneSurface:
    """Superseded on 2026-08-09: `/lands` is no longer a page of its own.

    It rendered the frozen `lands` table behind an archive banner the owner
    never asked for. Those rows are mirrored into `properties` under the
    "Legacy Lands" subscription, so the working page already holds them and
    the route only has to stop being a second surface -- without breaking the
    bookmarks and the legacy detail links that still point at it.
    """

    def test_lands_redirects_to_the_one_surface(self, client, app):
        with app.app_context():
            db.session.add(
                Land(
                    source_email_id="issue105_land",
                    title="ArchivedLandUniqueTitle",
                    municipality="Cudillero",
                    listing_status="active",
                    score_total=55,
                )
            )
            db.session.commit()

        resp = client.get("/lands")
        assert resp.status_code in (301, 302, 308)
        assert resp.headers["Location"].endswith("/properties")

    def test_lands_query_string_still_lands_on_a_working_page(self, client):
        resp = client.get("/lands?inv_metr=EXCELLENT", follow_redirects=True)
        assert resp.status_code == 200
        assert 'id="filters-card"' in resp.get_data(as_text=True)

    def test_legacy_csv_export_keeps_filtering(self, client, app):
        """The archived rows still have their own CSV export, and the
        investment-rating filter it shares with /properties has to keep
        working there -- the export is the only thing left reading `lands`."""
        with app.app_context():
            db.session.add_all(
                [
                    Land(
                        source_email_id="issue105_land_excellent",
                        title="ExcellentLandUniqueTitle",
                        listing_status="active",
                        score_total=40,
                        ai_analysis={
                            "rental_market_analysis": {
                                "investment_rating": "EXCELLENT - strong demand"
                            }
                        },
                    ),
                    Land(
                        source_email_id="issue105_land_moderate",
                        title="ModerateLandUniqueTitle",
                        listing_status="active",
                        score_total=90,
                        ai_analysis={
                            "rental_market_analysis": {
                                "investment_rating": "MODERATE - thin market"
                            }
                        },
                    ),
                ]
            )
            db.session.commit()

        csv_body = client.get("/export.csv?inv_metr=EXCELLENT").get_data(as_text=True)
        assert "ExcellentLandUniqueTitle" in csv_body
        assert "ModerateLandUniqueTitle" not in csv_body
