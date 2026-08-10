import re
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text

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


def _add_operator_drift(engine):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE lands ADD COLUMN operator_note TEXT"))
        connection.execute(text("CREATE TABLE operator_audit (id INTEGER)"))


# `search_profiles` as migrations 008 + 009 left it, stated rather than
# derived. The frozen fingerprint in migrations/runner.py describes the
# *pre-ledger* schema, so create_all() stopped producing it the moment
# migration 013 added columns. Deriving it by dropping those columns back off
# is no longer possible either: SQLite refuses to drop a column that a CHECK
# constraint mentions, and 013 adds one.
_HISTORICAL_SEARCH_PROFILES = """
CREATE TABLE search_profiles (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    description TEXT,
    is_active BOOLEAN,
    is_default BOOLEAN,
    email_matchers JSON,
    classification_rules JSON,
    travel_targets JSON,
    ui_config JSON,
    scoring_config JSON,
    ai_config JSON,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""
_HISTORICAL_SEARCH_PROFILE_INDEXES = (
    ("ix_search_profiles_name", "name"),
    ("ix_search_profiles_is_active", "is_active"),
    ("ix_search_profiles_is_default", "is_default"),
)


def _create_historical_schema(engine):
    """Build the schema as it stood at migration 012.

    The runner's fingerprint check is deliberately strict, so this must be the
    real historical shape - anything left over from a later migration is drift
    and would (correctly) make the runner refuse to baseline.
    """
    db.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE search_profiles"))
        connection.execute(text(_HISTORICAL_SEARCH_PROFILES))
        for index, column in _HISTORICAL_SEARCH_PROFILE_INDEXES:
            connection.execute(
                text(f"CREATE INDEX {index} ON search_profiles ({column})")  # noqa: S608
            )
        # Migration 015 added listing_status_source to both listing tables, so
        # create_all() produces a column the pre-ledger schema never had. Unlike
        # 013's additions this one can simply be dropped: the CHECK constraint
        # naming it lives in the PostgreSQL migration, not in the model, so
        # SQLite has nothing to refuse.
        for table in ("properties", "lands"):
            connection.execute(
                text(f"ALTER TABLE {table} DROP COLUMN listing_status_source")  # noqa: S608
            )
        # Migration 016 (issue #176) added background_jobs, a genuinely new
        # table with no pre-ledger equivalent -- unlike a new column on an
        # existing table, create_all() has nothing to trim it down from, so
        # drop it outright.
        connection.execute(text("DROP TABLE background_jobs"))


def test_the_stated_historical_table_matches_the_runners_fingerprint(tmp_path):
    """The hand-written table above must stay in step with the fingerprint.

    If they drift, every baseline test below would exercise a schema the runner
    never actually sees, and would keep passing while doing so.
    """
    from migrations.runner import HISTORICAL_SCHEMA_FINGERPRINT

    engine = create_engine(f"sqlite:///{tmp_path / 'fingerprint.db'}")
    _create_historical_schema(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("search_profiles")
    }

    assert columns == set(HISTORICAL_SCHEMA_FINGERPRINT["search_profiles"])
    engine.dispose()


def _baseline_migrations_dir(tmp_path: Path) -> Path:
    """A copy of the repository's migrations, truncated to the baseline set.

    The repository's migration SQL is PostgreSQL-only (and multi-statement,
    which pysqlite refuses outright), so these SQLite tests can only ever
    cover migrations that are *never executed* - the 000-012 baseline, which
    is recorded as metadata. Migration SQL after 012 is exercised for real
    against PostgreSQL in tests/test_postgres_migrations.py.
    """
    from migrations.runner import BASELINE_IDENTIFIERS, MIGRATIONS_DIR

    directory = tmp_path / "baseline-migrations"
    directory.mkdir()
    for identifier in BASELINE_IDENTIFIERS:
        name = f"{identifier}.sql"
        shutil.copy(MIGRATIONS_DIR / name, directory / name)
    return directory


def test_the_baseline_set_is_every_migration_the_sqlite_tests_can_cover():
    """Pins why the tests below use a truncated migrations directory.

    If a later migration is added to the baseline identifiers (it must not be
    - the set is frozen history), or the copy helper silently misses a file,
    these tests would quietly stop covering what they claim to.
    """
    from migrations.runner import (
        BASELINE_IDENTIFIERS,
        MIGRATIONS_DIR,
        discover_migrations,
    )

    all_migrations = discover_migrations(MIGRATIONS_DIR)
    baselined = [m for m in all_migrations if m.identifier in BASELINE_IDENTIFIERS]

    assert [m.identifier for m in baselined] == list(BASELINE_IDENTIFIERS)
    assert [m.identifier for m in all_migrations[: len(BASELINE_IDENTIFIERS)]] == list(
        BASELINE_IDENTIFIERS
    ), "the baseline must stay a prefix of migration history"


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


def test_populated_historical_schema_is_baselined_without_replay(tmp_path):
    from migrations.runner import (
        BASELINE_IDENTIFIERS,
        discover_migrations,
        run_migrations,
    )

    migrations_dir = _baseline_migrations_dir(tmp_path)
    database_path = tmp_path / "historical.db"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    _create_historical_schema(engine)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lands "
                "(source_email_id, url, idealista_property_id, title) "
                "VALUES (:source_email_id, :url, NULL, :title)"
            ),
            {
                "source_email_id": "historical-land",
                "url": "https://www.idealista.com/inmueble/12345678/",
                "title": "Keep this row unchanged",
            },
        )
        connection.execute(
            text(
                "INSERT INTO sync_history "
                "(sync_type, backend, price_updated_count, expired_count) "
                "VALUES ('full', 'imap', NULL, NULL)"
            )
        )
        connection.execute(
            text("CREATE INDEX idx_lands_is_favorite ON lands (is_favorite)")
        )

    with engine.connect() as connection:
        land_before = dict(
            connection.execute(
                text("SELECT * FROM lands WHERE source_email_id = 'historical-land'")
            )
            .mappings()
            .one()
        )
        sync_before = dict(
            connection.execute(text("SELECT * FROM sync_history")).mappings().one()
        )

    executed_statements = []

    def record_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        executed_statements.append(statement.strip())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        executed = run_migrations(engine, migrations_dir=migrations_dir)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    with engine.connect() as connection:
        applied = connection.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all()
        land_after = dict(
            connection.execute(
                text("SELECT * FROM lands WHERE source_email_id = 'historical-land'")
            )
            .mappings()
            .one()
        )
        sync_after = dict(
            connection.execute(text("SELECT * FROM sync_history")).mappings().one()
        )

    assert executed == []
    migration_sql = {
        migration.sql.strip() for migration in discover_migrations(migrations_dir)
    }
    assert migration_sql.isdisjoint(executed_statements)
    assert [f"{version}_{name}" for version, name in applied] == list(
        BASELINE_IDENTIFIERS
    )
    assert land_after == land_before
    assert sync_after == sync_before
    assert "idx_lands_is_favorite" in {
        index["name"] for index in inspect(engine).get_indexes("lands")
    }
    engine.dispose()


def test_historical_baseline_runs_only_genuinely_new_migrations(tmp_path):
    from migrations.runner import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'future.db'}")
    _create_historical_schema(engine)
    migrations_dir = _baseline_migrations_dir(tmp_path)
    _write_migration(
        migrations_dir,
        "999_create_future_marker.sql",
        "CREATE TABLE future_marker (id INTEGER PRIMARY KEY);",
    )

    executed = run_migrations(engine, migrations_dir=migrations_dir)

    assert executed == ["999_create_future_marker"]
    assert inspect(engine).has_table("future_marker")
    engine.dispose()


def test_migration_runner_rejects_ambiguous_schema_without_partial_replay(tmp_path):
    from migrations.runner import MigrationError, run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'ambiguous.db'}")
    _create_historical_schema(engine)
    _add_operator_drift(engine)

    with pytest.raises(MigrationError) as exc_info:
        run_migrations(engine)

    error = str(exc_info.value)
    assert "unexpected tables: operator_audit" in error
    assert "lands unexpected columns: operator_note" in error
    assert "python -m migrations.runner --baseline-existing --yes" in error
    assert "Manual verified baseline for drifted databases" in error
    inspector = inspect(engine)
    assert inspector.has_table("operator_audit")
    assert not inspector.has_table("schema_migrations")
    engine.dispose()


def test_manual_baseline_records_drifted_schema_and_normal_start_is_current(
    tmp_path, monkeypatch, capsys
):
    from migrations.runner import (
        BASELINE_IDENTIFIERS,
        discover_migrations,
        main as migration_main,
        run_migrations,
    )

    database_url = f"sqlite:///{tmp_path / 'manual-baseline.db'}"
    engine = create_engine(database_url)
    db.metadata.create_all(engine)
    _add_operator_drift(engine)

    monkeypatch.setenv("DATABASE_URL", database_url)
    migration_main(["--baseline-existing", "--yes"])

    baseline = [
        migration
        for migration in discover_migrations()
        if migration.identifier in BASELINE_IDENTIFIERS
    ]
    output_lines = capsys.readouterr().out.strip().splitlines()
    assert output_lines == [
        "Recorded manual baseline entries:",
        *[
            f"{migration.version} {migration.name} {migration.checksum}"
            for migration in baseline
        ],
    ]

    with engine.connect() as connection:
        ledger = connection.execute(
            text(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )
        ).all()
    assert ledger == [
        (migration.version, migration.name, migration.checksum)
        for migration in baseline
    ]
    # Same checksums as the repository files this directory was copied from,
    # so the recorded baseline validates and nothing is pending.
    assert (
        run_migrations(engine, migrations_dir=_baseline_migrations_dir(tmp_path)) == []
    )
    engine.dispose()


def test_manual_baseline_refuses_existing_ledger(tmp_path, monkeypatch):
    from migrations.runner import MigrationError, main as migration_main, run_migrations

    database_url = f"sqlite:///{tmp_path / 'existing-ledger.db'}"
    engine = create_engine(database_url)
    _create_historical_schema(engine)
    assert (
        run_migrations(engine, migrations_dir=_baseline_migrations_dir(tmp_path)) == []
    )
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(MigrationError, match="schema_migrations already exists"):
        migration_main(["--baseline-existing", "--yes"])

    engine.dispose()


def test_manual_baseline_refuses_empty_database(tmp_path, monkeypatch):
    from migrations.runner import MigrationError, main as migration_main

    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(MigrationError, match="no application tables"):
        migration_main(["--baseline-existing", "--yes"])


def test_manual_baseline_requires_yes(tmp_path, monkeypatch):
    from migrations.runner import MigrationError, main as migration_main

    database_url = f"sqlite:///{tmp_path / 'confirmation.db'}"
    engine = create_engine(database_url)
    db.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE operator_audit (id INTEGER)"))
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(MigrationError, match="requires the explicit --yes flag"):
        migration_main(["--baseline-existing"])

    assert not inspect(engine).has_table("schema_migrations")
    engine.dispose()


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


def test_no_migration_contains_an_unescaped_percent_sign():
    """psycopg2 eats a lone percent sign before PostgreSQL sees the statement.

    The runner calls `exec_driver_sql`, which reaches psycopg2 with an empty
    parameter mapping, so the driver runs its own interpolation pass over the
    SQL. A migration containing `format('... %I ...')` or `LIKE 'x%'` never
    executes: it dies with "immutabledict is not a sequence" *at deploy time*,
    after the container has already replaced the running app. Doubling the
    sign is the escape psycopg2 documents; this test is why nobody has to
    rediscover that from a failed deploy (found while writing migration 013).
    """
    from migrations.runner import discover_migrations

    lone_percent = re.compile(r"(?<!%)%(?!%)")
    offenders = {
        migration.identifier: sorted(
            {
                line_number
                for line_number, line in enumerate(migration.sql.splitlines(), start=1)
                if lone_percent.search(line)
            }
        )
        for migration in discover_migrations(
            Path(__file__).parent.parent / "migrations"
        )
        if lone_percent.search(migration.sql)
    }

    assert not offenders, (
        "unescaped percent signs in migration SQL (double them, or use "
        f"quote_ident/concatenation instead of format()): {offenders}"
    )
