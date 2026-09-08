"""The taste ranking on the surfaces (#498), asserted BY VALUE.

The property page renders numbers, not just sections (the None×None lesson);
the list's taste mode orders current scores ahead of stale and unscored rows
in BOTH directions; the CSV export accepts the same sort and carries the
same provenance; the compact API answers with the score AND its state; and
the review reason is a textarea a paragraph fits into.
"""

import csv
import io
import re
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import taste_service
from services.taste_recommendation import RecommendationContext
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
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _mk_property(profile_row, **overrides):
    values = dict(
        source_email_id=f"taste-ui:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=200000,
        area=250,
        municipality="Malpica",
        search_profile_id=profile_row.id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _taste_block(version, score, prop, scorer=None):
    return {
        "status": "ok",
        "score": score,
        "reasons_ru": ["Похоже на эталон 969."],
        "matched_likes": ["regular plot"],
        "matched_dislikes": [],
        "closest_reference_id": None,
        "confidence": "medium",
        "profile_version": version,
        "scorer_version": scorer
        if scorer is not None
        else taste_service.TASTE_SCORER_VERSION,
        "facts_fingerprint": taste_service.facts_fingerprint(
            taste_service.gather_facts(prop)
        ),
        "scored_at": "2026-08-30T12:00:00+00:00",
    }


def _insert_profile_version():
    row = TasteProfile(
        provider="claude",
        signals_fingerprint="a" * 64,
        source={
            "signals": [{"property_id": 1, "verdict": "interested", "reason": "x"}]
        },
        profile={
            "likes": [
                {
                    "trait": "sea",
                    "weight": 1,
                    "evidence": "x",
                    "evidence_property_ids": [1],
                }
            ],
            "dislikes": [],
            "dealbreakers": [],
            "summary_ru": "Вам нравится море.",
        },
    )
    db.session.add(row)
    db.session.commit()
    return row.id


@pytest.fixture
def ranked_rows(app, profile_row):
    """One current score (40), one stale-but-higher (99), one unscored."""
    version = _insert_profile_version()
    current = _mk_property(profile_row, title="Current forty")
    current.taste = _taste_block(version, 40.0, current)
    current.taste_score = 40.0
    stale = _mk_property(profile_row, title="Stale ninetynine")
    stale.taste = _taste_block(version - 1 if version > 1 else 0, 99.0, stale)
    stale.taste_score = 99.0
    unscored = _mk_property(profile_row, title="Unscored")
    db.session.commit()
    return {
        "version": version,
        "current": current,
        "stale": stale,
        "unscored": unscored,
    }


class TestTheList:
    def test_taste_mode_ranks_current_ahead_of_stale_in_both_directions(
        self, client, ranked_rows
    ):
        for order in ("desc", "asc"):
            page = client.get(f"/properties?mode=taste&order={order}")
            assert page.status_code == 200
            html = page.data.decode()
            assert html.index("Current forty") < html.index("Stale ninetynine"), (
                f"stale 99 outranked current 40 with order={order}"
            )

    def test_the_coverage_line_counts_current_scores_against_the_version(
        self, client, ranked_rows
    ):
        page = client.get("/properties")
        html = page.data.decode()
        assert page.status_code == 200
        # 1 of 3, against the version the rows were scored under.
        assert f"1 of 3 scored against profile v{ranked_rows['version']}" in html

    def test_without_a_profile_the_page_stays_dormant(self, client, profile_row):
        _mk_property(profile_row)
        page = client.get("/properties")
        assert page.status_code == 200
        assert b"scored against profile" not in page.data

    def test_the_sort_option_and_mode_button_render(self, client, ranked_rows):
        html = client.get("/properties").data.decode()
        assert 'value="taste_score"' in html
        assert "mode-taste-btn" in html

    def test_two_current_scores_really_reverse_between_desc_and_asc(
        self, client, app, profile_row
    ):
        """One current score cannot tell a reversed order from an ignored
        one (the codex finding); two can."""
        version = _insert_profile_version()
        low = _mk_property(profile_row, title="Lowtwenty")
        low.taste = _taste_block(version, 20.0, low)
        low.taste_score = 20.0
        high = _mk_property(profile_row, title="Highninety")
        high.taste = _taste_block(version, 90.0, high)
        high.taste_score = 90.0
        db.session.commit()
        desc = client.get("/properties?mode=taste&order=desc").data.decode()
        asc = client.get("/properties?mode=taste&order=asc").data.decode()
        assert desc.index("Highninety") < desc.index("Lowtwenty")
        assert asc.index("Lowtwenty") < asc.index("Highninety")


class TestTheCsv:
    def test_the_export_carries_the_taste_columns_and_the_sort(
        self, client, ranked_rows
    ):
        response = client.get("/properties/export.csv?sort=taste_score&order=desc")
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.data.decode())))
        header = rows[0]
        for column in (
            "Taste Score",
            "Taste State",
            "Taste Profile Version",
            "Taste Scored At",
        ):
            assert column in header, f"CSV lost {column}"
        by_title = {row[header.index("Title")]: row for row in rows[1:]}
        current = by_title["Current forty"]
        assert current[header.index("Taste Score")] == "40.0"
        assert current[header.index("Taste State")] == "ok"
        assert current[header.index("Taste Profile Version")] == str(
            ranked_rows["version"]
        )
        stale = by_title["Stale ninetynine"]
        assert stale[header.index("Taste State")] == "stale"
        unscored = by_title["Unscored"]
        assert unscored[header.index("Taste Score")] == ""
        assert unscored[header.index("Taste State")] == "none"
        # The export sorted by taste the way the page does: current first.
        titles = [row[header.index("Title")] for row in rows[1:]]
        assert titles.index("Current forty") < titles.index("Stale ninetynine")


class TestTheDetailPage:
    def test_the_card_renders_the_number_and_a_reason(self, client, ranked_rows):
        prop = ranked_rows["current"]
        page = client.get(f"/properties/{prop.id}")
        assert page.status_code == 200
        html = page.data.decode()
        assert "taste-card" in html
        assert "40/100" in html
        assert "Похоже на эталон 969." in html
        assert f"profile v{ranked_rows['version']}" in html

    def test_a_stale_score_says_so(self, client, ranked_rows):
        page = client.get(f"/properties/{ranked_rows['stale'].id}")
        html = page.data.decode()
        assert page.status_code == 200
        assert "99/100" in html
        assert "earlier taste profile" in html

    def test_an_unscored_row_says_nobody_scored_it(self, client, ranked_rows):
        page = client.get(f"/properties/{ranked_rows['unscored'].id}")
        html = page.data.decode()
        assert page.status_code == 200
        assert "Not scored against the taste profile" in html

    def test_the_review_reason_is_a_textarea(self, client, ranked_rows):
        page = client.get(f"/properties/{ranked_rows['current'].id}")
        html = page.data.decode()
        assert (
            '<textarea class="form-control form-control-sm" id="review-reason"' in html
        )


class TestRecommendationSurface:
    def test_list_and_cards_show_evidence_without_a_fit_percentage(
        self, client, app, profile_row
    ):
        """The browser receives the v4 reading as evidence, including the
        hard distinction between a reference and a bookmarked rejection."""
        reference = _mk_property(profile_row, title="Reference house", is_favorite=True)
        candidate = _mk_property(profile_row, title="Evidence candidate")
        rejected = _mk_property(
            profile_row,
            title="Rejected bookmark",
            is_favorite=True,
            owner_verdict="rejected",
        )
        profile = {
            "state": "current",
            "version": 4,
            "positive_reference_count": 3,
            "mapped_clause_count": 2,
            "unapplied_clause_count": 1,
        }
        readings = {
            reference.id: {
                "state": "reference",
                "nearest_positive_reference": None,
                "matches": [],
                "conflicts": [],
                "needs_verification": [],
            },
            candidate.id: {
                "state": "candidate",
                "nearest_positive_reference": {
                    "id": reference.id,
                    "visual_matched_aspect_ids": ["visual_appeal"],
                },
                "matches": [{"label_key": "recommendation_aspect_sea_view"}],
                "conflicts": [{"label_key": "recommendation_aspect_house_condition"}],
                "needs_verification": [
                    {"label_key": "recommendation_aspect_plot_outline"}
                ],
            },
            rejected.id: {
                "state": "rejected",
                "nearest_positive_reference": None,
                "matches": [],
                "conflicts": [],
                "needs_verification": [],
            },
        }

        def _context(items, *_args, **_kwargs):
            return RecommendationContext(
                profile=profile,
                readings={item.id: readings[item.id] for item in items},
            )

        with patch("routes.main_routes.taste_recommendation.build_context", _context):
            page = client.get("/properties?profile_id=all")
            cards = client.get("/properties?profile_id=all&view_type=cards")

        assert page.status_code == cards.status_code == 200
        body = page.get_data(as_text=True)
        cards_body = cards.get_data(as_text=True)
        assert 'id="recommendation-profile-status"' in body
        assert "3 positive references and 2 recorded preferences" in body
        assert "1 preference(s) are not used yet" in body
        assert 'data-recommendation-state="reference"' in body
        assert 'data-recommendation-state="candidate"' in body
        assert 'data-recommendation-state="rejected"' in body
        assert "Closest positive reference:" in body
        assert f"#{reference.id}" in body
        assert 'data-recommendation-visual-resemblance="true"' in body
        assert "Also resembles:" in body
        assert "appearance" in body
        assert "sea view" in body
        assert "house condition" in body
        assert "plot shape" in body
        assert "Known conflicts need attention." in body
        assert "Rejected — not recommended" in body
        profile_status = re.search(
            r'id="recommendation-profile-status"[^>]*>(.*?)</span>', body, re.DOTALL
        )
        assert profile_status and "0 of 3" not in profile_status.group(1)
        assert (
            "/100"
            not in body[
                body.index("Evidence candidate") : body.index("Rejected bookmark")
            ]
        )
        # Cards are a separate DOM branch and must carry the same explanation.
        assert cards_body.count('data-recommendation-state="candidate"') == 1
        assert "Known conflicts need attention." in cards_body

    def test_spanish_recommendation_labels_render_on_the_live_page(
        self, client, profile_row
    ):
        prop = _mk_property(profile_row, title="Spanish recommendation")
        context = RecommendationContext(
            profile={"state": "pending", "version": None},
            readings={
                prop.id: {
                    "state": "candidate",
                    "nearest_positive_reference": None,
                    "matches": [{"label_key": "recommendation_aspect_sea_view"}],
                    "conflicts": [],
                    "needs_verification": [],
                }
            },
        )
        with client.session_transaction() as session:
            session["language"] = "es"
        with patch(
            "routes.main_routes.taste_recommendation.build_context",
            return_value=context,
        ):
            body = client.get("/properties?profile_id=all").get_data(as_text=True)

        assert "Las recomendaciones se están preparando" in body
        assert "Candidata" in body
        assert "vistas al mar" in body

    def test_recommendation_sort_selects_the_candidate_before_pagination(
        self, client, profile_row
    ):
        """The query builds the full recommendation context before it takes
        a page.  The page-size control has a deliberate floor of ten, so a
        requested one proves the same boundary by excluding the eleventh row.
        """
        candidate = _mk_property(profile_row, title="Top recommendation")
        _ = [
            _mk_property(profile_row, title=f"Lower recommendation {index}")
            for index in range(10)
        ]

        def _context(items, *_args, **_kwargs):
            return RecommendationContext(
                profile={"state": "current", "version": 4},
                readings={
                    item.id: {
                        "state": "candidate",
                        "nearest_positive_reference": None,
                        "matches": [],
                        "conflicts": [],
                        "needs_verification": [],
                        "rank_value": 100.0 if item.id == candidate.id else 1.0,
                    }
                    for item in items
                },
            )

        with patch("routes.main_routes.taste_recommendation.build_context", _context):
            body = client.get(
                "/properties?profile_id=all&mode=recommendation&sort=recommendation&per_page=1"
            ).get_data(as_text=True)

        assert body.index("Top recommendation") < body.index("Lower recommendation 0")
        assert "Lower recommendation 9" not in body
        assert 'id="mode-recommendation-btn"' in body
        assert 'value="recommendation" selected' in body

    def test_recommendation_mode_hides_only_confirmed_hard_exclusions(
        self, client, profile_row
    ):
        excluded = _mk_property(profile_row, title="Confirmed hard conflict")
        _mk_property(profile_row, title="Unknown remains eligible")

        def _context(items, *_args, **_kwargs):
            return RecommendationContext(
                profile={"state": "current", "version": 4},
                readings={
                    item.id: {
                        "state": "candidate",
                        "eligibility": "excluded"
                        if item.id == excluded.id
                        else "eligible",
                        "hard_exclusions": [{"aspect_id": "plot_outline"}]
                        if item.id == excluded.id
                        else [],
                        "nearest_positive_reference": None,
                        "matches": [],
                        "conflicts": [],
                        "needs_verification": [],
                        "rank_value": 1.0,
                    }
                    for item in items
                },
            )

        with patch("routes.main_routes.taste_recommendation.build_context", _context):
            body = client.get(
                "/properties?profile_id=all&mode=recommendation&sort=recommendation"
            ).get_data(as_text=True)

        assert "Confirmed hard conflict" not in body
        assert "Unknown remains eligible" in body
        assert 'id="recommendation-hard-exclusions"' in body

    def test_ordinary_sort_builds_recommendations_for_the_page_only(
        self, client, profile_row
    ):
        for index in range(11):
            _mk_property(profile_row, title=f"Ordinary row {index}")
        observed_sizes = []

        def _context(items, *_args, **_kwargs):
            items = list(items)
            observed_sizes.append(len(items))
            return RecommendationContext(
                profile={"state": "current", "version": 4},
                readings={},
            )

        with patch("routes.main_routes.taste_recommendation.build_context", _context):
            response = client.get("/properties?profile_id=all&per_page=10")

        assert response.status_code == 200
        assert observed_sizes == [10]

    def test_compass_leaves_the_favorites_only_scope_for_candidates(
        self, client, profile_row
    ):
        reference = _mk_property(
            profile_row, title="Favorite reference", is_favorite=True
        )
        candidate = _mk_property(profile_row, title="Non-favorite candidate")

        def _context(items, *_args, **_kwargs):
            return RecommendationContext(
                profile={"state": "current", "version": 4},
                readings={
                    item.id: {
                        "state": "reference"
                        if item.id == reference.id
                        else "candidate",
                        "nearest_positive_reference": None,
                        "matches": [],
                        "conflicts": [],
                        "needs_verification": [],
                        "rank_value": 100.0 if item.id == candidate.id else 1.0,
                    }
                    for item in items
                },
            )

        with patch("routes.main_routes.taste_recommendation.build_context", _context):
            favorites_page = client.get("/properties?profile_id=all&favorites=on")
            favorites_body = favorites_page.get_data(as_text=True)
            compass = re.search(
                r'id="mode-recommendation-btn"[^>]*href="([^"]+)"',
                favorites_body,
            )
            assert compass, "Favorites view did not render the recommendation compass"
            href = compass.group(1).replace("&amp;", "&")
            recommendation_page = client.get(href)

        assert favorites_page.status_code == recommendation_page.status_code == 200
        assert "Non-favorite candidate" not in favorites_body
        assert "favorites=" not in href
        assert "Non-favorite candidate" in recommendation_page.get_data(as_text=True)


class TestTheCompactApi:
    def test_the_default_payload_carries_score_and_state(self, client, ranked_rows):
        profile_id = ranked_rows["current"].search_profile_id
        data = client.get(f"/api/properties?profile_id={profile_id}").get_json()
        rows = {p["title"]: p for p in data["properties"]}
        assert rows["Current forty"]["taste_score"] == 40.0
        assert rows["Current forty"]["taste_state"] == "ok"
        assert rows["Stale ninetynine"]["taste_state"] == "stale"
        assert rows["Unscored"]["taste_score"] is None
        assert rows["Unscored"]["taste_state"] == "none"


@pytest.mark.parametrize("view_type", ["list", "cards"])
def test_recommendation_disclosure_bounds_long_repeated_evidence(
    client, app, profile_row, view_type
):
    candidate = _mk_property(profile_row, title="Compact recommendation")

    def facet(name):
        return {"aspect_id": name, "label_key": f"recommendation_aspect_{name}"}

    reading = {
        "state": "candidate",
        "nearest_positive_reference": {
            "id": candidate.id,
            "visual_matched_aspect_ids": ["house_character", "visual_appeal"],
        },
        "matches": [facet("sea_view"), facet("plot_area_m2"), facet("house_character")],
        "conflicts": [facet("house_condition")] * 3,
        "needs_verification": [facet("house_condition")] * 25
        + [facet("plot_outline")] * 4,
    }
    context = RecommendationContext(
        profile={"state": "current", "positive_reference_count": 1},
        readings={candidate.id: reading},
    )
    with patch(
        "routes.main_routes.taste_recommendation.build_context", return_value=context
    ):
        response = client.get(f"/properties?profile_id=all&view_type={view_type}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    block = re.search(
        r'<div class="recommendation-reading[^>]+data-recommendation-state="candidate".*?</details>',
        body,
        re.DOTALL,
    ).group(0)
    preview, details = block.split("<details", 1)
    assert details.startswith(' class="recommendation-details">')  # closed by default
    assert "Topics to check: 2" in details
    assert "Conflicts: 1" in preview
    assert preview.count("text-bg-success-subtle") == 2
    # Preserve every unique topic, and keep conflicting and unknown copies
    # separate: only same-outcome duplication is collapsed.
    assert details.count("text-bg-success-subtle") == 3
    assert details.count("text-bg-warning-subtle") == 2
    assert details.count("text-bg-danger-subtle") == 1
    assert details.count("house condition") == 2
    assert "≈ house character" in details and "≈ appearance" in details
    button = re.search(
        r'<a id="mode-recommendation-btn".*?</a>', body, re.DOTALL
    ).group(0)
    assert re.sub(r"<[^>]*>", "", button).strip() == "Recommendations"
