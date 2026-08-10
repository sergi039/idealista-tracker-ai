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

import json
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
ASSIGNMENT_CLEANUP_MIGRATION = "014_clear_profile_assignment_metadata"
STATUS_SOURCE_MIGRATION = "015_add_listing_status_source"
IDENTITY_COLUMNS = ("source_search_key", "source_search_url", "is_auto_created")
IDENTITY_INDEX = "ux_search_profiles_source_search_key"
LABEL_INDEX = "ux_search_profiles_name_without_key"
CATCH_ALL_CONSTRAINT = "ck_search_profiles_default_has_no_search_key"

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

        # The catch-all may not be anybody's saved search, and the database is
        # what enforces it - not each caller that happens to write is_default.
        assert CATCH_ALL_CONSTRAINT in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("search_profiles")
        }
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_profile(
                    connection,
                    name="Identified default",
                    source_search_key="idealista:v1:" + "c" * 64,
                    is_default=True,
                )
        with engine.begin() as connection:
            _insert_profile(connection, name="Real catch-all", is_default=True)
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

        assert run_migrations(engine) == [
            IDENTITY_MIGRATION,
            ASSIGNMENT_CLEANUP_MIGRATION,
            STATUS_SOURCE_MIGRATION,
        ]

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


def test_014_clears_profile_assignment_and_frees_a_pinned_listing(postgres_url):
    """The metadata is gone, and only that key is gone.

    `search_profile_repair_service` refuses to move a row whose
    `enrichment.profile_assignment.manual_override` is set. Nothing can clear
    that flag through the UI any more -- the form and its route were removed --
    so a stale pin would freeze a listing in a fragmented profile forever. This
    migration is what unfreezes it, and it must not take the rest of the
    enrichment payload with it.
    """
    from migrations.runner import run_migrations

    engine = create_engine(postgres_url)
    try:
        run_migrations(engine)

        with engine.begin() as connection:
            _insert_profile(connection, name="Land at Norte", is_active=True)
            profile_id = connection.execute(
                text("SELECT id FROM search_profiles WHERE name = 'Land at Norte'")
            ).scalar_one()

            rows = {
                "pinned": {
                    "google": {"travel_state": "refused"},
                    "profile_assignment": {
                        "method": "manual_override",
                        "manual_override": True,
                    },
                },
                "geo_filed": {
                    "profile_assignment": {
                        "method": "nearest_custom_target",
                        "distance_km": 1.2,
                    }
                },
                "untouched": {"google": {"travel_state": "ok"}},
                "null_enrichment": None,
                # A `json`-only document: PostgreSQL stores the column as text
                # and accepts this literal, while `jsonb` parses numbers into
                # `numeric` and raises "value overflow" on it. The shorter
                # `enrichment::jsonb - 'key'` form of this migration would abort
                # here and clear nothing at all -- taking the deploy with it.
                "json_only_number": (
                    '{"profile_assignment": {"manual_override": true},'
                    ' "score": 1e1000000}'
                ),
                "not_an_object": "[1, 2, 3]",
                # `json` accepts an escaped NUL; `json_each` decodes keys into
                # `text`, which cannot hold one. This row can only be skipped --
                # what matters is that it is skipped rather than aborting the
                # migration and leaving every other row uncleaned.
                "nul_in_key": (
                    '{"profile_assignment": {"manual_override": true}, "\\u0000": 1}'
                ),
            }
            for source_email_id, enrichment in rows.items():
                if enrichment is None:
                    payload = None
                elif isinstance(enrichment, str):
                    payload = enrichment
                else:
                    payload = json.dumps(enrichment)
                connection.execute(
                    text(
                        "INSERT INTO properties "
                        "(source_email_id, title, search_profile_id, enrichment) "
                        "VALUES (:sid, :title, :pid, CAST(:enrichment AS json))"
                    ),
                    {
                        "sid": source_email_id,
                        "title": source_email_id,
                        "pid": profile_id,
                        "enrichment": payload,
                    },
                )

        # Re-running the file is what the deploy does on an already-migrated
        # database; the runner records it, so this asserts the effect directly.
        with engine.begin() as connection:
            connection.exec_driver_sql(
                (MIGRATIONS_DIR / f"{ASSIGNMENT_CLEANUP_MIGRATION}.sql").read_text(
                    encoding="utf-8"
                )
            )

        with engine.begin() as connection:
            stored = dict(
                connection.execute(
                    text("SELECT source_email_id, enrichment FROM properties")
                ).all()
            )

        assert stored["pinned"] == {"google": {"travel_state": "refused"}}, (
            "the pin was cleared but the rest of the enrichment payload was not kept"
        )
        assert stored["geo_filed"] == {}
        assert stored["untouched"] == {"google": {"travel_state": "ok"}}
        assert stored["null_enrichment"] is None
        assert stored["json_only_number"] == {"score": float("inf")}, (
            "a json-only numeric literal aborted the migration or lost its payload"
        )
        assert stored["not_an_object"] == [1, 2, 3]
        # Skipped, not lost, and above all: it did not stop the rest.
        assert "profile_assignment" in stored["nul_in_key"], (
            "an undecodable row was silently emptied instead of being left alone"
        )

        # Idempotent: the deploy re-runs nothing, but a hand re-run must not
        # turn a now-empty object into SQL NULL or otherwise drift.
        with engine.begin() as connection:
            connection.exec_driver_sql(
                (MIGRATIONS_DIR / f"{ASSIGNMENT_CLEANUP_MIGRATION}.sql").read_text(
                    encoding="utf-8"
                )
            )
        with engine.begin() as connection:
            assert (
                dict(
                    connection.execute(
                        text("SELECT source_email_id, enrichment FROM properties")
                    ).all()
                )
                == stored
            )
    finally:
        engine.dispose()


def test_014_re_raises_anything_that_is_not_a_json_decoding_failure(postgres_url):
    """A swallowed lock timeout would record the migration over untouched rows.

    The two SQLSTATEs this migration tolerates are properties of the stored
    document. Everything else -- a lock timeout, a deadlock, a disk error --
    means the row was not processed for an operational reason, and recording
    the migration as applied would make it permanent. A trigger raising a
    different SQLSTATE stands in for all of them.
    """
    from migrations.runner import run_migrations

    engine = create_engine(postgres_url)
    try:
        run_migrations(engine)

        with engine.begin() as connection:
            _insert_profile(connection, name="Land at Norte", is_active=True)
            profile_id = connection.execute(
                text("SELECT id FROM search_profiles WHERE name = 'Land at Norte'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO properties "
                    "(source_email_id, title, search_profile_id, enrichment) "
                    "VALUES ('pinned', 'pinned', :pid, CAST(:enrichment AS json))"
                ),
                {
                    "pid": profile_id,
                    "enrichment": json.dumps(
                        {"profile_assignment": {"manual_override": True}}
                    ),
                },
            )
            connection.exec_driver_sql(
                "CREATE FUNCTION refuse_update() RETURNS trigger AS $refuse$ "
                "BEGIN RAISE EXCEPTION 'simulated operational failure' "
                "USING ERRCODE = 'lock_not_available'; END $refuse$ LANGUAGE plpgsql"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER properties_refuse_update BEFORE UPDATE ON properties "
                "FOR EACH ROW EXECUTE FUNCTION refuse_update()"
            )

        sql = (MIGRATIONS_DIR / f"{ASSIGNMENT_CLEANUP_MIGRATION}.sql").read_text(
            encoding="utf-8"
        )
        with pytest.raises(SQLAlchemyError) as failure:
            with engine.begin() as connection:
                connection.exec_driver_sql(sql)
        assert "simulated operational failure" in str(failure.value)

        # And the row is untouched, not half-cleaned.
        with engine.begin() as connection:
            stored = connection.execute(
                text("SELECT enrichment FROM properties WHERE source_email_id='pinned'")
            ).scalar_one()
        assert stored == {"profile_assignment": {"manual_override": True}}
    finally:
        engine.dispose()


def test_015_adds_the_status_source_column_and_guards_its_values(postgres_url):
    """The column the page reads to say where a status came from.

    Nothing is backfilled on purpose: an existing row records no evidence of how
    its status was decided, and inventing one would be the false confirmation
    the column exists to prevent.
    """
    from migrations.runner import run_migrations

    engine = create_engine(postgres_url)
    try:
        run_migrations(engine)

        for table in ("properties", "lands"):
            columns = {column["name"] for column in inspect(engine).get_columns(table)}
            assert "listing_status_source" in columns

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO properties (source_email_id, title, listing_status) "
                    "VALUES ('015-legacy', 'Legacy row', 'active')"
                )
            )
            stored = connection.execute(
                text(
                    "SELECT listing_status_source FROM properties "
                    "WHERE source_email_id = '015-legacy'"
                )
            ).scalar_one()
        assert stored is None, "the SQL default is nothing; the model writes 'ingest'"

        # The CHECK constraint is the one that keeps a typo out of the column
        # the templates switch on.
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE properties SET listing_status_source = 'guessed' "
                        "WHERE source_email_id = '015-legacy'"
                    )
                )

        for value in ("ingest", "email", "check", "manual"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE properties SET listing_status_source = :value "
                        "WHERE source_email_id = '015-legacy'"
                    ),
                    {"value": value},
                )

        # Re-running the file must not fail on the constraint it already added.
        sql = (MIGRATIONS_DIR / f"{STATUS_SOURCE_MIGRATION}.sql").read_text(
            encoding="utf-8"
        )
        with engine.begin() as connection:
            connection.exec_driver_sql(sql)
    finally:
        engine.dispose()
