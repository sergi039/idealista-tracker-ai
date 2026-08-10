"""A recorded price change belongs on the page that shows the price.

`/lands/50` read "Land in Ania … 40,000 €" in the title and €35,000 in the Price
tile, which looks like a contradiction and is not one: the listing arrived by
email at 40,000 and a price-drop email twelve minutes later lowered it. The row
recorded all of it -- `previous_price`, `price_change_amount`,
`price_change_percentage`, `price_changed_date` -- and the list at /properties
has been rendering it all along. Neither detail page mentioned any of it, for
any of the 51 properties and 5 lands that carry one (counted 2026-08-10).

The second half of the same defect: `static/js/main.js` printed
`enhanced_description.price_info.current_price` as "Current price: €40,000".
That number is whatever the price was when the AI description was generated, and
nothing updates it when the price moves. Every row holding both a price change
and that blob disagreed with its own row -- 5 lands and 5 properties, no
exceptions. The price lives on the row; a stale copy of it has nothing to add.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import create_app, db
from models import Land, Property
from tests import setup_test_environment

MAIN_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "main.js"


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


def _price_fields(drop=True):
    """The shape a price-drop email leaves behind, as imap_service writes it."""
    if drop:
        return {
            "price": Decimal("35000.00"),
            "previous_price": Decimal("40000.00"),
            "price_change_amount": Decimal("-5000.00"),
            "price_change_percentage": Decimal("-12.50"),
            "price_changed_date": datetime(2026, 2, 18, 20, 51, 46),
        }
    return {
        "price": Decimal("45000.00"),
        "previous_price": Decimal("40000.00"),
        "price_change_amount": Decimal("5000.00"),
        "price_change_percentage": Decimal("12.50"),
        "price_changed_date": datetime(2026, 2, 18, 20, 51, 46),
    }


def make_property(key, **overrides):
    fields = {
        "source_email_id": f"price-{key}",
        "title": "Land in Ania, n/a, Las Regueras 40,000 €",
        "municipality": "Las Regueras",
        "area": Decimal("970.00"),
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def make_land(key, **overrides):
    fields = {
        "source_email_id": f"price-land-{key}",
        "title": "Land in Ania, n/a, Las Regueras 40,000 €",
        "municipality": "Las Regueras",
        "land_type": "developed",
        "area": Decimal("970.00"),
    }
    fields.update(overrides)
    land = Land(**fields)
    db.session.add(land)
    db.session.commit()
    return land


class TestThePropertyPageShowsTheChange:
    def test_a_drop_reads_as_a_drop(self, app, client):
        prop = make_property("drop", **_price_fields())

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "€35,000" in body, "the current price"
        assert "-€5,000" in body
        assert "(-12.5%)" in body
        assert "was €40,000" in body
        assert "2026-02-18" in body

    def test_a_rise_reads_as_a_rise(self, app, client):
        prop = make_property("rise", **_price_fields(drop=False))

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "+€5,000" in body
        assert "(+12.5%)" in body
        assert "was €40,000" in body

    def test_an_unchanged_price_says_nothing_extra(self, app, client):
        """Most rows have no change, and they must not grow an empty widget."""
        prop = make_property("flat", price=Decimal("35000.00"))

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "€35,000" in body
        assert "price-change-card" not in body
        assert "was €" not in body


class TestTheLandPageShowsTheChange:
    def test_a_drop_reads_as_a_drop(self, app, client):
        land = make_land("drop", **_price_fields())

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)

        assert "35,000€" in body
        assert "-5,000€" in body
        assert "(-12.5%)" in body
        assert "was 40,000€" in body
        assert "2026-02-18" in body

    def test_an_unchanged_price_says_nothing_extra(self, app, client):
        land = make_land("flat", price=Decimal("35000.00"))

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)

        assert "price-change-card" not in body


class TestTheStaleAiPriceIsGone:
    """It was the only thing on the page claiming to be the *current* price
    without reading the row."""

    def test_the_renderer_is_gone_from_main_js(self):
        """Matched on code, not prose: the comment left in its place names what
        was removed, so a plain search for "Current price" finds the epitaph."""
        source = MAIN_JS.read_text(encoding="utf-8")

        assert "priceInfo.current_price" not in source
        assert "priceInfo.original_price" not in source
        assert "getElementById('price-info-text')" not in source
        assert "getElementById('price-info-section')" not in source

    def test_the_markup_it_filled_is_gone_too(self, app, client):
        """A hidden div nothing writes to is how a stale field comes back."""
        land = make_land("ai", **_price_fields())

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)

        assert 'id="price-info-section"' not in body
        assert 'id="price-info-text"' not in body

    def test_the_rest_of_the_enhanced_description_still_renders(self):
        """Only the price line went: the AI text and its highlights stay."""
        source = MAIN_JS.read_text(encoding="utf-8")

        assert "enhanced-description-text" in source
        assert "highlights-list" in source
