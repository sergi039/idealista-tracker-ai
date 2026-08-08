"""Dependency checks used by the public liveness/readiness endpoint."""

from __future__ import annotations

import logging

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text

from migrations.runner import get_schema_status as get_migration_status

logger = logging.getLogger(__name__)


def _expected_foreign_keys(
    table,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    expected = set()
    for constraint in table.foreign_key_constraints:
        expected.add(
            (
                tuple(constraint.column_keys),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "").upper(),
            )
        )
    return expected


def _actual_foreign_keys(inspector, table_name: str):
    actual = set()
    for foreign_key in inspector.get_foreign_keys(table_name):
        actual.add(
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (foreign_key.get("options", {}).get("ondelete") or "").upper(),
            )
        )
    return actual


def _named_constraints(table, constraint_type) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name
    }


def get_schema_status(engine, metadata) -> dict[str, str]:
    """Check migration history plus every model table, column, and foreign key."""
    migration_status = get_migration_status(engine)
    if migration_status["status"] != "ok":
        return migration_status

    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            return {"status": "incomplete"}

        actual_columns = {
            column["name"] for column in inspector.get_columns(table.name)
        }
        expected_columns = {column.name for column in table.columns}
        if not expected_columns.issubset(actual_columns):
            return {"status": "incomplete"}

        expected_foreign_keys = _expected_foreign_keys(table)
        if not expected_foreign_keys.issubset(
            _actual_foreign_keys(inspector, table.name)
        ):
            return {"status": "incomplete"}

        expected_indexes = {index.name for index in table.indexes}
        actual_indexes = {
            index_info["name"] for index_info in inspector.get_indexes(table.name)
        }
        if not expected_indexes.issubset(actual_indexes):
            return {"status": "incomplete"}

        expected_checks = _named_constraints(table, CheckConstraint)
        actual_checks = {
            constraint_info["name"]
            for constraint_info in inspector.get_check_constraints(table.name)
        }
        if not expected_checks.issubset(actual_checks):
            return {"status": "incomplete"}

        expected_unique = _named_constraints(table, UniqueConstraint)
        actual_unique = {
            constraint_info["name"]
            for constraint_info in inspector.get_unique_constraints(table.name)
        }
        if not expected_unique.issubset(actual_unique):
            return {"status": "incomplete"}

    return {"status": "ok"}


def collect_health_checks(database) -> dict[str, str]:
    """Return health states for every production-critical subsystem."""
    checks = {"database": "unavailable", "schema": "unknown"}

    try:
        database.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
        checks["schema"] = get_schema_status(database.engine, database.metadata)[
            "status"
        ]
    except Exception as exc:
        logger.warning("Healthz database/schema check failed: %s", exc)
        try:
            database.session.rollback()
        except Exception:
            logger.warning("Healthz could not roll back the failed DB session")

    try:
        from services.scheduler_service import get_scheduler_status

        checks["scheduler"] = get_scheduler_status().get("status", "unknown")
    except Exception as exc:
        logger.warning("Healthz scheduler check failed: %s", exc)
        checks["scheduler"] = "unknown"

    return checks
