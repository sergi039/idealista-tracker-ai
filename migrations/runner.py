"""Apply the repository's numbered SQL migrations exactly once."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    inspect,
    text,
)

logger = logging.getLogger(__name__)

MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
MIGRATIONS_DIR = Path(__file__).resolve().parent
MIGRATION_TABLE = "schema_migrations"
MIGRATION_LOCK_ID = 18_2026_08

# The application used db.create_all() before the migration ledger existed.
# This frozen fingerprint describes that final historical schema. Versions
# through 012 are metadata-only baselined when (and only when) it matches.
BASELINE_IDENTIFIERS = (
    "000_create_schema_migrations",
    "001_create_lands_table",
    "002_create_operational_tables",
    "003_create_settings_tables",
    "004_create_ai_analysis_variants_table",
    "005_add_indexes",
    "006_add_idealista_property_id",
    "007_create_properties_table",
    "008_create_search_profiles_table",
    "009_add_search_profiles_ai_config",
    "010_create_property_ai_analysis_variants_table",
    "011_add_check_constraints",
    "012_drop_duplicate_legacy_indexes",
)


def _columns(names: str) -> frozenset[str]:
    return frozenset(names.split())


HISTORICAL_SCHEMA_FINGERPRINT = {
    "ai_analysis_variants": _columns("id land_id provider model analysis created_at"),
    "app_settings": _columns("id key value created_at updated_at"),
    "land_history": _columns(
        "id land_id snapshot_date price title description area land_type url "
        "change_type price_previous price_change_amount price_change_percentage"
    ),
    "lands": _columns(
        "id source_email_id idealista_property_id email_subject email_sender title "
        "url price area municipality location_lat location_lon location_accuracy "
        "land_type description infrastructure_basic infrastructure_extended "
        "transport environment neighborhood services_quality legal_status "
        "property_details ai_analysis enhanced_description score_total "
        "score_investment score_lifestyle travel_time_oviedo travel_time_gijon "
        "travel_time_nearest_beach nearest_beach_name travel_time_airport "
        "travel_time_train_station travel_time_hospital travel_time_police "
        "distance_airport distance_train_station distance_hospital distance_police "
        "previous_price price_change_amount price_change_percentage "
        "price_changed_date is_favorite listing_status listing_removed_date "
        "listing_last_checked created_at email_date updated_at"
    ),
    "market_settings": _columns(
        "id construction_basic_min construction_basic_avg construction_basic_max "
        "construction_premium_min construction_premium_avg construction_premium_max "
        "purchase_costs_ratio urban_vacancy_rate urban_operating_expenses "
        "urban_management_fee suburban_vacancy_rate suburban_operating_expenses "
        "suburban_management_fee rural_vacancy_rate rural_operating_expenses "
        "rural_management_fee urban_rental_min urban_rental_avg urban_rental_max "
        "suburban_rental_min suburban_rental_avg suburban_rental_max rural_rental_min "
        "rural_rental_avg rural_rental_max created_at updated_at"
    ),
    "properties": _columns(
        "id source_email_id idealista_property_id email_subject email_sender "
        "search_profile_id title url deal_type property_category property_subtype "
        "price currency area area_type municipality location_lat location_lon "
        "location_accuracy description attributes property_details enrichment travel "
        "scoring ai_analysis enhanced_description score_total score_investment "
        "score_lifestyle previous_price price_change_amount price_change_percentage "
        "price_changed_date is_favorite listing_status listing_removed_date "
        "listing_last_checked created_at email_date updated_at"
    ),
    "property_ai_analysis_variants": _columns(
        "id property_id provider model analysis created_at"
    ),
    "scoring_criteria": _columns(
        "id criteria_name profile weight active created_at updated_at"
    ),
    "search_profiles": _columns(
        "id name description is_active is_default email_matchers classification_rules "
        "travel_targets ui_config scoring_config ai_config created_at updated_at"
    ),
    "sync_history": _columns(
        "id sync_type backend total_emails_found new_properties_added "
        "price_updated_count expired_count sync_duration status error_message "
        "started_at completed_at"
    ),
}

_ledger_metadata = MetaData()
_ledger_table = Table(
    MIGRATION_TABLE,
    _ledger_metadata,
    Column("version", String(3), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("checksum", String(64), nullable=False),
    Column(
        "applied_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    ),
)


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


def _historical_schema_errors(connection) -> list[str]:
    """Return deterministic differences from the pre-ledger schema."""
    inspector = inspect(connection)
    expected_tables = set(HISTORICAL_SCHEMA_FINGERPRINT)
    actual_tables = set(inspector.get_table_names()) - {MIGRATION_TABLE}
    errors = []

    missing_tables = sorted(expected_tables - actual_tables)
    if missing_tables:
        errors.append("missing tables: " + ", ".join(missing_tables))

    unexpected_tables = sorted(actual_tables - expected_tables)
    if unexpected_tables:
        errors.append("unexpected tables: " + ", ".join(unexpected_tables))

    for table_name in sorted(expected_tables & actual_tables):
        column_info = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        expected_columns = HISTORICAL_SCHEMA_FINGERPRINT[table_name]
        actual_columns = set(column_info)

        missing_columns = sorted(expected_columns - actual_columns)
        if missing_columns:
            errors.append(
                f"{table_name} missing columns: " + ", ".join(missing_columns)
            )

        unexpected_columns = sorted(actual_columns - expected_columns)
        if unexpected_columns:
            errors.append(
                f"{table_name} unexpected columns: " + ", ".join(unexpected_columns)
            )

        primary_key = tuple(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        if primary_key != ("id",):
            errors.append(
                f"{table_name} primary key is {primary_key!r}, expected ('id',)"
            )
        elif not isinstance(column_info["id"]["type"], Integer):
            errors.append(
                f"{table_name}.id type is {column_info['id']['type']}, expected INTEGER"
            )

    return errors


def _baseline_migrations(migrations: list[Migration]) -> list[Migration]:
    migrations_by_identifier = {
        migration.identifier: migration for migration in migrations
    }
    missing = [
        identifier
        for identifier in BASELINE_IDENTIFIERS
        if identifier not in migrations_by_identifier
    ]
    baseline_versions = {
        identifier.partition("_")[0] for identifier in BASELINE_IDENTIFIERS
    }
    unexpected = [
        migration.identifier
        for migration in migrations
        if migration.version in baseline_versions
        and migration.identifier not in BASELINE_IDENTIFIERS
    ]
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise MigrationError(
            "Historical baseline migration set does not match this application ("
            + "; ".join(details)
            + ")"
        )

    return [migrations_by_identifier[identifier] for identifier in BASELINE_IDENTIFIERS]


def _baseline_historical_schema(
    connection, migrations: list[Migration]
) -> dict[str, tuple[str, str]]:
    """Create only ledger metadata for an exact pre-ledger schema match."""
    errors = _historical_schema_errors(connection)
    if errors:
        raise MigrationError(
            "Database has application tables but no schema_migrations ledger and "
            "does not match the historical baseline fingerprint; refusing to "
            "guess or replay migrations. " + "; ".join(errors)
        )

    baseline = _baseline_migrations(migrations)
    _ledger_table.create(connection, checkfirst=False)
    connection.execute(
        _ledger_table.insert(),
        [
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": migration.checksum,
            }
            for migration in baseline
        ],
    )
    logger.info(
        "Historical schema fingerprint matched; recorded metadata-only baseline "
        "for %d migration(s) through %s without executing migration SQL",
        len(baseline),
        baseline[-1].version,
    )
    return {
        migration.version: (migration.name, migration.checksum)
        for migration in baseline
    }


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

        inspector = inspect(connection)
        has_ledger = inspector.has_table(MIGRATION_TABLE)
        existing_tables = set(inspector.get_table_names()) - {MIGRATION_TABLE}

        if not has_ledger and existing_tables:
            applied = _baseline_historical_schema(connection, migrations)
        else:
            applied = _applied_migrations(connection)
            if has_ledger and not applied and existing_tables:
                raise MigrationError(
                    "schema_migrations exists but is empty while application "
                    "tables are present; refusing to guess or replay migrations"
                )
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
