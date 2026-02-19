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


def test_migration_seeds_legacy_travel_targets(app):
    with app.app_context():
        land = Land(
            source_email_id="migrate_land_legacy_travel_1",
            title="Land Travel Seed",
            municipality="Asturias",
            land_type="developed",
            idealista_property_id=999001,
            infrastructure_extended={
                "supermarket_travel_time": 2,
                "supermarket_distance": 250,
                "school_travel_time": 3,
            },
            travel_time_airport=14,
            distance_airport=22,
            travel_time_hospital=17,
        )
        db.session.add(land)
        db.session.commit()

        svc = LandToPropertyMigrationService(profile_name="Legacy Lands Travel Seed")
        result = svc.migrate(dry_run=False)
        assert result["properties_created"] == 1

        prop = Property.query.filter_by(idealista_property_id=999001).first()
        assert prop is not None
        assert isinstance(prop.travel, dict)
        targets = prop.travel.get("targets") or {}
        assert targets.get("supermarket", {}).get("duration_min") == 2
        assert targets.get("school", {}).get("duration_min") == 3
        assert targets.get("airport", {}).get("duration_min") == 14
        assert targets.get("hospital", {}).get("duration_min") == 17


def test_backfill_missing_legacy_travel_targets(app):
    with app.app_context():
        # Existing migrated-like property: legacy blob present, travel missing supermarket
        prop = Property(
            source_email_id="migrate_land_backfill_1",
            title="Legacy Backfill Property",
            municipality="Asturias",
            property_category="land",
            property_subtype="developed",
            enrichment={
                "legacy_land": {
                    "infrastructure_extended": {
                        "supermarket_travel_time": 4,
                        "school_travel_time": 6,
                    },
                    "travel_time_airport": 12,
                    "distance_airport": 18,
                }
            },
            travel={
                "targets": {
                    "airport": {"duration_min": 12, "distance_km": 18.0},
                }
            },
        )
        db.session.add(prop)
        db.session.commit()

        svc = LandToPropertyMigrationService(profile_name="Legacy Lands Backfill")
        result = svc.backfill_missing_legacy_travel()
        assert result["updated"] >= 1

        refreshed = db.session.get(Property, prop.id)
        assert refreshed is not None
        targets = (refreshed.travel or {}).get("targets") or {}
        assert targets.get("supermarket", {}).get("duration_min") == 4
        assert targets.get("school", {}).get("duration_min") == 6
        # Existing calculated airport data should remain untouched
        assert targets.get("airport", {}).get("duration_min") == 12
