"""Background job execution, persisted in PostgreSQL (issue #176).

Jobs used to live only in a process-local dict driven by a ThreadPoolExecutor.
`tools/autopilot/deploy_watcher.sh` recreates the app container on every new
`main` -- as often as every 300 s -- so a job in flight at that moment was
abandoned: the process that owned it disappeared, `/api/jobs/<id>` answered
404, and an AI analysis that had already spent its provider quota produced no
stored result.

The `background_jobs` table (models.BackgroundJob) is now the single source
of truth: `enqueue_job` writes the row before handing work to the executor,
the worker thread updates it on start and on completion, and `get_job` always
reads the row back rather than a cache that a fresh process would start
empty.

Each of those writes happens from inside its own `app.app_context()`, so
Flask-SQLAlchemy's `db.session` -- a scoped session keyed to that context --
gives the worker thread and the Flask request thread separate sessions
automatically. Nothing here passes a `Session` across a thread boundary.

## The lease model (issue #176 PR #190, round-2 review)

"Is this row still owned by a live process?" used to be answered by
comparing the *reading process's own clock* against a `started_at`/
`created_at` timestamp, and `reconcile_orphaned_jobs` ran unconditionally on
every `create_app()`. An independent review rejected both: a process clock
that has drifted, or simply differs from the database server's, could
declare a genuinely live job dead; and any one-shot script that builds its
own `create_app()` (`utils/backfill_sea_view.py` and friends) while the web
process is still running would interrupt that process's own in-flight job
the instant it called `reconcile_orphaned_jobs`.

The fix is a lease, and the database's clock is the only clock that judges
it:

- `lease_expires_at` is set to the database's own `now() + LEASE_TTL_SECONDS`
  at enqueue, and renewed (same expression, same UPDATE) on every
  `_write_status` transition. It is written via a SQL expression
  (`_lease_expiry_expr`) that embeds `now()`/`CURRENT_TIMESTAMP`, never via
  a Python-computed `datetime.now() + timedelta(...)` -- so no process's
  clock, however skewed, can produce the value.
- A row counts as dead under exactly one predicate, applied nowhere but in
  SQL: `status IN ('queued', 'running') AND lease_expires_at < now()`. Both
  `reconcile_orphaned_jobs` (the startup/utility-script sweep) and
  `enqueue_job` (the dedupe-time reap) use this same predicate, expressed
  the same way, so "is it dead" never has two different answers depending
  on which code path asks.
- Reaping is an atomic compare-and-swap UPDATE
  (`_reap_expired_active_row`): `WHERE id = :id AND status = :seen_status
  AND lease_expires_at < now()`. Two concurrent callers racing the same
  expired row can only ever have one of them actually flip it -- the loser's
  UPDATE matches zero rows, which is information, not an error, and
  `enqueue_job` falls through to re-reading whichever row is now active
  (the winner's replacement, or someone else's) instead of raising.
- Because "dead" is judged entirely by the lease, `reconcile_orphaned_jobs`
  no longer needs "this is the process's first boot" to be true. It is safe
  to call from *any* `create_app()` -- the long-running web process at
  startup, or a one-shot utility script's own app instance while that web
  process is still alive -- because a row with a still-valid lease is never
  touched, no matter who calls it or how many times.

What this does *not* solve, honestly: if a job's own `_write_status` calls
fail (every retry, and the fresh-session fallback) so that neither `running`
nor a terminal status is ever recorded, that row is genuinely
indistinguishable from a live one until its lease expires. `LEASE_TTL_SECONDS`
is the bound on how long that can last -- not zero, because no lease system
can know a writer is dead before its lease says so, but bounded, self-healing,
and judged by one clock everywhere, which the old model was not.

Deployment here is a single gunicorn process (`--workers 1 --threads 4`,
Dockerfile) with multiple threads sharing one `_EXECUTOR`. The lease model
does not depend on that -- it already tolerates multiple independent
`create_app()` processes sharing one database, which is the harder case.
"""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from flask import current_app
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_MAX_WORKERS = int(os.environ.get("BACKGROUND_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))

ACTIVE_STATUSES = ("queued", "running")

# Shown by the UI in place of "Job not found" (static/js/main.js pollJob).
INTERRUPTED_MESSAGE = "Interrupted before it finished. Run it again."

# How many times _write_status retries a failed commit against the normal
# (scoped) session before falling back to a fresh one. Small and fast: this
# is recovering from a transient failure (a dropped connection, a lock held
# a moment too long), not waiting out an outage.
_TRANSITION_MAX_ATTEMPTS = 3
_TRANSITION_RETRY_DELAY_S = 0.05

# enqueue_job's own bound on insert/reap/retry cycles. Two concurrent callers
# racing one expired row settle in at most two rounds (see the module
# docstring); three leaves margin without risking a real bug looping forever.
_ENQUEUE_MAX_ATTEMPTS = 3

# How long a lease lasts past its last renewal before a row is presumed
# abandoned. The longest known job budget is the AI analysis call: up to
# 600 s server-side (services/property_ai_service.py), plus the client's own
# 60 s queueing allowance (static/js/main.js JOB_POLL_TIMEOUTS.aiAnalysis =
# 660000 ms). 900 s (15 min) leaves about 4 minutes of margin past that for
# scheduling jitter without leaving a genuinely dead row live for long.
LEASE_TTL_SECONDS = int(
    os.environ.get("BACKGROUND_JOB_LEASE_TTL_SECONDS", str(15 * 60))
)


def _now() -> datetime:
    """Wall-clock UTC, for informational timestamps only (finished_at on a
    terminal write, audit logging). Never used to decide whether a lease has
    expired -- see the module docstring; that decision is made in SQL,
    against the database's own now(), via `_now_expr`/`_lease_expiry_expr`.
    """
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _dialect_name(bind_or_session) -> str:
    """The SQL dialect a session/engine talks, defaulting to PostgreSQL --
    the only dialect this table is ever actually deployed against; SQLite is
    a test-only stand-in with no portable INTERVAL syntax of its own, which
    is the only reason this needs to branch at all."""
    try:
        return bind_or_session.get_bind().dialect.name
    except Exception:
        return "postgresql"


def _now_expr():
    """SQL expression for 'the database's own current time' -- portable
    across PostgreSQL and SQLite, used in every comparison that decides
    whether a lease has expired. Never evaluated in Python."""
    return func.current_timestamp()


def _lease_expiry_expr(dialect_name: str):
    """SQL expression for 'the database's own now(), plus LEASE_TTL_SECONDS'
    -- what gets written to `lease_expires_at`. PostgreSQL and SQLite have no
    shared syntax for timestamp-plus-interval, so this branches on dialect;
    every call site still only ever expresses "TTL seconds from the
    database's clock", never `datetime.now() + timedelta(...)`.
    """
    if dialect_name == "sqlite":
        return func.datetime(func.current_timestamp(), f"+{LEASE_TTL_SECONDS} seconds")
    return func.current_timestamp() + text(f"INTERVAL '{LEASE_TTL_SECONDS} seconds'")


def _row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "job_type": row.job_type,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "result": row.result,
        "error": row.error,
        "meta": row.meta or {},
    }


def _insert_queued_row(db, job_id: str, *, job_type: str, meta, dedupe_key) -> None:
    from models import BackgroundJob

    db.session.add(
        BackgroundJob(
            id=job_id,
            job_type=job_type,
            status="queued",
            dedupe_key=dedupe_key,
            meta=meta or {},
            lease_expires_at=_lease_expiry_expr(_dialect_name(db.session)),
        )
    )
    db.session.commit()


def _find_live_job_id(db, dedupe_key: str) -> Optional[str]:
    """id of the row genuinely, currently holding `dedupe_key` -- one whose
    lease has not expired by the database's own clock. None if no such row
    exists (either nothing is active for this key, or the one active row's
    lease has expired and should be reaped rather than handed back)."""
    from models import BackgroundJob

    row = (
        db.session.query(BackgroundJob.id)
        .filter(
            BackgroundJob.dedupe_key == dedupe_key,
            BackgroundJob.status.in_(ACTIVE_STATUSES),
            BackgroundJob.lease_expires_at.isnot(None),
            BackgroundJob.lease_expires_at >= _now_expr(),
        )
        .first()
    )
    return row[0] if row is not None else None


def _reap_expired_active_row(db, dedupe_key: str) -> None:
    """If an active row for `dedupe_key` has an expired (or missing) lease,
    atomically mark it `interrupted`.

    The UPDATE is a compare-and-swap: `WHERE id = :id AND status =
    :seen_status AND (lease NULL or expired)`. Two concurrent callers can
    both observe the same candidate row and both attempt this, but only one
    UPDATE can actually match it -- the loser's `rowcount` is 0, which this
    treats as "someone else handled it" rather than an error (#190 review
    round 2, findings 2 and 4). A no-op, not an error, when there is no
    active row for this key at all, or its lease is still valid (nothing to
    reap).
    """
    from models import BackgroundJob

    candidate = (
        db.session.query(BackgroundJob)
        .filter(
            BackgroundJob.dedupe_key == dedupe_key,
            BackgroundJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(BackgroundJob.created_at.desc())
        .first()
    )
    if candidate is None:
        return

    reaped = (
        db.session.query(BackgroundJob)
        .filter(
            BackgroundJob.id == candidate.id,
            BackgroundJob.status == candidate.status,
            (
                BackgroundJob.lease_expires_at.is_(None)
                | (BackgroundJob.lease_expires_at < _now_expr())
            ),
        )
        .update(
            {
                BackgroundJob.status: "interrupted",
                BackgroundJob.error: (
                    f"{INTERRUPTED_MESSAGE} (reaped: lease expired without "
                    f"renewal within the {LEASE_TTL_SECONDS}s TTL)"
                ),
                BackgroundJob.finished_at: _now(),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    if reaped:
        logger.warning(
            "Reaped expired lease on job %s (type=%s dedupe_key=%s)",
            candidate.id,
            candidate.job_type,
            dedupe_key,
        )


def enqueue_job(
    fn: Callable[[], Any],
    *,
    job_type: str,
    meta: Optional[Dict[str, Any]] = None,
    app=None,
    dedupe_key: Optional[str] = None,
) -> str:
    """Persist a queued job row and hand its execution to the thread pool.

    `dedupe_key`, when given, is enforced by the partial unique index
    `ux_background_jobs_active_dedupe_key` (migration 016), which only covers
    rows still `queued`/`running`. A second enqueue for the same key while
    one is genuinely active -- its lease has not expired -- returns the id
    of the job already in flight instead of scheduling a duplicate.

    "Genuinely active" is judged entirely by the database's own clock (see
    the module docstring's lease model), never by this process's. A row
    whose lease has expired is reaped -- via an atomic compare-and-swap, not
    an unconditional write -- and a fresh job queued in its place. Two
    concurrent callers racing the same expired row cannot both "win": the
    loser's own insert collides with whichever row is now live (the winner's
    replacement) and this falls through to returning *that* row's id rather
    than raising an IntegrityError at the caller (#190 review round 2,
    finding 4).
    """
    from app import db

    app_obj = app or current_app._get_current_object()

    with app_obj.app_context():
        job_id = None
        for attempt in range(1, _ENQUEUE_MAX_ATTEMPTS + 1):
            job_id = uuid.uuid4().hex
            try:
                _insert_queued_row(
                    db, job_id, job_type=job_type, meta=meta, dedupe_key=dedupe_key
                )
                break
            except IntegrityError:
                db.session.rollback()
                if dedupe_key is None:
                    raise  # the collision was on the primary key, not dedupe_key

                live_id = _find_live_job_id(db, dedupe_key)
                if live_id is not None:
                    logger.info(
                        "Job type=%s dedupe_key=%s already in flight as %s; "
                        "not queuing a duplicate",
                        job_type,
                        dedupe_key,
                        live_id,
                    )
                    return live_id

                # Nothing live is holding the key -- either its lease just
                # expired (reap it; if a concurrent caller reaps it first,
                # this attempt's UPDATE simply matches zero rows) or it
                # finished a moment ago (nothing to reap, benign). Either
                # way, retry the insert.
                _reap_expired_active_row(db, dedupe_key)
        else:
            raise RuntimeError(
                f"Could not enqueue job_type={job_type!r} dedupe_key={dedupe_key!r} "
                f"after {_ENQUEUE_MAX_ATTEMPTS} insert attempts"
            )

    _EXECUTOR.submit(_run_job, app_obj, job_id, fn)
    return job_id


def _try_write(session, job_id: str, fields: Dict[str, Any]) -> bool:
    """Apply `fields` to the job row, renew its lease, and commit through
    `session`.

    Returns False rather than raising on any failure -- missing row, a
    broken commit -- so the caller decides what to try next instead of a
    third failure (the rollback itself) escaping uncaught.
    """
    from models import BackgroundJob

    try:
        row = session.get(BackgroundJob, job_id)
        if row is None:
            logger.warning("Job %s vanished before %s", job_id, fields)
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        # Renewed on every transition, in the same UPDATE as the fields
        # above -- a worker that is still actively writing this row proves
        # it by extending its own lease each time (#190 review round 2).
        row.lease_expires_at = _lease_expiry_expr(_dialect_name(session))
        session.commit()
        return True
    except Exception:
        logger.exception("Job %s failed to record %s", job_id, fields)
        try:
            session.rollback()
        except Exception:
            logger.exception("Job %s could not roll back after a failed write", job_id)
        return False


def _write_status(app_obj, job_id: str, **fields) -> bool:
    """Persist a status transition, retrying, then falling back once to a
    fresh session, before giving up.

    Every caller is already inside `except`/`else` handling of the job
    function itself; a second failure here (a dropped connection, a lock the
    database would not grant in time) must not propagate out of the
    ThreadPoolExecutor task, where nothing ever calls `.result()` on the
    Future to observe it -- that used to leave the row silently stuck
    `running` forever, indistinguishable from a job still in progress (#190
    review, blocker 1).

    Bounded retry against the normal scoped session covers a transient
    failure. If every one of those still fails, one more attempt goes
    through a brand-new `Session` bound directly to the engine, in case
    whatever broke the scoped session's commits (a poisoned transaction, a
    stale identity map) is specific to it rather than to the database
    itself. Only if *that* also fails does this give up and return False --
    at which point the row keeps whatever lease it last had, and expires on
    schedule: `enqueue_job`'s reap check is what eventually frees its
    dedupe_key, bounded by `LEASE_TTL_SECONDS` rather than left blocked
    forever (#190 review round 2, finding 1).
    """
    from app import db as _db

    for attempt in range(1, _TRANSITION_MAX_ATTEMPTS + 1):
        if _try_write(_db.session, job_id, fields):
            return True
        if attempt < _TRANSITION_MAX_ATTEMPTS:
            time.sleep(_TRANSITION_RETRY_DELAY_S)

    try:
        with Session(bind=_db.engine) as fresh_session:
            if _try_write(fresh_session, job_id, fields):
                logger.warning(
                    "Job %s recorded %s only after falling back to a fresh session",
                    job_id,
                    fields,
                )
                return True
    except Exception:
        logger.exception("Job %s: opening a fresh session also failed", job_id)

    logger.critical(
        "Job %s could not record %s after %d attempt(s) plus a fresh-session "
        "fallback; its lease will simply expire in <= %ds and it will be "
        "reaped like any other abandoned row",
        job_id,
        fields,
        _TRANSITION_MAX_ATTEMPTS,
        LEASE_TTL_SECONDS,
    )
    return False


def _run_job(app_obj, job_id: str, fn: Callable[[], Any]) -> None:
    """Run `fn`, recording start/success/error against `job_id`.

    A module-level function rather than a closure over `enqueue_job`'s
    locals, so it can be called directly (synchronously, no executor) in
    tests that need to observe its exact recording behaviour.
    """
    with app_obj.app_context():
        if not _write_status(app_obj, job_id, status="running", started_at=_now()):
            return

        try:
            result = fn()
        except Exception as exc:
            _write_status(
                app_obj, job_id, status="error", error=str(exc), finished_at=_now()
            )
        else:
            if not _write_status(
                app_obj, job_id, status="success", result=result, finished_at=_now()
            ):
                # Every attempt to persist the result failed. Still try to
                # at least flag the row honestly instead of leaving it
                # silently "running" -- if the database is truly
                # unreachable this fails too, and the row's lease expires
                # on schedule regardless (see _write_status).
                _write_status(
                    app_obj,
                    job_id,
                    status="error",
                    finished_at=_now(),
                    error=(
                        "The job finished but its result could not be recorded "
                        "after repeated attempts. Run it again."
                    ),
                )


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    from app import db
    from models import BackgroundJob

    row = db.session.get(BackgroundJob, job_id)
    if row is None:
        return None
    return _row_to_dict(row)


def serialize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": job.get("id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "result": job.get("result"),
        "error": job.get("error"),
        "meta": job.get("meta") or {},
    }


def reconcile_orphaned_jobs() -> int:
    """Mark every row whose lease has expired -- `queued`/`running` and
    `lease_expires_at` in the past by the *database's* clock -- as
    `interrupted`.

    Called at application startup (app.py), inside an app context. Unlike
    the model this replaced, this is not "any active row belongs to a dead
    process": it only ever touches a row whose lease has actually run out,
    so it is safe to call from *any* `create_app()` -- the long-running web
    process at boot, or a one-shot utility script's own app instance built
    while that web process is still alive and holding a valid lease on its
    own job (#190 review round 2, finding 3). A repeated call -- a second
    boot, a second gunicorn worker, a utility script running alongside the
    web process -- matches only rows that are genuinely expired by then and
    updates nothing else.

    A no-op, rather than an error, when `background_jobs` does not exist
    yet. In production the migration entrypoint always creates it before
    this module is ever imported, but most of this suite's test fixtures
    call `create_app()` and only build the schema with `db.create_all()`
    afterwards -- setting `app.config["TESTING"]` too late for this function
    to see it. There is nothing to reconcile before the table exists either
    way, so this checks the actual precondition instead of trying to infer
    "are we under test" from Flask config.
    """
    from sqlalchemy import inspect

    from app import db
    from models import BackgroundJob

    if not inspect(db.engine).has_table(BackgroundJob.__tablename__):
        return 0

    updated = (
        db.session.query(BackgroundJob)
        .filter(
            BackgroundJob.status.in_(ACTIVE_STATUSES),
            (
                BackgroundJob.lease_expires_at.is_(None)
                | (BackgroundJob.lease_expires_at < _now_expr())
            ),
        )
        .update(
            {
                BackgroundJob.status: "interrupted",
                BackgroundJob.error: INTERRUPTED_MESSAGE,
                BackgroundJob.finished_at: _now(),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return updated
