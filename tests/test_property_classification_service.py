import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_classification_service import PropertyClassificationService
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


def test_property_classification_service_applies_global_defaults(app):
    with app.app_context():
        prop = Property(
            source_email_id="cls_1",
            title="Apartment in Centro, Madrid",
            email_subject="New listing",
            description="Great apartment with local amenities",
            area=80,
            area_type="unknown",
        )
        db.session.add(prop)
        db.session.commit()

        changed = PropertyClassificationService.apply_classification(prop, profile=None)
        assert changed is True
        assert prop.property_category == "housing"
        assert prop.property_subtype == "apartment"
        assert prop.area_type == "built"


def test_property_classification_service_respects_profile_override(app):
    with app.app_context():
        profile = SearchProfile(
            name="Override",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
            classification_rules=[
                {
                    "category": "commercial",
                    "subtype": "office",
                    "pattern": r"Apartment",
                    "priority": 999,
                }
            ],
        )
        db.session.add(profile)
        db.session.commit()

        prop = Property(
            source_email_id="cls_2",
            title="Apartment in Centro, Madrid",
            search_profile_id=profile.id,
            area=40,
            area_type="unknown",
        )
        db.session.add(prop)
        db.session.commit()

        changed = PropertyClassificationService.apply_classification(
            prop, profile=profile
        )
        assert changed is True
        assert prop.property_category == "commercial"
        assert prop.property_subtype == "office"
        assert prop.area_type == "built"


def test_property_classification_service_does_not_clear_without_match(app):
    with app.app_context():
        prop = Property(
            source_email_id="cls_3",
            title="Random listing title with no hints",
            property_category="land",
            property_subtype="plot",
            area_type="plot",
        )
        db.session.add(prop)
        db.session.commit()

        changed = PropertyClassificationService.apply_classification(prop, profile=None)
        assert changed is False
        assert prop.property_category == "land"
        assert prop.property_subtype == "plot"
        assert prop.area_type == "plot"


def test_property_classification_service_skips_locked_properties(app):
    with app.app_context():
        prop = Property(
            source_email_id="cls_4",
            title="Apartment in Centro, Madrid",
            area=55,
            area_type="unknown",
            attributes={"classification_locked": True},
        )
        db.session.add(prop)
        db.session.commit()

        changed = PropertyClassificationService.apply_classification(prop, profile=None)
        assert changed is False
        assert prop.property_category is None
        assert prop.property_subtype is None
        assert prop.area_type == "unknown"


@pytest.mark.parametrize(
    ("title", "expected_category", "expected_subtype"),
    [
        ("Villa in Marbella, Málaga", "housing", "house"),
        ("Plaza de garaje en venta en Centro, Madrid", "garage", "garage"),
        ("Local comercial en venta en Alicante", "commercial", "retail"),
        ("Commercial premises in Valencia", "commercial", "retail"),
        ("Edificio en venta en Barcelona", "building", "building"),
        ("Obra nueva en Madrid", "new_development", "obra_nueva"),
        ("Finca rústica en venta en Granada", "land", "plot"),
        ("Solar urbano en venta en Sevilla", "land", "plot"),
        ("Suelo urbanizable en venta en Cádiz", "land", "plot"),
    ],
)
def test_property_classification_service_covers_common_sale_types(
    app, title, expected_category, expected_subtype
):
    with app.app_context():
        prop = Property(
            source_email_id="cls_types",
            title=title,
            area=50,
            area_type="unknown",
        )
        db.session.add(prop)
        db.session.commit()

        changed = PropertyClassificationService.apply_classification(prop, profile=None)
        assert changed is True
        assert prop.property_category == expected_category
        assert prop.property_subtype == expected_subtype
