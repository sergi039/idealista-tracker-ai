from decimal import Decimal

import pytest

from app import create_app, db
from models import Land, Property
from services.land_to_property_migration_service import LandToPropertyMigrationService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_land_to_property_migration_service_dry_run(app):
    with app.app_context():
        land = Land(
            source_email_id="migrate_land_1",
            title="Land A",
            municipality="Madrid",
            land_type="developed",
            price=Decimal("100000.00"),
            area=Decimal("1000.00"),
            idealista_property_id=123,
        )
        db.session.add(land)
        db.session.commit()

        svc = LandToPropertyMigrationService(profile_name="Legacy Lands Test")
        result = svc.migrate(dry_run=True)
        assert result["dry_run"] is True
        assert result["lands_considered"] == 1
        assert result["properties_created"] == 1
        assert Property.query.count() == 0


def test_land_to_property_migration_service_writes_and_dedups(app):
    with app.app_context():
        land = Land(
            source_email_id="migrate_land_2",
            title="Land B",
            municipality="Madrid",
            land_type="buildable",
            price=Decimal("150000.00"),
            area=Decimal("1200.00"),
            idealista_property_id=456,
            url="https://www.idealista.com/inmueble/456/",
        )
        db.session.add(land)
        db.session.commit()

        svc = LandToPropertyMigrationService(profile_name="Legacy Lands Test 2")
        result = svc.migrate(dry_run=False)
        assert result["dry_run"] is False
        assert result["properties_created"] == 1
        assert Property.query.count() == 1

        # Second run should skip existing (dedup by idealista_property_id / url)
        result2 = svc.migrate(dry_run=False)
        assert result2["properties_created"] == 0
        assert result2["skipped_existing"] == 1
        assert Property.query.count() == 1


def test_land_to_property_migration_endpoint(client, app):
    with app.app_context():
        land = Land(
            source_email_id="migrate_land_3",
            title="Land C",
            municipality="Valencia",
            land_type="developed",
            idealista_property_id=789,
        )
        db.session.add(land)
        db.session.commit()

    resp = client.post("/api/migrate/lands-to-properties", json={"dry_run": False, "limit": 10, "profile_name": "Legacy Lands API"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["result"]["dry_run"] is False
    assert data["result"]["properties_created"] == 1

