"""The listing table has to fit a tablet, and the table is the default view.

Two separate promises are pinned here.

*The default view.* A bare `/properties` used to open on the cards. The owner
works from the table -- price, area, travel and date side by side -- so that is
what the page opens on now, and what the unknown-`view_type` fallback lands on.

*The layout the media queries need.* The fix for the sideways scroll on an iPad
lives in `static/css/style.css`: below the xxl breakpoint the container drops
Bootstrap's 720/960px cap, and between 768px and 1200px the table gives up its
fixed pixel columns. None of that can work while the template carries
`style="min-width: 120px !important"` on the cells -- an inline `!important`
outranks every stylesheet rule, so the media queries would silently do nothing.
The column widths therefore live in CSS classes, and these tests fail if they
migrate back into the markup.

Widths themselves are not asserted: a headless test cannot lay out a table.
What it *can* guarantee is that the two files still meet -- every `col-*` class
the template renders is styled, and no inline width overrides it.
"""

import re
from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

STYLESHEET = Path(__file__).resolve().parents[1] / "static" / "css" / "style.css"

# Every column the list table renders, in template order.
COLUMN_CLASSES = (
    "col-score",
    "col-fav",
    "col-title",
    "col-price",
    "col-area",
    "col-coords",
    "col-travel",
    "col-inv",
    "col-type",
    "col-added",
    "col-actions",
)


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
def listing(app):
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        db.session.add(
            Property(
                source_email_id="tablet_layout",
                title="TabletLayoutUniqueTitle",
                municipality="Cudillero",
                search_profile_id=profile.id,
                listing_status="active",
                property_category="land",
                property_subtype="plot",
                price=60000,
                area=1300,
                location_lat=43.5723,
                location_lon=-6.2123,
                score_total=47.3,
            )
        )
        db.session.commit()


def _list_table(body):
    """The `<table>` element of the list view, markup only."""
    start = body.index('<table class="table table-hover mb-0 lands-table')
    return body[start : body.index("</table>", start)]


class TestDefaultView:
    def test_bare_properties_opens_on_the_table(self, client, listing):
        body = client.get("/properties").get_data(as_text=True)
        assert 'id="properties-list-view"' in body
        assert 'id="properties-cards-view"' not in body

    def test_unknown_view_type_falls_back_to_the_table(self, client, listing):
        body = client.get("/properties?view_type=bogus").get_data(as_text=True)
        assert 'id="properties-list-view"' in body

    def test_cards_are_still_reachable(self, client, listing):
        body = client.get("/properties?view_type=cards").get_data(as_text=True)
        assert 'id="properties-cards-view"' in body
        assert "TabletLayoutUniqueTitle" in body


class TestColumnWidthsStayInCss:
    def test_every_column_carries_its_class(self, client, listing):
        table = _list_table(client.get("/properties").get_data(as_text=True))
        for column_class in COLUMN_CLASSES:
            assert f'class="{column_class}"' in table or f" {column_class}" in table, (
                f"{column_class} is missing from the list table"
            )

    def test_no_inline_widths_in_the_table(self, client, listing):
        """An inline min-width/width would outrank the tablet media queries."""
        table = _list_table(client.get("/properties").get_data(as_text=True))
        offenders = re.findall(r'style="[^"]*\b(?:min-width|width)\s*:[^"]*"', table)
        assert not offenders, f"inline widths back in the list table: {offenders}"

    def test_no_inline_nowrap_in_the_table(self, client, listing):
        """Same reason: the narrow layout has to be free to wrap a cell."""
        table = _list_table(client.get("/properties").get_data(as_text=True))
        offenders = re.findall(r'style="[^"]*white-space\s*:[^"]*"', table)
        assert not offenders, f"inline white-space back in the list table: {offenders}"

    def test_stylesheet_styles_every_column_class(self):
        """A class the template renders with no rule behind it is a width the
        media queries cannot reach -- the inline styles in disguise."""
        css = STYLESHEET.read_text(encoding="utf-8")
        # Two columns own no geometry of their own: the title is sized through
        # .lands-title-col, and the type column through its badges.
        styled_elsewhere = {
            "col-title": ".lands-title-col",
            "col-type": ".properties-type-badges",
        }
        for column_class in COLUMN_CLASSES:
            selector = styled_elsewhere.get(column_class, f".{column_class}")
            assert selector in css, f"{column_class} has no CSS rule ({selector})"

    def test_container_is_uncapped_below_the_desktop_breakpoint(self):
        """The 720/960px cap is what left an iPad scrolling sideways."""
        css = STYLESHEET.read_text(encoding="utf-8")
        assert "@media (min-width: 768px) and (max-width: 1399.98px)" in css
        assert "main.container" in css

    def test_the_phone_breakpoint_stops_below_the_tablet_one(self):
        """At exactly 768px both used to apply: stacked *and* compacted."""
        css = STYLESHEET.read_text(encoding="utf-8")
        assert "@media (max-width: 767.98px)" in css
        assert "@media (max-width: 768px)" not in css
