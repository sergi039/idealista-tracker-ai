"""What a `sync_history` row may be shown as (#637).

`SyncHistory.status` defaults to `'completed'`, so a run that had just started,
a run still working through its slow tail and a run killed by a deploy all read
`completed` with `completed_at` NULL. Writers now create the row as `running`
(`STATUS_RUNNING`); this module is the one reader that turns a row into the
state to DISPLAY.

`interrupted` is a presentation state and is never written to the database, the
same pattern as `unchecked` in `services/listing_verification.py`: a row still
`running` (or the legacy `completed` with no `completed_at`) whose `started_at`
is older than `SYNC_RUN_STALE_AFTER` has no process behind it any more.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

STATUS_RUNNING = "running"

# The slow tail of a run (free enrichment after the mail is stored) can take
# hours, so a young `running` row is believed; past this age it is not.
SYNC_RUN_STALE_AFTER = timedelta(hours=6)

_FINISHED = {"completed", "partial", "failed"}


def _as_utc(value: datetime) -> datetime:
    # Naive datetimes stored in the table are UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def read_state(
    status: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    now: datetime,
) -> str:
    """Return running / completed / partial / failed / interrupted / unknown."""
    unfinished = status == STATUS_RUNNING or (
        status == "completed" and completed_at is None
    )
    if unfinished:
        # No start time means no evidence the run is alive.
        if started_at is None:
            return "interrupted"
        if _as_utc(now) - _as_utc(started_at) > SYNC_RUN_STALE_AFTER:
            return "interrupted"
        return STATUS_RUNNING
    if status in _FINISHED:
        return status
    return "unknown"


def display_state(row, now: datetime | None = None) -> str:
    """`read_state` for a `SyncHistory` row, with UTC now supplied here."""
    return read_state(
        row.status,
        row.started_at,
        row.completed_at,
        now if now is not None else datetime.now(timezone.utc),
    )
