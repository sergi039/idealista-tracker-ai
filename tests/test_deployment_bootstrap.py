from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app import create_app, db
from tests import setup_test_environment


@pytest.fixture
def health_app(monkeypatch):
    setup_test_environment()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AUTO_CREATE_DB", "false")
    monkeypatch.setenv("AUTO_START_SCHEDULER", "false")

    app = create_app(testing=True)
    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_healthz_fails_when_schema_is_missing(health_app):
    response = health_app.test_client().get("/api/healthz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["database"] == "ok"
    assert response.get_json()["checks"]["schema"] == "missing"


def test_healthz_fails_when_scheduler_is_not_running(health_app, monkeypatch):
    with health_app.app_context():
        db.create_all()

    monkeypatch.setattr(
        "services.health_service.get_schema_status",
        lambda _engine, _metadata: {"status": "ok"},
    )
    monkeypatch.setattr(
        "services.scheduler_service.get_scheduler_status",
        lambda: {"status": "stopped"},
    )

    response = health_app.test_client().get("/api/healthz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["schema"] == "ok"
    assert response.get_json()["checks"]["scheduler"] == "stopped"


def test_healthz_is_green_only_when_schema_and_scheduler_are_healthy(
    health_app, monkeypatch
):
    with health_app.app_context():
        db.create_all()

    monkeypatch.setattr(
        "services.health_service.get_schema_status",
        lambda _engine, _metadata: {"status": "ok"},
    )
    monkeypatch.setattr(
        "services.scheduler_service.get_scheduler_status",
        lambda: {"status": "running", "jobs": [{"id": "morning_ingestion"}]},
    )

    response = health_app.test_client().get("/api/healthz")

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "checks": {"database": "ok", "schema": "ok", "scheduler": "running"},
    }


def _write_migration(directory: Path, filename: str, sql: str) -> None:
    (directory / filename).write_text(sql, encoding="utf-8")


def test_migration_runner_applies_each_file_once_and_reports_current(tmp_path):
    from migrations.runner import get_schema_status, run_migrations

    _write_migration(
        tmp_path,
        "000_create_schema_migrations.sql",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(3) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
    )
    _write_migration(
        tmp_path,
        "001_create_widgets.sql",
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL);",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")

    first_run = run_migrations(engine, migrations_dir=tmp_path)
    second_run = run_migrations(engine, migrations_dir=tmp_path)

    assert first_run == ["000_create_schema_migrations", "001_create_widgets"]
    assert second_run == []
    assert inspect(engine).has_table("widgets")
    assert get_schema_status(engine, migrations_dir=tmp_path) == {"status": "ok"}


def test_migration_runner_fails_on_changed_applied_migration(tmp_path):
    from migrations.runner import MigrationError, run_migrations

    _write_migration(
        tmp_path,
        "000_create_schema_migrations.sql",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(3) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'checksum.db'}")
    run_migrations(engine, migrations_dir=tmp_path)
    _write_migration(
        tmp_path,
        "000_create_schema_migrations.sql",
        "SELECT 1;",
    )

    with pytest.raises(MigrationError, match="checksum"):
        run_migrations(engine, migrations_dir=tmp_path)


def test_repository_migrations_are_uniquely_numbered():
    from migrations.runner import discover_migrations

    root = Path(__file__).parent.parent
    migrations = discover_migrations(root / "migrations")

    assert migrations[0].version == "000"
    assert len({migration.version for migration in migrations}) == len(migrations)
    assert all(
        migration.path.name[0:3].isdigit() and migration.path.name[3] == "_"
        for migration in migrations
    )
