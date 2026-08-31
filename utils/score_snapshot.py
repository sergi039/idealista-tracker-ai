"""One home for the rollback snapshots the bulk tools write and read back.

`utils/backfill_pool.py`, `utils/recalc_sea_distance.py` and
`utils/recalc_property_travel.py` each carried their own `_snapshot_row` /
`_write_snapshot` / `_restore`, identical apart from which JSON column rides
along (`enrichment` for two of them, `travel` for the third).
`utils/restore_score_snapshot.py` would have been the fourth copy; three
diverging copies is how the merge-bot stub lost its shebang (#284), and a
rollback point is the last thing anybody holds when a rewrite went wrong.

The rules this module keeps, all of them learned rather than invented:

* a snapshot is never overwritten — the backfills refuse it, and a restore
  that silently replaced the rollback point it was about to consume would
  leave nowhere to go back to;
* it is written atomically (owner-only temp file in the target directory,
  fsync, rename), because a half-written rollback point reads as a whole one;
* a row restores exactly the columns it carries. An absent column means "not
  mine", never "set it to null" — the pool-weight snapshot carries no
  `enrichment`, and restoring it as null would erase every measurement in it;
* every value in every row is parsed *before* anything is written, so one
  unusable number cannot leave the table half restored.

The two snapshot shapes are one shape: a bare list of rows (what the
backfills write) and `{"created_at": ..., "profiles": {...}, "scores": [...]}`
(what a config change needs, because putting the weights back is half of
putting the scores back). `load()` normalises both.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app import db
from models import Property, SearchProfile

logger = logging.getLogger(__name__)

SCORE_COLUMNS: Tuple[str, ...] = ("score_total", "score_investment", "score_lifestyle")
# Every JSON column a snapshot row is allowed to carry. A key outside this set
# is refused rather than ignored: a snapshot naming a column this app does not
# restore is not a snapshot of this app's state.
JSON_COLUMNS: Tuple[str, ...] = ("scoring", "enrichment", "travel")
# What a listing is, as opposed to what it scored. A repair that corrects the
# classification has to snapshot these beside the scores, because
# `PropertyScoringService.scorer_for()` picks the scorer *by*
# `property_category` -- so restoring a score without the category it was
# computed under puts back a number the app would never produce again.
# Opt-in per caller, like the JSON columns: a plain score snapshot must keep
# writing exactly the rows it wrote before.
CLASSIFICATION_COLUMNS: Tuple[str, ...] = (
    "property_category",
    "property_subtype",
    "area_type",
)
# Where a listing says it is. Separate from the classification set because it
# is a different kind of fact and a different repair touches it: the stored
# string is what the alert email carried, and `services/property_comparables.
# same_municipality()` builds a row's peer pool from it, so a snapshot that
# restored the name without the scores it produced would put back a value
# score computed against a different set of neighbours.
LOCATION_COLUMNS: Tuple[str, ...] = ("municipality",)


class SnapshotError(Exception):
    """The snapshot cannot be trusted, so nothing is written."""


@dataclass
class Snapshot:
    """A normalised snapshot: the rows, and the profile config that went with them."""

    rows: List[Dict[str, Any]]
    profiles: Dict[int, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    path: Optional[str] = None

    @property
    def ids(self) -> List[int]:
        return [row["id"] for row in self.rows]


def decimal_str(value: Any) -> Optional[str]:
    """A score column as the snapshot stores it: a string, or None."""
    return str(value) if value is not None else None


def snapshot_row(
    prop: Property,
    json_columns: Sequence[str] = ("scoring",),
    classification_columns: Sequence[str] = (),
) -> Dict[str, Any]:
    """The current state of one property's score columns, plus what is asked for."""
    unknown = [name for name in json_columns if name not in JSON_COLUMNS]
    unknown += [
        name
        for name in classification_columns
        if name not in CLASSIFICATION_COLUMNS + LOCATION_COLUMNS
    ]
    if unknown:
        raise SnapshotError(f"Not a snapshot column: {', '.join(sorted(unknown))}")
    row: Dict[str, Any] = {"id": prop.id}
    for column in SCORE_COLUMNS:
        row[column] = decimal_str(getattr(prop, column))
    for column in classification_columns:
        row[column] = getattr(prop, column)
    for column in json_columns:
        row[column] = getattr(prop, column)
    return row


def _parse_score(value: Any, column: str, row_id: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SnapshotError(f"Row {row_id}: {column} is not a number: {value!r}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotError(
            f"Row {row_id}: {column} is not a number: {value!r}"
        ) from exc
    if not parsed.is_finite():
        raise SnapshotError(f"Row {row_id}: {column} is not finite: {value!r}")
    return parsed


def parse_row(row: Any) -> Dict[str, Any]:
    """Validate one row and return the column values to write.

    Raising here is the point: the caller parses every row before it writes
    the first one, so a snapshot with one bad value restores nothing at all
    rather than half of itself.
    """
    if not isinstance(row, dict):
        raise SnapshotError(f"Snapshot row is not an object: {row!r}")
    row_id = row.get("id")
    if isinstance(row_id, bool) or not isinstance(row_id, int):
        raise SnapshotError(f"Snapshot row has no integer id: {row!r}")

    known = {
        "id",
        *SCORE_COLUMNS,
        *JSON_COLUMNS,
        *CLASSIFICATION_COLUMNS,
        *LOCATION_COLUMNS,
    }
    unknown = sorted(set(row) - known)
    if unknown:
        raise SnapshotError(f"Row {row_id}: unknown column(s) {', '.join(unknown)}")

    parsed: Dict[str, Any] = {"id": row_id}
    for column in SCORE_COLUMNS:
        if column in row:
            parsed[column] = _parse_score(row[column], column, row_id)
    for column in CLASSIFICATION_COLUMNS + LOCATION_COLUMNS:
        if column in row:
            value = row[column]
            if value is not None and not isinstance(value, str):
                raise SnapshotError(
                    f"Row {row_id}: {column} is not a name: {type(value).__name__}"
                )
            parsed[column] = value
    for column in JSON_COLUMNS:
        if column in row:
            value = row[column]
            if not isinstance(value, (dict, list, type(None))):
                raise SnapshotError(
                    f"Row {row_id}: {column} is not JSON data: {type(value).__name__}"
                )
            parsed[column] = value
    if len(parsed) == 1:
        raise SnapshotError(f"Row {row_id} restores nothing: no columns in it")
    return parsed


def differs(prop: Property, parsed: Dict[str, Any]) -> bool:
    """Would applying this row change the row that is there now?"""
    for column, value in parsed.items():
        if column == "id":
            continue
        current = getattr(prop, column)
        if column in SCORE_COLUMNS:
            if (current is None) != (value is None):
                return True
            if current is not None and Decimal(str(current)) != value:
                return True
        elif current != value:
            return True
    return False


def apply_row(prop: Property, parsed: Dict[str, Any]) -> None:
    """Write one parsed row onto a property. Nothing here can fail."""
    for column, value in parsed.items():
        if column != "id":
            setattr(prop, column, value)


def load(path: str) -> Snapshot:
    """Read a snapshot file in either shape, or raise SnapshotError.

    Nothing is parsed lazily: every row is validated here, so a caller that
    reached the end of this function holds a snapshot it can apply whole.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SnapshotError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"{path} is not valid JSON: {exc}") from exc

    profiles: Dict[int, Any] = {}
    created_at: Optional[str] = None
    if isinstance(payload, list):
        raw_rows: Any = payload
    elif isinstance(payload, dict):
        if "scores" not in payload:
            raise SnapshotError(f"{path} has no 'scores' list")
        raw_rows = payload["scores"]
        created_at = payload.get("created_at")
        raw_profiles = payload.get("profiles") or {}
        if not isinstance(raw_profiles, dict):
            raise SnapshotError(f"{path}: 'profiles' is not an object")
        for key, value in raw_profiles.items():
            try:
                profile_id = int(key)
            except (TypeError, ValueError) as exc:
                raise SnapshotError(
                    f"{path}: profile key {key!r} is not an id"
                ) from exc
            if not isinstance(value, (dict, type(None))):
                raise SnapshotError(
                    f"{path}: profile {profile_id} config is not an object or null"
                )
            profiles[profile_id] = value
    else:
        raise SnapshotError(f"{path} is neither a list of rows nor a snapshot object")

    if not isinstance(raw_rows, list):
        raise SnapshotError(f"{path}: 'scores' is not a list")

    rows = [parse_row(row) for row in raw_rows]
    seen = set()
    for row in rows:
        if row["id"] in seen:
            raise SnapshotError(f"{path}: property {row['id']} appears twice")
        seen.add(row["id"])
    return Snapshot(rows=rows, profiles=profiles, created_at=created_at, path=path)


def write(payload: Any, path: str) -> None:
    """Write a snapshot without ever replacing one, and without a torn file."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        raise SystemExit(
            f"Snapshot {path} already exists; refusing to overwrite a rollback point."
        )
    handle_fd, temp_path = tempfile.mkstemp(
        dir=directory or ".", prefix=".snapshot-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # `link` rather than `replace`: the existence check above is not a lock,
        # and a parallel session that created the path in between must not lose
        # its rollback point to this one.
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise SystemExit(
                f"Snapshot {path} already exists; refusing to overwrite a rollback point."
            ) from exc
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    count = (
        len(payload.get("scores", [])) if isinstance(payload, dict) else len(payload)
    )
    logger.info("Wrote rollback snapshot for %s properties to %s", count, path)


def write_rows(rows: Iterable[Dict[str, Any]], path: str) -> None:
    """The bare-list form the backfills write."""
    write(list(rows), path)


def apply_rows(parsed_rows: Iterable[Dict[str, Any]]) -> Tuple[int, List[int]]:
    """Write already-parsed rows, returning (restored, ids that no longer exist).

    The caller commits. A property the snapshot names and the database no
    longer has is *reported*, never silently dropped.
    """
    restored = 0
    missing: List[int] = []
    for parsed in parsed_rows:
        prop = db.session.get(Property, parsed["id"])
        if prop is None:
            missing.append(parsed["id"])
            logger.warning("Property %s from snapshot no longer exists", parsed["id"])
            continue
        apply_row(prop, parsed)
        restored += 1
    return restored, missing


def restore_rows(raw_rows: Iterable[Dict[str, Any]]) -> Tuple[int, List[int]]:
    """Parse every row, then apply them — the whole file or none of it."""
    return apply_rows([parse_row(row) for row in raw_rows])


def restore_file(path: str) -> Tuple[int, List[int]]:
    """The backfills' `--restore`: read a snapshot file and put its rows back."""
    return apply_rows(load(path).rows)


def restore_profiles(profiles: Dict[int, Any]) -> Tuple[int, List[int]]:
    """Put `scoring_config` back on each profile, returning (restored, missing)."""
    restored = 0
    missing: List[int] = []
    for profile_id, config in profiles.items():
        profile = db.session.get(SearchProfile, profile_id)
        if profile is None:
            missing.append(profile_id)
            logger.warning("Profile %s from snapshot no longer exists", profile_id)
            continue
        profile.scoring_config = config
        restored += 1
    return restored, missing
