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
* and, most importantly, no control may *claim* to filter by sea view or by
  beach travel time. `Property` has no beach target and no sea-view field of
  its own, and per #98 not one of the 350 rows holds a single travel time. A
  control rendered as working over empty data is the failure mode this file
  exists to prevent.
"""

import re

import pytest

from app import create_app, db
from models import Land, Property, SearchProfile
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
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

    def test_navbar_links_lands_and_labels_it_an_archive(
        self, client, scored_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        nav = body.split("</nav>", 1)[0]
        match = re.search(r'href="/lands"[^>]*>(.*?)</a>', nav, re.S)
        assert match, "the navbar must still reach /lands"
        assert "archive" in match.group(1).lower()


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


class TestUnavailableControlsAreHonest:
    """Sea view and beach travel time have no data behind them on `Property`.

    They must be visible as *unavailable*, never as working filters.
    """

    def test_sea_view_control_is_disabled_and_explains_itself(
        self, client, scored_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        match = re.search(r"<input[^>]*name=\"sea_view\"[^>]*>", body)
        assert match, "the sea-view control should be visible, marked unavailable"
        assert "disabled" in match.group(0)

    def test_unavailable_controls_link_to_the_tracking_issue(
        self, client, scored_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        assert "issues/98" in body

    def test_sea_view_parameter_does_not_silently_filter(
        self, client, scored_properties
    ):
        """A stale bookmark carrying sea_view=on must not appear to work:
        the page shows the same rows it shows without the parameter."""
        plain = client.get("/properties").get_data(as_text=True)
        with_param = client.get("/properties?sea_view=on").get_data(as_text=True)
        for title in ("InvestorPickUniqueTitle", "LifestylePickUniqueTitle"):
            assert (title in plain) == (title in with_param)
        assert "InvestorPickUniqueTitle" in with_param

    def test_beach_sort_is_never_offered_as_an_enabled_option(
        self, client, scored_properties
    ):
        body = client.get("/properties").get_data(as_text=True)
        for match in re.finditer(
            r"<option[^>]*value=\"travel_time_nearest_beach\"[^>]*>", body
        ):
            assert "disabled" in match.group(0), (
                "the beach sort has no data behind it on Property and must not "
                "be selectable"
            )

    def test_beach_sort_parameter_does_not_pretend_to_sort(
        self, client, scored_properties
    ):
        """`Property` has no beach travel time at all, so the page must fall
        back to its documented default order rather than silently claim the
        rows are sorted by distance to the beach."""
        fallback = client.get("/properties?sort=travel_time_nearest_beach").get_data(
            as_text=True
        )
        default = client.get("/properties").get_data(as_text=True)
        assert _order(
            fallback, "LifestylePickUniqueTitle", "InvestorPickUniqueTitle"
        ) == _order(default, "LifestylePickUniqueTitle", "InvestorPickUniqueTitle")


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


class TestLandsStaysReachableAsArchive:
    def test_lands_page_still_renders(self, client, app):
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
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "ArchivedLandUniqueTitle" in body

    def test_lands_page_declares_itself_an_archive(self, client):
        body = client.get("/lands").get_data(as_text=True)
        assert "archive" in body.lower()

    def test_lands_investment_rating_filter_and_sort_still_work(self, client, app):
        """The investment-rating filter and sort now share one expression
        with /properties. The archived page must keep behaving exactly as it
        did before that deduplication."""
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

        filtered = client.get("/lands?inv_metr=EXCELLENT").get_data(as_text=True)
        assert "ExcellentLandUniqueTitle" in filtered
        assert "ModerateLandUniqueTitle" not in filtered

        # Sorted by rating, the EXCELLENT row leads despite its lower score.
        sorted_body = client.get(
            "/lands?sort=investment_metrics&order=desc&view_type=list"
        ).get_data(as_text=True)
        assert _order(
            sorted_body, "ExcellentLandUniqueTitle", "ModerateLandUniqueTitle"
        )

        csv_body = client.get("/export.csv?inv_metr=EXCELLENT").get_data(as_text=True)
        assert "ExcellentLandUniqueTitle" in csv_body
        assert "ModerateLandUniqueTitle" not in csv_body
