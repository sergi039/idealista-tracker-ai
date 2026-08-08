"""Apply the repository's numbered SQL migrations exactly once."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text

logger = logging.getLogger(__name__)

MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
MIGRATIONS_DIR = Path(__file__).resolve().parent
MIGRATION_TABLE = "schema_migrations"
MIGRATION_LOCK_ID = 18_2026_08


class MigrationError(RuntimeError):
    """Raised when migration discovery or application is unsafe."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str
    sql: str

    @property
    def identifier(self) -> str:
        return self.path.stem


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return validated migrations in numeric order.

    Every SQL file in the migration root must be numbered. An ignored or
    duplicate file would make two deployments build different schemas, so it
    is a hard error.
    """
    migrations = []
    versions = set()

    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"Migration filename must match NNN_name.sql: {path.name}"
            )

        version = match.group("version")
        if version in versions:
            raise MigrationError(f"Duplicate migration version: {version}")
        versions.add(version)

        raw_sql = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                checksum=hashlib.sha256(raw_sql).hexdigest(),
                sql=raw_sql.decode("utf-8"),
            )
        )

    if not migrations:
        raise MigrationError(f"No migrations found in {migrations_dir}")

    return migrations


def _applied_migrations(connection) -> dict[str, tuple[str, str]]:
    if not inspect(connection).has_table(MIGRATION_TABLE):
        return {}

    rows = connection.execute(
        text("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
    )
    return {row.version: (row.name, row.checksum) for row in rows}


def _validate_applied(
    migrations: list[Migration], applied: dict[str, tuple[str, str]]
) -> None:
    expected = {migration.version: migration for migration in migrations}
    unknown = sorted(set(applied) - set(expected))
    if unknown:
        raise MigrationError(
            "Database contains migrations unknown to this application: "
            + ", ".join(unknown)
        )

    for version, (applied_name, applied_checksum) in applied.items():
        migration = expected[version]
        if applied_name != migration.name:
            raise MigrationError(
                f"Migration {version} name changed: {applied_name} != {migration.name}"
            )
        if applied_checksum != migration.checksum:
            raise MigrationError(
                f"Migration {version} checksum changed after it was applied"
            )


def run_migrations(engine: Engine, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply all pending migrations atomically and return their identifiers."""
    migrations = discover_migrations(migrations_dir)
    applied_identifiers = []

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )

        applied = _applied_migrations(connection)
        _validate_applied(migrations, applied)

        for migration in migrations:
            if migration.version in applied:
                continue

            logger.info("Applying migration %s", migration.identifier)
            connection.exec_driver_sql(migration.sql)
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, checksum) "
                    "VALUES (:version, :name, :checksum)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                },
            )
            applied_identifiers.append(migration.identifier)

    return applied_identifiers


def get_schema_status(
    engine: Engine, migrations_dir: Path = MIGRATIONS_DIR
) -> dict[str, str]:
    """Report whether migration history exactly matches this application."""
    try:
        migrations = discover_migrations(migrations_dir)
        with engine.connect() as connection:
            applied = _applied_migrations(connection)
        if not applied:
            return {"status": "missing"}
        _validate_applied(migrations, applied)
    except MigrationError:
        logger.warning("Migration history validation failed", exc_info=True)
        return {"status": "incomplete"}

    expected_versions = {migration.version for migration in migrations}
    if set(applied) != expected_versions:
        return {"status": "incomplete"}
    return {"status": "ok"}


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise MigrationError("DATABASE_URL must be set before running migrations")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        applied = run_migrations(engine)
    finally:
        engine.dispose()

    if applied:
        logger.info("Applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        logger.info("Database schema is already current")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
