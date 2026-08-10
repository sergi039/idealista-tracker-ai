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
empty. `reconcile_orphaned_jobs`, called once at application startup, is what
turns a row still `queued`/`running` into `interrupted` -- proof that the
process before this one died mid-job, not that the job vanished. That check
runs once, at startup, so it only catches a row orphaned by a process that
has since exited; a row whose own writer thread is stuck *within* a live
process is instead caught lazily, the next time something tries to reuse its
dedupe_key (see `_is_stale`, `enqueue_job`).

Each of those writes happens from inside its own `app.app_context()`, so
Flask-SQLAlchemy's `db.session` -- a scoped session keyed to that context --
gives the worker thread and the Flask request thread separate sessions
automatically. Nothing here passes a `Session` across a thread boundary.

Deployment here is a single gunicorn process (`--workers 1 --threads 4`,
Dockerfile). `reconcile_orphaned_jobs`'s "any active row belongs to a dead
process" assumption depends on that: with more than one worker process, a row
another live worker owns would look identical to one an actually-dead process
left behind, and reconciliation would need a process-owner predicate (a PID
or an instance id stamped on the row) to tell them apart. Out of scope here
because the deployment is single-worker today; flagged for whoever changes
that.
"""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from flask import current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_MAX_WORKERS = int(os.environ.get("BACKGROUND_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))

ACTIVE_STATUSES = ("queued", "running")

# Shown by the UI in place of "Job not found" (static/js/main.js pollJob).
INTERRUPTED_MESSAGE = "Interrupted by a redeploy before it finished. Run it again."

# How many times _write_status retries a failed commit against the normal
# (scoped) session before falling back to a fresh one. Small and fast: this
# is recovering from a transient failure (a dropped connection, a lock held
# a moment too long), not waiting out an outage.
_TRANSITION_MAX_ATTEMPTS = 3
_TRANSITION_RETRY_DELAY_S = 0.05

# A row with no update in this long is presumed abandoned -- its writer
# thread died in a way even _write_status's fresh-session fallback could not
# record (review of #190, blocker 1). Comfortably past the longest known job
# budget: static/js/main.js's JOB_POLL_TIMEOUTS.aiAnalysis is 660 s.
STALE_JOB_AFTER_SECONDS = int(
    os.environ.get("BACKGROUND_JOB_STALE_AFTER_SECONDS", str(30 * 60))
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


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


def _is_stale(row) -> bool:
    """No process is plausibly still working `row`.

    `started_at` is the better signal once a job has one; `created_at`
    covers a row whose own "mark running" write never landed (see
    `_write_status`). Both columns hold naive UTC values, matching
    `models.utcnow()` elsewhere -- compare against a naive "now" rather than
    risk a naive/aware subtraction across the two backends this runs
    against.
    """
    reference = row.started_at or row.created_at
    if reference is None:
        return False
    if reference.tzinfo is not None:
        reference = reference.astimezone(timezone.utc).replace(tzinfo=None)
    now = _now().replace(tzinfo=None)
    return (now - reference).total_seconds() > STALE_JOB_AFTER_SECONDS


def _insert_queued_row(db, job_id: str, *, job_type: str, meta, dedupe_key) -> None:
    from models import BackgroundJob

    db.session.add(
        BackgroundJob(
            id=job_id,
            job_type=job_type,
            status="queued",
            dedupe_key=dedupe_key,
            meta=meta or {},
        )
    )
    db.session.commit()


def _reap_stale_row(db, row) -> None:
    """Mark an abandoned active row `interrupted` so it stops blocking dedupe.

    Called with `row` bound to the caller's own session, already loaded --
    this only flips its status and commits, it does not requery.
    """
    logger.warning(
        "Job type=%s dedupe_key=%s has a stale %s row %s (no update in over "
        "%ds); marking it interrupted so a new job is not blocked forever",
        row.job_type,
        row.dedupe_key,
        row.status,
        row.id,
        STALE_JOB_AFTER_SECONDS,
    )
    row.status = "interrupted"
    row.error = (
        f"{INTERRUPTED_MESSAGE} (reaped as stale: no update in over "
        f"{STALE_JOB_AFTER_SECONDS}s)"
    )
    row.finished_at = _now()
    db.session.commit()


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
    one is active returns the id of the job already in flight instead of
    scheduling a duplicate -- caught from the database rejecting the insert,
    not from a Python check that a second concurrent request could still
    slip past between the check and the insert.

    If the row already holding that key is stale (`_is_stale`), it is
    reaped -- marked `interrupted` -- and a fresh job is queued instead of
    being handed back. Without this, a row whose writer thread could not
    record its own outcome (every `_write_status` attempt failed) would
    block that dedupe_key forever, indistinguishable from a job still
    genuinely in progress (#190 review, blocker 1).
    """
    from app import db

    app_obj = app or current_app._get_current_object()
    job_id = uuid.uuid4().hex

    with app_obj.app_context():
        try:
            _insert_queued_row(
                db, job_id, job_type=job_type, meta=meta, dedupe_key=dedupe_key
            )
        except IntegrityError:
            db.session.rollback()
            if dedupe_key is None:
                raise  # the collision was on the primary key, not dedupe_key
            from models import BackgroundJob

            existing = (
                db.session.query(BackgroundJob)
                .filter(
                    BackgroundJob.dedupe_key == dedupe_key,
                    BackgroundJob.status.in_(ACTIVE_STATUSES),
                )
                .order_by(BackgroundJob.created_at.desc())
                .first()
            )
            if existing is not None and not _is_stale(existing):
                logger.info(
                    "Job type=%s dedupe_key=%s already in flight as %s; "
                    "not queuing a duplicate",
                    job_type,
                    dedupe_key,
                    existing.id,
                )
                return existing.id

            if existing is not None:
                _reap_stale_row(db, existing)
            # The row that blocked us was reaped above, or finished between
            # the failed insert and this read (a benign race) -- either way,
            # retry the insert once.
            _insert_queued_row(
                db, job_id, job_type=job_type, meta=meta, dedupe_key=dedupe_key
            )

    _EXECUTOR.submit(_run_job, app_obj, job_id, fn)
    return job_id


def _try_write(session, job_id: str, fields: Dict[str, Any]) -> bool:
    """Apply `fields` to the job row and commit through `session`.

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
    at which point the row stays whatever it last was, and
    `enqueue_job`'s staleness check is what eventually frees its
    dedupe_key rather than leaving it blocked forever.
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
        "fallback; it will stay stuck until reaped as stale",
        job_id,
        fields,
        _TRANSITION_MAX_ATTEMPTS,
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
                # unreachable this fails too, and the row is picked up by
                # the staleness check the next time something tries to
                # reuse its dedupe_key.
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
    """Mark every row still `queued`/`running` as `interrupted`.

    Called once at application startup (app.py, before the app serves
    traffic), inside an app context. Any row in an active status at that
    point was written by a process that is not this one -- this process is
    only now starting -- so the status is dishonest until this corrects it.
    A single UPDATE...WHERE rather than a fetch-then-mutate loop: it is
    atomic on its own, and idempotent -- a second call, from a repeated boot
    or a second gunicorn worker, matches zero rows and updates nothing.

    Assumes a single worker process (see the module docstring): it has no
    way to tell "a row another live worker owns" from "a row an actually-dead
    process left behind" without a process-owner predicate, which the
    current single-worker deployment does not need.

    A no-op, rather than an error, when `background_jobs` does not exist yet.
    In production the migration entrypoint always creates it before this
    module is ever imported (see the module docstring), but most of this
    suite's test fixtures call `create_app()` and only build the schema with
    `db.create_all()` afterwards -- setting `app.config["TESTING"]` too late
    for this function to see it. There is nothing to reconcile before the
    table exists either way, so this checks the actual precondition instead
    of trying to infer "are we under test" from Flask config.
    """
    from sqlalchemy import inspect

    from app import db
    from models import BackgroundJob

    if not inspect(db.engine).has_table(BackgroundJob.__tablename__):
        return 0

    updated = (
        db.session.query(BackgroundJob)
        .filter(BackgroundJob.status.in_(ACTIVE_STATUSES))
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
