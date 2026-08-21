"""The subscription chips are one thin strip, not a wall of buttons.

Production carries eleven live subscriptions, and each chip used to size
itself to its own saved-search name: a Bootstrap button wraps its label, so
the long names became two-line buttons of eleven different widths and the
toolbar around them fell apart (owner report, 2026-08-21). The rules in
`static/css/style.css` give every chip the same fixed size, truncate the
name inside it, and let the strip scroll sideways inside itself instead of
wrapping.

A headless test cannot lay out the strip, so -- like
`test_tablet_list_layout.py` -- what is pinned is that the two files still
meet: the template renders every chip label through the span the ellipsis
rule targets (the "All subscriptions" chip used to carry bare text, which no
truncation rule could reach), and the stylesheet still carries the strip's
geometry. Delete either half and the other stops working silently.
"""

from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

STYLESHEET = Path(__file__).resolve().parents[1] / "static" / "css" / "style.css"


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
def two_subscriptions(app):
    """Two live subscriptions, one with a name long enough to truncate."""
    with app.app_context():
        short = SearchProfile(
            name="Asturias",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        long = SearchProfile(
            name="houses at your custom search area norte",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([short, long])
        db.session.commit()
        for index, profile in enumerate((short, long)):
            db.session.add(
                Property(
                    source_email_id=f"chips_layout_{index}",
                    title=f"ChipsLayoutListing{index}",
                    municipality="Cudillero",
                    search_profile_id=profile.id,
                    listing_status="active",
                    property_category="land",
                    property_subtype="plot",
                    price=60000,
                    area=1300,
                )
            )
        db.session.commit()


def _chips_markup(body):
    """The chips button group, markup only. The group holds nothing but
    `<a>` chips, so its own `</div>` is the first one after it opens."""
    start = body.index("properties-subscription-chips")
    return body[start : body.index("</div>", start)]


def _rule_block(css, selector):
    """The body of one CSS rule, from its selector to the matching brace."""
    start = css.index(selector)
    open_brace = css.index("{", start)
    depth = 0
    for index in range(open_brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces after {selector!r}")


class TestEveryChipLabelIsTruncatable:
    def test_each_chip_wraps_its_label_in_the_name_span(
        self, client, two_subscriptions
    ):
        """One span per chip -- including "All subscriptions", which used to
        be bare text that no ellipsis rule could reach."""
        chips = _chips_markup(client.get("/properties").get_data(as_text=True))
        chip_count = chips.count("<a ")
        assert chip_count >= 3, "expected the All chip plus two subscriptions"
        assert chips.count("properties-subscription-name") == chip_count

    def test_no_inline_widths_on_the_chips(self, client, two_subscriptions):
        """An inline width would outrank the equal-size rule, the same trap
        `test_tablet_list_layout.py` pins for the table columns."""
        chips = _chips_markup(client.get("/properties").get_data(as_text=True))
        assert "style=" not in chips


class TestTheStripGeometryStaysInCss:
    def test_the_strip_scrolls_inside_itself(self):
        css = STYLESHEET.read_text(encoding="utf-8")
        block = _rule_block(css, ".properties-subscription-chips {")
        assert "overflow-x: auto" in block, "the strip no longer scrolls sideways"
        assert "flex-wrap: nowrap" in block, "the chips are free to wrap again"
        assert "min-width: 0" in block, (
            "without min-width: 0 a flex item refuses to shrink, and the strip "
            "pushes the toolbar apart instead of scrolling"
        )

    def test_every_chip_is_the_same_fixed_size(self):
        css = STYLESHEET.read_text(encoding="utf-8")
        block = _rule_block(css, ".properties-subscription-chips .btn {")
        assert "flex: 0 0 9rem" in block, "chips size themselves to their names again"
        assert "white-space: nowrap" in block, (
            "a wrapping label makes chips of different heights"
        )

    def test_the_name_span_still_truncates(self):
        css = STYLESHEET.read_text(encoding="utf-8")
        block = _rule_block(css, ".properties-subscription-name {")
        assert "text-overflow: ellipsis" in block
        assert "overflow: hidden" in block
