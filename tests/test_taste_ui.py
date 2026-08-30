"""The taste ranking on the surfaces (#498), asserted BY VALUE.

The property page renders numbers, not just sections (the None×None lesson);
the list's taste mode orders current scores ahead of stale and unscored rows
in BOTH directions; the CSV export accepts the same sort and carries the
same provenance; the compact API answers with the score AND its state; and
the review reason is a textarea a paragraph fits into.
"""

import csv
import io

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import taste_service
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


def _taste_block(version, score, scorer=None):
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
    current.taste = _taste_block(version, 40.0)
    current.taste_score = 40.0
    stale = _mk_property(profile_row, title="Stale ninetynine")
    stale.taste = _taste_block(version - 1 if version > 1 else 0, 99.0)
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
