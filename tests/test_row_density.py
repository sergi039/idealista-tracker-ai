"""The density of a listing row and a card is a layout promise (owner, 2026-11-14).

Density itself is proven in a browser on a built image: a headless test cannot
lay out a table (docs/rules/testing.md). What a test can pin is what the
layout stands on, so a later edit cannot undo it without a test going red:

* the four row groups stay four cells, and the reason cell has its one flex
  wrapper -- the element that lets the rating and the Assessment disclosure
  share a line instead of stacking in three storeys;
* the decision cell holds exactly one set of decision buttons, and every row
  and card keeps the `listing-<id>` anchor the detail page returns to;
* the column widths in `workspace.css` add up to the whole table at every
  width and in both modes -- a fixed-layout table whose percentages do not
  sum is a table the browser quietly rescales;
* the rules that keep "Details and checks" on one line, the thumbnail wide
  enough for it, the card's price/area/place on one line and its travel
  times in a row exist in the stylesheet.
"""

import re
from pathlib import Path

import pytest
from unittest.mock import patch

from app import create_app, db
from models import Property, SearchProfile
from services.taste_recommendation import RecommendationContext
from tests import setup_test_environment

CSS = (
    Path(__file__).resolve().parents[1] / "static" / "css" / "workspace.css"
).read_text(encoding="utf-8")
COLUMNS = ("col-title", "col-price", "col-score", "col-actions")


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app(testing=True)
    with app.app_context():
        db.create_all()
        profile = SearchProfile(name="Galicia", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.flush()
        for index in range(2):
            db.session.add(
                Property(
                    source_email_id=f"density:{index}",
                    title=f"Density house {index}",
                    municipality="Arteixo",
                    property_category="housing",
                    property_subtype="house",
                    search_profile_id=profile.id,
                    listing_status="active",
                    price=250000,
                    area=230,
                    location_lat=43.3,
                    location_lon=-8.5,
                    location_accuracy="approximate",
                    url=f"https://www.idealista.com/inmueble/7000{index}/",
                    score_total=50.0,
                )
            )
        db.session.commit()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _block(css, header):
    """The body of one media query, matched by brace depth."""
    start = css.index(header)
    open_brace = css.index("{", start)
    depth = 0
    for index in range(open_brace, len(css)):
        depth += {"{": 1, "}": -1}.get(css[index], 0)
        if depth == 0:
            return css[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _last_widths(css, prefix):
    """The width each column ends up with: the LAST declaration wins, like the
    cascade, so a later override cannot hide behind the first match."""
    found = {}
    for column in COLUMNS:
        matches = re.findall(
            re.escape(prefix) + r"\s*\.%s\s*\{\s*width:\s*([\d.]+)%%" % column, css
        )
        assert matches, f"{prefix} .{column} has no percentage width"
        found[column] = float(matches[-1])
    return found


def _rows(body):
    """Each list row, as (id, markup)."""
    parts = re.split(r'(?=<tr id="listing-\d+")', body)
    return [
        (
            re.match(r'<tr id="(listing-\d+)"', part).group(1),
            part[: part.index("</tr>")],
        )
        for part in parts
        if part.startswith('<tr id="listing-')
    ]


def _cells(row):
    return re.findall(r'<td class="([^"]*col-[a-z]+)">(.*?)</td>', row, re.S)


def _density_block(css):
    """The shared density rules, from their comment to the cards' comment."""
    start = css.index("Row density (owner, 2026-11-14")
    return css[start : css.index("/* Cards: the same economy.", start)]


class TestRowMarkup:
    def test_every_row_is_four_groups_in_order_with_its_own_anchor(self, client):
        body = client.get("/properties?mode=combined&view_type=list").get_data(
            as_text=True
        )
        rows = _rows(body)
        assert len(rows) == 2
        for row_id, row in rows:
            cells = _cells(row)
            assert [re.search(r"col-[a-z]+", c).group(0) for c, _ in cells] == list(
                COLUMNS
            )
            by_group = {re.search(r"col-[a-z]+", c).group(0): html for c, html in cells}
            assert "Density house" in by_group["col-title"]
            assert "250,000" in by_group["col-price"]
            assert "workspace-secondary-scores" in by_group["col-score"]
            assert "data-owner-decision" in by_group["col-actions"]
            number = row_id.split("-")[1]
            assert f'data-owner-decision-id="{number}"' in by_group["col-actions"]

    def test_the_reason_cell_wraps_everything_it_holds_once(self, client):
        for _, row in _rows(
            client.get("/properties?mode=combined&view_type=list").get_data(
                as_text=True
            )
        ):
            cell = dict(
                (re.search(r"col-[a-z]+", c).group(0), h) for c, h in _cells(row)
            )["col-score"]
            assert cell.count('class="workspace-reason-cell"') == 1
            inner = cell.strip()[len('<div class="workspace-reason-cell">') :]
            assert cell.strip().startswith('<div class="workspace-reason-cell">')
            assert cell.strip().endswith("</div>")
            # The disclosure is inside the wrapper, not left beside it.
            assert inner.index("workspace-secondary-scores") < inner.rindex("</div>")
            assert cell.count("<details") == cell.count("</details>") == 1

    def test_the_decision_group_is_exactly_the_three_decisions_once(self, client):
        for _, row in _rows(
            client.get("/properties?mode=combined&view_type=list").get_data(
                as_text=True
            )
        ):
            cell = dict(
                (re.search(r"col-[a-z]+", c).group(0), h) for c, h in _cells(row)
            )["col-actions"]
            assert cell.count("data-owner-decision-id") == 1
            assert sorted(re.findall(r'data-decision="([a-z]+)"', cell)) == [
                "interested",
                "rejected",
                "waiting",
            ]
            assert "data-owner-undecided" in cell

    def test_every_card_keeps_its_anchor_and_its_density_hooks(self, client):
        body = client.get("/properties?mode=combined&view_type=cards").get_data(
            as_text=True
        )
        cards = re.split(r'(?=<div id="listing-\d+" class="col-md-6)', body)[1:]
        assert len(cards) == 2
        for card in cards:
            number = re.match(r'<div id="listing-(\d+)"', card).group(1)
            assert f'data-owner-decision-id="{number}"' in card
            for hook in (
                "property-card-body",
                "property-card-line",
                "property-card-price",
                "property-card-location",
                "property-card-travel",
            ):
                assert hook in card, f"{hook} missing from card {number}"


def test_in_the_recommended_tab_the_reason_rating_and_assessment_share_the_wrapper(
    client,
):
    def context(items, *_args, **_kwargs):
        reading = {
            "state": "candidate",
            "recommended": True,
            "eligibility": "eligible",
            "needs_review": False,
            "nearest_positive_reference": {"id": 1283, "visual_matched_aspect_ids": []},
            "matches": [],
            "conflicts": [],
            "needs_verification": [],
        }
        return RecommendationContext(
            profile={"state": "current", "version": 1, "usable_signal_count": 3},
            readings={item.id: dict(reading) for item in items},
        )

    with patch("routes.main_routes.taste_recommendation.build_context", context):
        body = client.get(
            "/properties?mode=recommendation&view_type=list&verdict=all"
        ).get_data(as_text=True)
    rows = _rows(body)
    assert len(rows) == 2
    for _, row in rows:
        cell = dict((re.search(r"col-[a-z]+", c).group(0), h) for c, h in _cells(row))[
            "col-score"
        ]
        wrapper = cell.strip()
        assert wrapper.startswith('<div class="workspace-reason-cell">')
        for part in (
            "recommendation-reading",
            "data-recommendation-vote",
            "workspace-secondary-scores",
        ):
            assert wrapper.count(part) == 1, part
        # The reason comes first, then the rating, then the assessment.
        assert (
            wrapper.index("recommendation-reading")
            < wrapper.index("data-recommendation-vote")
            < wrapper.index("workspace-secondary-scores")
        )


class TestColumnWidthsAddUp:
    def test_desktop_widths_fill_the_table(self):
        widths = _last_widths(CSS[: CSS.index("@media")], ".workspace-data-table")
        assert sum(widths.values()) == pytest.approx(100)
        assert widths["col-title"] == max(widths.values())

    def test_tablet_widths_fill_the_table_in_both_modes(self):
        tablet = _block(CSS, "@media (max-width: 1199.98px)")
        combined = _last_widths(tablet, ".workspace-data-table")
        assert sum(combined.values()) == pytest.approx(100)
        reason = dict(combined)
        for column in ("col-title", "col-score"):
            matches = re.findall(
                r"\.workspace-data-table\.recommendation-table\s*\.%s\s*\{\s*width:\s*([\d.]+)%%"
                % column,
                tablet,
            )
            assert matches, f"recommendation table has no tablet width for {column}"
            reason[column] = float(matches[-1])
        assert sum(reason.values()) == pytest.approx(100)

    def test_the_reason_widths_do_not_reach_a_phone(self):
        """A phone's cells are grid items. A more specific tablet width leaked
        into them once (a 79px reason cell, rows 130px taller): it must sit
        inside a `min-width: 768px` guard."""
        tablet = _block(CSS, "@media (max-width: 1199.98px)")
        guarded = _block(tablet, "@media (min-width: 768px)")
        assert ".workspace-data-table.recommendation-table .col-score" in guarded
        assert (
            ".workspace-data-table.recommendation-table .col-score"
            not in tablet.replace(guarded, "")
        )


class TestDensityRulesExist:
    def test_details_and_checks_cannot_wrap_where_the_column_follows_its_width(self):
        """From 1200px the thumbnail column is as wide as the label, so neither
        language overlaps the copy; a tablet column is too narrow for that and
        keeps the label free to wrap (a 150px Spanish label left 64px of copy)."""
        wide = _block(CSS, "@media (min-width: 1200px)")
        assert re.search(
            r"\.workspace-row-details\s*>\s*summary\s*\{\s*white-space:\s*nowrap", wide
        )
        assert "grid-template-columns: minmax(7rem, max-content) minmax(0, 1fr)" in CSS
        tablet = _block(CSS, "@media (min-width: 768px) and (max-width: 1199.98px)")
        assert "grid-template-columns: 4.5rem minmax(0, 1fr)" in tablet

    def test_the_decision_controls_share_a_line_from_the_tablet_width_up(self):
        block = _density_block(CSS)
        desktop = _block(block, "@media (min-width: 768px)")
        assert re.search(
            r"\.workspace-owner-decision\s*\{\s*display:\s*flex;\s*flex-wrap:\s*wrap",
            desktop,
        )

    def test_the_rating_and_the_assessment_share_a_line_without_a_column(self):
        desktop = _block(_density_block(CSS), "@media (min-width: 768px)")
        rule = re.search(r"\.workspace-reason-cell\s*\{([^}]*)\}", desktop).group(1)
        assert "display: flex" in rule and "flex-wrap: wrap" in rule
        assert "flex-direction" not in rule
        # A phone keeps the wrapper out of the way of its own grid.
        assert re.search(
            r"\.workspace-reason-cell\s*\{\s*display:\s*contents", _density_block(CSS)
        )

    def test_a_card_keeps_price_area_and_place_on_a_line_and_travel_in_a_row(self):
        assert re.search(
            r"\.property-card-travel\s*\.properties-travel-times\s*\{\s*flex-direction:\s*row",
            CSS,
        )
        assert re.search(
            r"\.property-card-line\s*\{\s*display:\s*flex;\s*flex-wrap:\s*wrap", CSS
        )
        assert "#properties-cards-view .listing-card-photo" in CSS
