from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_scoring_service import PropertyScoringService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _profile_with_travel_disabled():
    # Disable presets to keep scoring stable (no travel required).
    presets = {
        "airport": {"enabled": False, "mode": "driving"},
        "train_station": {"enabled": False, "mode": "driving"},
        "hospital": {"enabled": False, "mode": "driving"},
        "police": {"enabled": False, "mode": "driving"},
        "supermarket": {"enabled": False, "mode": "driving"},
        "school": {"enabled": False, "mode": "driving"},
        "subway_station": {"enabled": False, "mode": "driving"},
    }
    return SearchProfile(
        name="Test Profile",
        is_active=True,
        is_default=True,
        travel_targets={"presets": presets, "custom": []},
    )


def test_property_scoring_housing_uses_value_percentile(app):
    with app.app_context():
        profile = _profile_with_travel_disabled()
        db.session.add(profile)
        db.session.commit()

        peers = [
            Property(
                source_email_id="peer_h1",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype="apartment",
                municipality="Madrid",
                price=Decimal("300000"),
                area=Decimal("100"),
            ),
            Property(
                source_email_id="peer_h2",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype="apartment",
                municipality="Madrid",
                price=Decimal("250000"),
                area=Decimal("90"),
            ),
            Property(
                source_email_id="peer_h3",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype="apartment",
                municipality="Madrid",
                price=Decimal("400000"),
                area=Decimal("120"),
            ),
        ]
        db.session.add_all(peers)

        target = Property(
            source_email_id="target_h",
            search_profile_id=profile.id,
            property_category="housing",
            property_subtype="apartment",
            municipality="Madrid",
            price=Decimal("280000"),
            area=Decimal("100"),
        )
        db.session.add(target)
        db.session.commit()

        svc = PropertyScoringService()
        assert svc.calculate_for_property(target, commit=True) is True

        refreshed = db.session.get(Property, target.id)
        assert refreshed is not None
        assert refreshed.score_total is not None
        assert refreshed.scoring["category"] == "housing"
        assert (
            refreshed.scoring["profiles"]["investment"]["components"]["value_score"]
            is not None
        )


def test_property_scoring_land_uses_value_percentile(app):
    with app.app_context():
        profile = _profile_with_travel_disabled()
        profile.name = "Test Profile 2"
        db.session.add(profile)
        db.session.commit()

        peers = [
            Property(
                source_email_id="peer_l1",
                search_profile_id=profile.id,
                property_category="land",
                property_subtype="plot",
                municipality="Valencia",
                price=Decimal("80000"),
                area=Decimal("1000"),
            ),
            Property(
                source_email_id="peer_l2",
                search_profile_id=profile.id,
                property_category="land",
                property_subtype="plot",
                municipality="Valencia",
                price=Decimal("120000"),
                area=Decimal("1200"),
            ),
            Property(
                source_email_id="peer_l3",
                search_profile_id=profile.id,
                property_category="land",
                property_subtype="plot",
                municipality="Valencia",
                price=Decimal("95000"),
                area=Decimal("900"),
            ),
        ]
        db.session.add_all(peers)

        target = Property(
            source_email_id="target_l",
            search_profile_id=profile.id,
            property_category="land",
            property_subtype="plot",
            municipality="Valencia",
            price=Decimal("100000"),
            area=Decimal("1000"),
        )
        db.session.add(target)
        db.session.commit()

        svc = PropertyScoringService()
        assert svc.calculate_for_property(target, commit=True) is True

        refreshed = db.session.get(Property, target.id)
        assert refreshed is not None
        assert refreshed.score_total is not None
        assert refreshed.scoring["category"] == "land"
        assert (
            refreshed.scoring["profiles"]["investment"]["components"]["value_score"]
            is not None
        )
