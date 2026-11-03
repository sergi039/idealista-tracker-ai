"""A land listing's category must survive the SQL-to-similarity boundary.

Before the fix, land stored with area_type=built lost its parcel component:
a 200 m² candidate matched a 2000 m² reference at 100 instead of 80.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import favorite_similarity as fs
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app(testing=True)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("subtype", ["plot", "developed"])
@pytest.mark.parametrize("area_type", ["built", "unknown"])
def test_land_surface_changes_similarity_and_selection(app, subtype, area_type):
    profile = SearchProfile(name="Land comparison", is_active=True)
    db.session.add(profile)
    db.session.flush()

    def listing(name, area, favorite=False):
        return Property(
            source_email_id=f"land-area:{name}",
            title=name,
            search_profile_id=profile.id,
            property_category="land",
            property_subtype=subtype,
            area_type=area_type,
            area=area,
            price=100000,
            location_lat=43.31,
            location_lon=-8.86,
            location_accuracy="precise",
            is_favorite=favorite,
        )

    reference = listing("Reference", 2000, True)
    reference.area_type = "plot"
    small = listing("Small", 200)
    matching = listing("Matching", 2000)
    db.session.add_all([reference, small, matching])
    db.session.commit()

    context = fs.build_context([profile.id])
    reading = context.read(small.id)
    assert reading["state"] == fs.STATE_OK
    assert reading["score"] == 80.0
    assert reading["components"] == {"price": 100.0, "plot": 0.0, "geography": 100.0}
    assert context.read(matching.id)["score"] == 100.0
    ordered = Property.query.order_by(
        fs.sort_expression(Property, context).desc(), Property.id.asc()
    ).all()
    assert [row.id for row in ordered] == [reference.id, matching.id, small.id]
