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
process before this one died mid-job, not that the job vanished.

Each of those writes happens from inside its own `app.app_context()`, so
Flask-SQLAlchemy's `db.session` -- a scoped session keyed to that context --
gives the worker thread and the Flask request thread separate sessions
automatically. Nothing here passes a `Session` across a thread boundary.
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from flask import current_app
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

_MAX_WORKERS = int(os.environ.get("BACKGROUND_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))

ACTIVE_STATUSES = ("queued", "running")

# Shown by the UI in place of "Job not found" (static/js/main.js pollJob).
INTERRUPTED_MESSAGE = "Interrupted by a redeploy before it finished. Run it again."


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
            if existing is not None:
                logger.info(
                    "Job type=%s dedupe_key=%s already in flight as %s; "
                    "not queuing a duplicate",
                    job_type,
                    dedupe_key,
                    existing.id,
                )
                return existing.id
            # The row that blocked us finished between the failed insert and
            # this read -- retry once rather than reporting a job that no
            # longer exists.
            _insert_queued_row(
                db, job_id, job_type=job_type, meta=meta, dedupe_key=dedupe_key
            )

    def _transition(_db, **fields) -> bool:
        """Best-effort status write. Returns False if it could not be made.

        Every caller below is already inside `except`/`else` handling of the
        job function itself; a second failure here (a dropped connection, a
        lock the database would not grant in time) must not propagate out of
        the ThreadPoolExecutor task, where nothing ever calls `.result()` on
        the Future to observe it -- that used to leave the row silently stuck
        `running` forever, indistinguishable from a job still in progress.
        """
        from models import BackgroundJob

        try:
            row = _db.session.get(BackgroundJob, job_id)
            if row is None:
                logger.warning("Job %s vanished before %s", job_id, fields)
                return False
            for key, value in fields.items():
                setattr(row, key, value)
            _db.session.commit()
            return True
        except Exception:
            logger.exception("Job %s failed while recording %s", job_id, fields)
            try:
                _db.session.rollback()
            except Exception:
                logger.exception(
                    "Job %s could not roll back after a failed write", job_id
                )
            return False

    def _run():
        with app_obj.app_context():
            from app import db as _db

            if not _transition(_db, status="running", started_at=_now()):
                return

            try:
                result = fn()
            except Exception as exc:
                _transition(_db, status="error", error=str(exc), finished_at=_now())
            else:
                _transition(_db, status="success", result=result, finished_at=_now())

    _EXECUTOR.submit(_run)
    return job_id


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
