"""On a narrow screen a search's inflow is one compact card (owner, 2026-11-16).

Measured on production at 375px: with every period stacked on its own line,
twenty searches took 5266px -- six phone screens -- and the count's link was a
9x17px target. The fix is in the stylesheet alone: the row becomes a grid with
the name across the top and the periods under it (two by two on a phone, four
in a line up to the table's own breakpoint), and the count's link covers its
whole cell.

The layout is proven by the browser measurement in the PR
(docs/rules/testing.md). What a test can pin is what that measurement stood
on: the rules as the cascade leaves them at each width (read with the resolver
`tests/test_row_density.py` already owns), and the two things in the markup
the rules assume -- the name is the row's first cell, and a cell holds at most
one link for the stretched target to belong to.
"""

import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from tests.test_row_density import effective

PHONE = (575, 390, 320)
NARROW_TABLE = (991, 768, 576)
WIDE = (992, 1440)


class TestTheRowIsAGridUnderTheTablesBreakpoint:
    @pytest.mark.parametrize("width", PHONE)
    def test_a_phone_puts_the_periods_two_by_two(self, width):
        assert effective(".inflow-table tr", "display", width) == "grid"
        assert (
            effective(".inflow-table tr", "grid-template-columns", width)
            == "repeat(2, minmax(0, 1fr))"
        )

    @pytest.mark.parametrize("width", NARROW_TABLE)
    def test_a_wider_narrow_screen_puts_the_four_in_a_line(self, width):
        assert effective(".inflow-table tr", "display", width) == "grid"
        assert (
            effective(".inflow-table tr", "grid-template-columns", width)
            == "repeat(4, minmax(0, 1fr))"
        )

    @pytest.mark.parametrize("width", PHONE + NARROW_TABLE)
    def test_the_name_spans_the_card_and_drops_its_caption(self, width):
        assert effective(".inflow-table td:first-child", "grid-column", width) == (
            "1 / -1"
        )
        assert (
            effective(".inflow-table td:first-child::before", "content", width)
            == "none"
        )

    @pytest.mark.parametrize("width", PHONE + NARROW_TABLE)
    def test_a_grid_cell_is_not_forced_to_the_cards_width(self, width):
        """The shared management-table rule gives every cell `width: 100%`;
        left in force it would make each period as wide as the card."""
        assert effective(".inflow-table td", "width", width) == "auto"

    @pytest.mark.parametrize("width", WIDE)
    def test_none_of_it_reaches_the_table_that_is_drawn_as_a_table(self, width):
        assert effective(".inflow-table tr", "display", width) is None
        assert effective(".inflow-table tr", "grid-template-columns", width) is None
        assert effective(".inflow-table td > a::after", "position", width) is None


class TestTheCountsLinkCoversItsCell:
    @pytest.mark.parametrize("width", PHONE + NARROW_TABLE)
    def test_the_link_is_stretched_over_a_positioned_cell(self, width):
        assert effective(".inflow-table td", "position", width) == "relative"
        assert effective(".inflow-table td > a::after", "position", width) == (
            "absolute"
        )
        assert effective(".inflow-table td > a::after", "inset", width) == "0"
        assert effective(".inflow-table td > a::after", "content", width) == '""'

    @pytest.mark.parametrize("width", PHONE + NARROW_TABLE)
    def test_a_period_cell_is_as_tall_as_a_control(self, width):
        assert (
            effective(".inflow-table td", "min-height", width)
            == "var(--ui-control-height)"
        )


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Galicia costa",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        db.session.add(
            Property(
                source_email_id="inflow_phone_1",
                title="Casa en Muxia",
                property_category="housing",
                price=150000,
                search_profile_id=profile.id,
                listing_status="active",
                is_favorite=True,
            )
        )
        db.session.commit()
        yield app.test_client()
        db.drop_all()


class TestTheMarkupTheRulesAssume:
    def _rows(self, client):
        body = client.get("/profiles?view=technical").get_data(as_text=True)
        section = re.search(
            r'<section[^>]*id="subscription-inflow".*?</section>', body, re.DOTALL
        )
        assert section, "the page drew no inflow section"
        rows = re.findall(
            r"<tr data-inflow-profile=.*?</tr>", section.group(0), re.DOTALL
        )
        assert rows, "the inflow table drew no rows"
        return [re.findall(r"<td\b.*?</td>", row, re.DOTALL) for row in rows]

    def test_the_name_is_the_first_cell_and_the_four_periods_follow(self, client):
        for cells in self._rows(client):
            assert len(cells) == 5
            assert "<strong>" in cells[0]
            assert all("<strong>" not in cell for cell in cells[1:])

    def test_a_cell_holds_at_most_one_link_as_a_direct_child(self, client):
        """`td > a::after` stretches every link that is a direct child; two in
        one cell would lie on top of each other and only the last would open."""
        linked = 0
        for cells in self._rows(client):
            assert "<a " not in cells[0]
            for cell in cells[1:]:
                assert cell.count("<a ") <= 1
                linked += cell.count("<a ")
        assert linked, "no period cell carried a link, so nothing was checked"
