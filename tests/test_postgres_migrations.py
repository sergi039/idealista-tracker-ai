"""The migration SQL is PostgreSQL, so it is tested on PostgreSQL.

Everything in `migrations/` is PostgreSQL-only and multi-statement, which
pysqlite refuses outright ("You can only execute one statement at a time"), so
the SQLite tests in tests/test_deployment_bootstrap.py can only cover
migrations that are never executed - the frozen 000-012 baseline. Migration
013 (issue #102) is the first file after that baseline, and asserting its
effect through `db.create_all()` would prove only that the *models* changed.

These tests therefore run the real runner over the real files against a real
PostgreSQL server, both on an empty database (the fresh-install path) and on a
database that already holds rows under the pre-013 schema (the upgrade path
the owner's database will actually take).

Point them at a *throwaway* server, never at a database with real data:

    docker run -d --rm --name pg-migtest -e POSTGRES_PASSWORD=migtest \\
        -e POSTGRES_USER=migtest -e POSTGRES_DB=migtest \\
        -p 127.0.0.1:55432:5432 postgres:15-alpine
    TEST_DATABASE_URL_POSTGRES=postgresql://migtest:migtest@127.0.0.1:55432/migtest \\
        uv run pytest tests/test_postgres_migrations.py -v

Each test creates and drops its own database on that server. CI sets both
environment variables, so a missing server there is a failure rather than a
silent skip.
"""

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import db
from migrations.runner import BASELINE_IDENTIFIERS, MIGRATIONS_DIR

# Importing the models is what registers them on db.metadata, which the
# schema-parity assertion below compares the migrated database against.
import models  # noqa: F401

SERVER_URL_ENV = "TEST_DATABASE_URL_POSTGRES"
REQUIRE_ENV = "REQUIRE_POSTGRES_TESTS"

IDENTITY_MIGRATION = "013_add_search_profile_search_identity"
IDENTITY_COLUMNS = ("source_search_key", "source_search_url", "is_auto_created")
IDENTITY_INDEX = "ux_search_profiles_source_search_key"
LABEL_INDEX = "ux_search_profiles_name_without_key"

_DATABASE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _server_url() -> str:
    url = (os.environ.get(SERVER_URL_ENV) or "").strip()
    if url:
        return url
    if (os.environ.get(REQUIRE_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        pytest.fail(
            f"{REQUIRE_ENV} is set but {SERVER_URL_ENV} is empty: the PostgreSQL "
            "migration tests would silently skip, which is how untested "
            "migration SQL reaches production"
        )
    pytest.skip(
        f"set {SERVER_URL_ENV} to a throwaway PostgreSQL server to run the "
        "migration tests (see this module's docstring)"
    )


@pytest.fixture
def postgres_url():
    """A freshly created, empty PostgreSQL database, dropped afterwards."""
    server_url = _server_url()
    name = f"idealista_migration_test_{uuid.uuid4().hex[:12]}"
    assert _DATABASE_NAME_RE.fullmatch(name)

    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except SQLAlchemyError as exc:
        admin.dispose()
        pytest.fail(f"{SERVER_URL_ENV} is set but unusable: {exc}")

    database_url = server_url.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield database_url
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # noqa: S608
            )
        admin.dispose()


def _migrations_through_baseline(tmp_path: Path) -> Path:
    """The repository's migrations, truncated to the pre-013 baseline."""
    import shutil

    directory = tmp_path / "through-012"
    directory.mkdir()
    for identifier in BASELINE_IDENTIFIERS:
        shutil.copy(
            MIGRATIONS_DIR / f"{identifier}.sql", directory / f"{identifier}.sql"
        )
    return directory


def _insert_profile(connection, **values) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    connection.execute(
        text(f"INSERT INTO search_profiles ({columns}) VALUES ({placeholders})"),  # noqa: S608
        values,
    )


def test_a_fresh_database_gets_every_migration_and_matches_the_models(postgres_url):
    from migrations.runner import get_schema_status, run_migrations
    from services.health_service import get_schema_status as get_full_schema_status

    engine = create_engine(postgres_url)
    try:
        executed = run_migrations(engine)

        assert executed[0] == "000_create_schema_migrations"
        assert IDENTITY_MIGRATION in executed
        assert executed == sorted(executed), "migrations must apply in file order"
        assert get_schema_status(engine) == {"status": "ok"}
        assert run_migrations(engine) == [], "a second run must be a no-op"

        # The strongest available statement that the SQL and the models agree:
        # every model table, column, foreign key, index and constraint has to
        # exist in the database the migrations built.
        assert get_full_schema_status(engine, db.metadata) == {"status": "ok"}
    finally:
        engine.dispose()


def test_013_adds_the_identity_columns_and_a_unique_key_index(postgres_url):
    from migrations.runner import run_migrations

    engine = create_engine(postgres_url)
    try:
        run_migrations(engine)
        inspector = inspect(engine)

        columns = {
            column["name"] for column in inspector.get_columns("search_profiles")
        }
        assert set(IDENTITY_COLUMNS).issubset(columns)

        unique_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("search_profiles")
            if index["unique"]
        }
        assert IDENTITY_INDEX in unique_indexes
        assert unique_indexes[IDENTITY_INDEX]["column_names"] == ["source_search_key"]

        # The partial index that replaces the dropped UNIQUE on the label.
        assert LABEL_INDEX in unique_indexes
        assert unique_indexes[LABEL_INDEX]["column_names"] == ["name"]
        assert "source_search_key IS NULL" in (
            unique_indexes[LABEL_INDEX]
            .get("dialect_options", {})
            .get("postgresql_where")
            or ""
        )

        key = "idealista:v1:" + "a" * 64
        with engine.begin() as connection:
            _insert_profile(connection, name="First", source_search_key=key)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_profile(connection, name="Second", source_search_key=key)

        # NULL is "not identified yet", not a value: many rows may hold it.
        with engine.begin() as connection:
            _insert_profile(connection, name="Third")
            _insert_profile(connection, name="Fourth")
    finally:
        engine.dispose()


def test_013_frees_the_label_on_a_database_that_already_holds_rows(
    postgres_url, tmp_path
):
    """The upgrade path: same label, two subscriptions, nothing backfilled."""
    from migrations.runner import run_migrations

    engine = create_engine(postgres_url)
    try:
        run_migrations(engine, migrations_dir=_migrations_through_baseline(tmp_path))

        with engine.begin() as connection:
            _insert_profile(connection, name="Terrenos norte", description="pre-013")
        with pytest.raises(IntegrityError):
            # This is what migration 013 exists to make possible.
            with engine.begin() as connection:
                _insert_profile(connection, name="Terrenos norte")

        assert run_migrations(engine) == [IDENTITY_MIGRATION]

        # Two *identified* subscriptions may now share the label...
        with engine.begin() as connection:
            _insert_profile(
                connection,
                name="Terrenos norte",
                source_search_key="idealista:v1:" + "b" * 64,
            )
        # ... but two unidentified ones still may not: dropping the UNIQUE must
        # not drop the protection around check-then-insert.
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_profile(connection, name="Terrenos norte")

        with engine.begin() as connection:
            existing = (
                connection.execute(
                    text(
                        "SELECT description, source_search_key, source_search_url, "
                        "is_auto_created FROM search_profiles "
                        "WHERE description = 'pre-013'"
                    )
                )
                .mappings()
                .one()
            )
            labels = connection.execute(
                text(
                    "SELECT count(*) FROM search_profiles WHERE name = 'Terrenos norte'"
                )
            ).scalar_one()

        assert labels == 2
        # Not backfilled: no stored row records which saved search it came from.
        assert existing["source_search_key"] is None
        assert existing["source_search_url"] is None
        # And an existing profile is never treated as one the ingester named.
        assert existing["is_auto_created"] is False
    finally:
        engine.dispose()


def test_the_deploy_entrypoint_migrates_and_boots(postgres_url):
    """`python -m migrations.runner && gunicorn ...`, as the Dockerfile runs it."""
    entrypoint = subprocess.run(
        [
            sys.executable,
            "-c",
            "from migrations.runner import main as migrate; "
            "migrate(); import main; assert main.app",
        ],
        cwd=Path(__file__).parent.parent,
        env={
            "PATH": os.environ.get("PATH", ""),
            "DATABASE_URL": postgres_url,
            "SECRET_KEY": "postgres-entrypoint-secret",
            "SESSION_SECRET": "postgres-entrypoint-session-secret",
            "AUTO_START_SCHEDULER": "false",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert entrypoint.returncode == 0, entrypoint.stderr
