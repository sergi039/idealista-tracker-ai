"""The density of a listing row and a card is a layout promise (owner, 2026-11-14,
2026-11-15).

Density itself is proven in a browser on a built image: a headless test cannot
lay out a table (docs/rules/testing.md). What a test can pin is what the
layout stands on, so a later edit cannot undo it without a test going red:

* the four row groups stay four cells; the reason cell and the price cell each
  have their one wrapper -- the elements that let the rating and the Assessment
  disclosure, and the area and the type badges, share a line instead of
  stacking;
* the decision cell holds exactly one set of decision buttons, and every row
  and card keeps the `listing-<id>` anchor the detail page returns to;
* what the stylesheet *ends up* with at a given viewport width (the cascade is
  read the way a browser reads it: media conditions, source order, the last
  declaration wins) -- the column widths add up to the whole table, the object
  is the widest column, the thumbnail column is as wide as the disclosure
  label needs, the disclosure keeps its 40px target, the cell padding is as
  small as agreed, and none of it reaches a phone;
* the rules that keep a card's price/area/place on one line and its travel
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


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declarations(body):
    """(name, value, important), the `padding` shorthand also written out as
    `padding-block` / `padding-inline` so a later reset cannot hide behind the
    longhand a test reads."""
    found = []
    for item in body.split(";"):
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        name = name.strip()
        value = re.sub(r"\s+", " ", value.strip())
        important = value.endswith("!important")
        value = value.removesuffix("!important").strip()
        found.append((name, value, important))
        if name == "padding":
            top, right, bottom, left = {
                1: lambda v: (v[0],) * 4,
                2: lambda v: (v[0], v[1], v[0], v[1]),
                3: lambda v: (v[0], v[1], v[2], v[1]),
                4: lambda v: tuple(v),
            }[len(parts := re.findall(r"var\([^)]*\)|\S+", value))](parts)
            found.append(
                (
                    "padding-block",
                    top if top == bottom else f"{top} {bottom}",
                    important,
                )
            )
            found.append(
                (
                    "padding-inline",
                    right if right == left else f"{right} {left}",
                    important,
                )
            )
    return found


def _rules(css, conditions=()):
    """Every style rule in source order as (media conditions, selectors,
    declarations); `@media` blocks are flattened, other at-rules (`@container`,
    `@keyframes`) are skipped. A selector list with a comma inside parentheses
    (`:is(.a, .b)`) is refused rather than silently mis-split."""
    css = _strip_comments(css)
    position = 0
    while True:
        brace = css.find("{", position)
        if brace == -1:
            return
        head = css[position:brace].strip()
        depth, end = 1, brace + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            end += 1
        body = css[brace + 1 : end - 1]
        if head.startswith("@media"):
            yield from _rules(body, conditions + (head[len("@media") :].strip(),))
        elif not head.startswith("@"):
            assert not re.search(r"\([^)]*,[^)]*\)", head), f"not modelled: {head}"
            selectors = [re.sub(r"\s+", " ", s.strip()) for s in head.split(",")]
            yield conditions, selectors, _declarations(body)
        position = end


def _applies(conditions, width):
    for condition in conditions:
        bounds = re.findall(r"\((min|max)-width:\s*([\d.]+)px\)", condition)
        if not bounds or re.sub(
            r"\((min|max)-width:\s*[\d.]+px\)|\band\b|\s", "", condition
        ):
            return False  # another feature (reduced motion...): not a width rule
        for kind, limit in bounds:
            if (kind == "min" and width < float(limit)) or (
                kind == "max" and width > float(limit)
            ):
                return False
    return True


def effective(selector, prop, width, css=CSS):
    """What `prop` ends up as for exactly this selector at this viewport width:
    the last matching declaration in source order, an `!important` one beating
    a plain one, and the flag kept in the value (`contents !important`) so
    dropping it is a red test. None when nothing declares it.

    Its limit, stated once: exact selector text only. It reads what this file
    wrote, it does not compute specificity between *different* selectors, so a
    more specific rule added elsewhere is invisible to it. That hole is closed
    where it matters by `TestNothingElseDeclaresWhatTheRowsRelyOn`, which lists
    every rule that may declare these properties. What it cannot see, by
    design: font sizes or `flex-direction` added to a selector it does not
    read, attribute selectors, physical longhands other than the two that
    test closes. Layout is proven by the browser measurement in the PR
    (docs/rules/testing.md); this pins the stylesheet it was measured on."""
    value, important = None, False
    for conditions, selectors, declarations in _rules(css):
        if selector not in selectors or not _applies(conditions, width):
            continue
        for name, new, flagged in declarations:
            if name == prop and (flagged or not important):
                value, important = new, flagged
    return None if value is None else value + (" !important" if important else "")


def declaring(prop, pattern, css=CSS):
    """Every (selector, value) in the stylesheet that declares `prop` on a
    selector matching `pattern`, whatever its media condition."""
    found = []
    for _conditions, selectors, declarations in _rules(css):
        for selector in selectors:
            if re.search(pattern, selector):
                found += [
                    (selector, value)
                    for name, value, _flag in declarations
                    if name == prop
                ]
    return found


def _percent(selector, width):
    value = effective(selector, "width", width)
    assert value and value.endswith("%"), f"{selector} has no percentage at {width}px"
    return float(value[:-1])


def _widths(width, recommendation):
    """The four column widths a table shows at this viewport. The recommendation
    table's own, more specific, rule wins over the shared one."""
    shared = {c: _percent(f".workspace-data-table .{c}", width) for c in COLUMNS}
    if not recommendation:
        return shared
    own = {
        c: effective(f".workspace-data-table.recommendation-table .{c}", "width", width)
        for c in COLUMNS
    }
    return {c: float(own[c][:-1]) if own[c] else shared[c] for c in COLUMNS}


DESKTOP = (1920, 1440, 1280, 1200)
TABLET = (1199, 1024, 834, 768)
PHONE = (767, 390, 320)


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
            # Keyboard and screen-reader order is the visual order: the three
            # decisions, then favourite and Open (no CSS `order` reshuffle).
            assert cell.index("data-owner-decision-id") < cell.index(
                "property-favorite-btn"
            )

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


class TestColumnWidths:
    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    @pytest.mark.parametrize("recommendation", [False, True])
    def test_the_widths_fill_the_table_at_every_width_in_both_modes(
        self, width, recommendation
    ):
        """A fixed-layout table whose percentages do not sum is a table the
        browser quietly rescales."""
        assert sum(_widths(width, recommendation).values()) == pytest.approx(100)

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    @pytest.mark.parametrize("recommendation", [False, True])
    def test_the_object_is_the_widest_column(self, width, recommendation):
        widths = _widths(width, recommendation)
        assert widths["col-title"] == max(widths.values())

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    @pytest.mark.parametrize("recommendation", [False, True])
    def test_a_price_and_the_decision_controls_keep_the_room_they_need(
        self, width, recommendation
    ):
        """Measured: at 1200px a price column of 10.5% broke `295,000€` in two
        (codex, then a browser at 1200px). The three decision buttons,
        favourite and Open need 230px beside each other plus 16px of cell
        padding: 21% of the 1174px table at 1440px is 247px. 20% left 235px
        and put the star, Open and the status on a second line (owner, review
        of #685); the browser measure counts the rows whose five controls
        share a `top`."""
        widths = _widths(width, recommendation)
        assert widths["col-price"] >= (12 if width in DESKTOP else 16)
        assert widths["col-actions"] >= (21 if width in DESKTOP else 19)

    def test_the_recommendation_reason_is_given_room_at_both_ends_of_the_range(self):
        """The reason is prose plus two rating buttons and the Assessment
        disclosure; Spanish needs about 300px for them on one line at 1440px."""
        assert _widths(1440, True)["col-score"] >= 26
        assert _widths(834, True)["col-score"] >= 29

    @pytest.mark.parametrize("width", PHONE)
    def test_no_percentage_width_reaches_a_phone(self, width):
        """A phone's cells are grid items, not columns: a column width that
        leaked into them once made a 79px reason cell and rows 130px taller."""
        for column in COLUMNS:
            assert (
                effective(
                    f".workspace-data-table.recommendation-table .{column}",
                    "width",
                    width,
                )
                is None
            )


class TestTheObjectCell:
    def test_the_thumbnail_column_is_as_wide_as_the_label_needs(self):
        """The disclosure label wraps inside the thumbnail column (two lines of
        12px text are 36px, inside the 40px target); 6.25rem holds the longest
        word of either language, `comprobaciones`."""
        for width in DESKTOP:
            assert (
                effective(".workspace-with-photo", "grid-template-columns", width)
                == "6.25rem minmax(0, 1fr)"
            )
            assert effective(".workspace-photo", "width", width) == "6.25rem"
            assert (
                effective(".workspace-photo .listing-thumb", "width", width)
                == "6.25rem"
            )
        for width in TABLET:
            assert (
                effective(".workspace-with-photo", "grid-template-columns", width)
                == "4.5rem minmax(0, 1fr)"
            )

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_the_label_may_wrap_and_keeps_its_40px_target(self, width):
        summary = ".workspace-row-details > summary"
        assert effective(summary, "min-height", width) == "var(--ui-control-height)"
        assert effective(summary, "white-space", width) in (None, "normal")
        slim = ".workspace-with-photo .workspace-row-details > summary"
        assert effective(slim, "padding-block", width) == "2px"
        # `display: flex` on a <summary> removes its native marker: the closed
        # and open state of the disclosure would have no indicator (codex, 2).
        assert effective(slim, "display", width) in (None, "list-item")
        assert effective(summary, "list-style", width) is None
        assert effective(summary, "list-style-type", width) is None

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_the_thumbnail_is_short_enough_for_a_closed_row_to_be_dense(self, width):
        """Photo + the 40px trigger under it is the floor of a row: 52px + 40px
        keeps it at 92px, the 3.75rem it replaced made it 100."""
        thumb = effective(".workspace-photo .listing-thumb", "height", width)
        if width in DESKTOP:
            assert thumb == "3.25rem"
        assert thumb in ("3.25rem", "4.5rem")

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_the_badges_are_compact_and_the_location_lines_sit_tight(self, width):
        assert (
            effective(
                ".app-workspace .workspace-property-facts .badge",
                "padding-block",
                width,
            )
            == "2px"
        )
        assert (
            effective(
                ".workspace-location-line .workspace-location-warning",
                "margin-top",
                width,
            )
            == "0"
        )
        assert effective(".workspace-municipality", "margin-top", width) == "0"

    @pytest.mark.parametrize("width", PHONE)
    def test_none_of_it_reaches_a_phone(self, width):
        badge = ".app-workspace .workspace-property-facts .badge"
        assert effective(badge, "padding-block", width) != "2px"
        slim = ".workspace-with-photo .workspace-row-details > summary"
        assert effective(slim, "padding-block", width) is None


class TestRowPaddingAndTheReasonLines:
    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_a_row_carries_six_pixels_above_and_below(self, width):
        cell = ".app-workspace .workspace-data-table > tbody > tr > td"
        assert effective(cell, "padding-block", width) == "0.375rem"
        assert effective(cell, "padding-inline", width) == "var(--ui-space-2)"

    @pytest.mark.parametrize("width", PHONE)
    def test_a_phone_cell_keeps_its_own_padding(self, width):
        cell = ".app-workspace .workspace-data-table > tbody > tr > td"
        assert effective(cell, "padding-block", width) == "0"
        assert effective(cell, "padding-inline", width) == "0"

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_the_reason_lines_have_no_gap_the_content_does_not_need(self, width):
        reading = ".workspace-data-table .recommendation-reading"
        assert effective(reading, "display", width) == "flex"
        assert effective(reading, "gap", width) == "2px"
        assert effective(f"{reading} > *", "margin-top", width) == "0"
        assert (
            effective(
                ".app-workspace .workspace-reason-cell .recommendation-reference-line .badge",
                "padding-block",
                width,
            )
            == "2px"
        )


class TestThePriceCellIsOneFlow:
    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_price_area_and_the_badges_share_the_cells_flow(self, width):
        cell = ".workspace-price-cell"
        assert effective(cell, "display", width) == "flex"
        assert effective(cell, "flex-wrap", width) == "wrap"
        # The type badges are `d-flex` (a Bootstrap utility with !important):
        # the override is the one place this stylesheet needs the same weight.
        badges = ".workspace-data-table .col-price .workspace-price-cell .properties-type-badges"
        assert effective(badges, "display", width) == "contents !important"
        assert (
            effective(
                ".workspace-data-table .col-price .workspace-price-cell .workspace-price-facts",
                "display",
                width,
            )
            == "contents"
        )

    @pytest.mark.parametrize("width", PHONE)
    def test_on_a_phone_the_wrapper_is_out_of_the_way_of_the_cells_own_grid(
        self, width
    ):
        assert effective(".workspace-price-cell", "display", width) == "contents"
        assert (
            effective(
                ".workspace-data-table .col-price .workspace-price-cell .properties-type-badges",
                "display",
                width,
            )
            is None
        )

    @pytest.mark.parametrize("mode", ["combined", "recommendation"])
    def test_every_price_cell_holds_the_price_the_area_and_the_badges_once(
        self, client, mode
    ):
        def context(items, *_a, **_k):
            reading = {
                "state": "candidate",
                "recommended": True,
                "eligibility": "eligible",
                "needs_review": False,
                "nearest_positive_reference": {
                    "id": 1,
                    "visual_matched_aspect_ids": [],
                },
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
                f"/properties?mode={mode}&view_type=list&verdict=all"
            ).get_data(as_text=True)
        rows = _rows(body)
        assert len(rows) == 2
        for _, row in rows:
            cell = dict(
                (re.search(r"col-[a-z]+", c).group(0), h) for c, h in _cells(row)
            )["col-price"]
            assert cell.strip().startswith('<div class="workspace-price-cell">')
            assert cell.count("workspace-price-cell") == 1
            assert cell.strip().endswith("</div>")
            for part in (
                "250,000€",
                "230 m²",
                "properties-type-badges",
                "housing",
                "house",
            ):
                assert part in cell, part
            # The price is first: the cell reads top to bottom as it is drawn.
            assert cell.index("250,000€") < cell.index("230 m²") < cell.index("housing")


class TestAPriceIsNeverBrokenInsideItsNumber:
    """The td carries `overflow-wrap: anywhere`; in a narrow column that split
    `295,000€` into `295,000` and `€` (1200px, measured). `nowrap` keeps the
    number whole; a 7-digit price in the narrowest column then spills into the
    cell's own padding, never into the next column."""

    @pytest.mark.parametrize("width", DESKTOP + TABLET)
    def test_the_price_does_not_wrap(self, width):
        strong = ".app-workspace .workspace-data-table .col-price strong"
        assert effective(strong, "white-space", width) == "nowrap"
        assert effective(strong, "overflow-wrap", width) == "normal"

    @pytest.mark.parametrize("width", PHONE)
    def test_a_phone_is_left_to_its_own_price_rules(self, width):
        strong = ".app-workspace .workspace-data-table .col-price strong"
        assert effective(strong, "white-space", width) is None


class TestNothingElseDeclaresWhatTheRowsRelyOn:
    """`effective()` reads one exact selector, so a later, more specific rule
    would be invisible to it. These close that hole for the properties the
    tests above lean on: the full list of rules allowed to declare them."""

    def test_only_the_known_rules_set_a_column_width(self):
        found = declaring("width", r"col-(title|price|score|actions)")
        assert sorted({selector for selector, _ in found}) == sorted(
            [f".workspace-data-table .{c}" for c in COLUMNS]
            + [f".workspace-data-table.recommendation-table .{c}" for c in COLUMNS]
        )
        assert all(value.endswith("%") for _, value in found)

    def test_only_one_rule_sets_the_padding_of_a_row_cell(self):
        found = declaring("padding-block", r"tr > td$")
        assert {selector for selector, _ in found} == {
            ".app-workspace .workspace-data-table > tbody > tr > td"
        }
        # A physical longhand would sidestep the logical ones read above.
        for longhand in ("padding-top", "padding-bottom"):
            assert declaring(longhand, r"tr > td") == []

    def test_nothing_hides_a_row_disclosure(self):
        for selector, value in declaring("display", r"workspace-row-details"):
            assert value != "none", selector

    def test_the_control_height_token_stays_at_the_target(self):
        """40px on a desktop and a tablet, 44px on a phone: the disclosure
        summary's min-height is this token, so shrinking it shrinks every
        target at once."""
        root = Path(__file__).resolve().parents[1] / "static" / "css"
        heights = [
            float(value)
            for name in ("design-tokens.css", "workspace.css")
            for value in re.findall(
                r"--ui-control-height:\s*([\d.]+)rem",
                (root / name).read_text(encoding="utf-8"),
            )
        ]
        assert heights and all(height >= 2.5 for height in heights)


class TestTheDensityRulesOfCardsAndTheDecisionCell:
    def test_the_decision_controls_share_a_line_from_the_tablet_width_up(self):
        decision = ".workspace-owner-decision"
        for width in DESKTOP + TABLET:
            assert effective(decision, "display", width) == "flex"
            assert effective(decision, "flex-wrap", width) == "wrap"

    def test_the_rating_and_the_assessment_share_a_line_without_a_column(self):
        for width in DESKTOP + TABLET:
            cell = ".workspace-reason-cell"
            assert effective(cell, "display", width) == "flex"
            assert effective(cell, "flex-wrap", width) == "wrap"
            assert effective(cell, "flex-direction", width) is None
        # A phone keeps the wrapper out of the way of its own grid.
        for width in PHONE:
            assert effective(".workspace-reason-cell", "display", width) == "contents"

    def test_a_card_keeps_price_area_and_place_on_a_line_and_travel_in_a_row(self):
        assert (
            effective(
                "#properties-cards-view .property-card-travel .properties-travel-times",
                "flex-direction",
                1440,
            )
            == "row !important"
        )
        assert (
            effective("#properties-cards-view .property-card-line", "display", 1440)
            == "flex"
        )
        assert (
            effective("#properties-cards-view .property-card-line", "flex-wrap", 1440)
            == "wrap"
        )
        assert (
            effective("#properties-cards-view .listing-card-photo", "height", 1440)
            == "7.5rem"
        )
