"""The repair half of issue #220: rewriting only what that defect damaged.

`utils/repair_prices.py` re-reads the price from a listing's stored description.
The danger of a tool like this is not that it misses a row — it is that it
rewrites one it should not have touched, because "the parser reads this
description differently now" is true of far more rows than the defect. So the
rule under test is the narrow one: rewrite only when the stored price *is* the
per-m² figure the same description states.
"""

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils.repair_prices import diagnose

BROKEN_DESCRIPTION = (
    "Hello Sergioalicante, 1 new listing that matches your search criteria "
    "99,000 € 309 €/m² 5 bed. 320 m2 CHANCE. Sale of housing under construction."
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


def _property(app, **fields):
    defaults = {
        "source_email_id": f"repair-{fields.get('price', 'x')}-{id(fields)}",
        "title": "Detached house in Barrio de Prendonés",
        "area": 320,
    }
    defaults.update(fields)
    prop = Property(**defaults)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestDiagnose:
    def test_it_repairs_a_row_whose_price_is_the_stated_unit_price(self, app):
        prop = _property(app, price=309, description=BROKEN_DESCRIPTION)

        assert diagnose(prop) == ("repair", 99000.0)

    def test_it_leaves_a_correct_row_alone(self, app):
        prop = _property(app, price=99000, description=BROKEN_DESCRIPTION)

        assert diagnose(prop) == ("already_correct", None)

    def test_it_will_not_rewrite_a_price_that_differs_for_another_reason(self, app):
        """The owner edited the price, or the listing text changed: a difference
        is not a mandate. Only the #220 shape is repaired."""
        prop = _property(
            app,
            price=85000,
            description="1 new listing 99,000 € 309 €/m² 5 bed. 320 m2",
        )

        assert diagnose(prop) == ("differs_for_another_reason", None)

    def test_it_will_not_rewrite_when_the_description_states_no_unit_price(self, app):
        prop = _property(
            app,
            price=309,
            description="A quiet plot. Asking 99,000 € for 320 m2.",
        )

        assert diagnose(prop) == ("differs_but_no_unit_price_stated", None)

    def test_a_description_with_no_price_at_all_is_not_a_repair(self, app):
        prop = _property(app, price=309, description="Ask the agent for the price.")

        assert diagnose(prop) == ("description_states_no_price", None)

    def test_an_empty_description_is_not_a_repair(self, app):
        prop = _property(app, price=309, description="")

        assert diagnose(prop) == ("no_description", None)

    def test_a_row_with_no_price_is_not_a_repair(self, app):
        prop = _property(app, price=None, description=BROKEN_DESCRIPTION)

        assert diagnose(prop) == ("no_stored_price", None)

    def test_the_grouped_unit_price_shape_is_recognised(self, app):
        """Listing 323 stored 1452.00 against '90,000 € 1,452 €/m²'."""
        prop = _property(
            app,
            price=1452,
            area=1747,
            description="1 new listing 90,000 € 1,452 €/m² 1747 m2",
        )

        assert diagnose(prop) == ("repair", 90000.0)
