"""Background job execution, persisted in PostgreSQL (issue #176).

Jobs used to live only in a process-local dict driven by a ThreadPoolExecutor.
`tools/autopilot/deploy_watcher.sh` recreates the app container on every new
`main` -- as often as every 300 s -- so a job in flight at that moment was
abandoned: the process that owned it disappeared, `/api/jobs/<id>` answered
404, and an AI analysis that had already spent its provider quota produced no
stored result.

The `background_jobs` table (models.BackgroundJob) is now the single source
of truth: `enqueue_job`/`run_job_sync` write the row before running work, the
worker updates it on start and on completion, and `get_job` always reads the
row back rather than a cache that a fresh process would start empty.

Each of those writes happens from inside its own `app.app_context()`, so
Flask-SQLAlchemy's `db.session` -- a scoped session keyed to that context --
gives the worker thread and the Flask request thread separate sessions
automatically. Nothing here passes a `Session` across a thread boundary.

## The lease model (PR #190, round-2 review)

"Is this row still owned by a live process?" used to be answered by
comparing the *reading process's own clock* against a `started_at`/
`created_at` timestamp, and `reconcile_orphaned_jobs` ran unconditionally on
every `create_app()`. That was rejected: a process clock that has drifted
could declare a genuinely live job dead, and a one-shot script building its
own `create_app()` while the web process is still running would interrupt
that process's own in-flight job.

The fix is a lease, and the database's clock is the only clock that judges
it: `lease_expires_at` is set to the database's own `now() + LEASE_TTL_SECONDS`
via a SQL expression (`_lease_expiry_expr`), never a Python-computed value.
A row counts as dead under exactly one predicate, applied only in SQL:
`status IN ('queued', 'running') AND lease_expires_at < now()`.
`reconcile_orphaned_jobs` and the dedupe-time reap both use it, so "is it
dead" never has two different answers depending on which code path asks.

## The heartbeat (round-3 review, finding 1)

A lease set once, at enqueue, is not enough: `_EXECUTOR` is a
`ThreadPoolExecutor` with a fixed worker count and an *unbounded* queue.
Enough ~600 s jobs ahead of one in the queue and it can outlive its own
`LEASE_TTL_SECONDS` before a worker ever picks it up -- at which point a
lease sweep (this process's own next `enqueue_job` call, or a different
process's `create_app()`) would reap it while its `Future` is still very
much alive, and queue a paid duplicate right behind it.

So the *owning process* renews every lease it holds, independent of whether
that job has started running yet. `_register_owned_job`/
`_unregister_owned_job` maintain a process-local set of ids -- added the
moment a row is claimed (`_acquire_job_slot`), removed the moment `_run_job`
is done with it, in *every* exit path (success, error, or losing ownership
mid-run). A single daemon thread (`_heartbeat_loop`), started lazily on the
first `enqueue_job`/`run_job_sync` call and living for the rest of the
process, wakes every `HEARTBEAT_INTERVAL_S` and renews every id currently in
that set with one `UPDATE ... WHERE id IN (...) AND status IN ('queued',
'running')`. A row this process no longer owns (already unregistered) is
never touched by it; a row belonging to a *different*, still-live process is
renewed by *that* process's own heartbeat, not this one's -- the sweep
predicate stays the single one described above either way.

## Compare-and-swap writes (round-3 review, finding 2)

Every status write is a CAS: `UPDATE ... WHERE id = :id AND status =
:expected_status`. A worker that is about to write can no longer assume it
still owns the row -- a lease sweep may have reaped it (interrupted) in the
window between its own last renewal and this write, however narrow the
heartbeat is meant to make that window. Zero rows matched means exactly
that: this worker has lost ownership, must not overwrite whatever the row
now says (an `interrupted` row is a terminal fact, not a race this worker
can still win), and discards its own result -- it no longer belongs to
anyone. `_try_write` returns `True` (committed), `False` (a transient
failure worth retrying), or `None` (lost ownership, stop -- retrying cannot
help and overwriting would be wrong).

## At most one execution per race (round-3 review, finding 3 / "F4-bis")

Two concurrent callers can still both observe the same expired row, and the
compare-and-swap above guarantees only one of them reaps it -- but that
alone is not enough. If the winner's replacement finishes *quickly* (a fast
job, or a mocked one in a test) before the loser's own retry runs, the
replacement is no longer active, the partial unique index no longer blocks
a new insert, and the loser's insert would succeed cleanly -- a second,
paid execution for work that already has an answer. `_acquire_job_slot`
closes this by remembering a *baseline* -- the newest row for this
dedupe_key at the moment this call started racing for it -- and checking,
before every insert attempt, whether anything newer than that baseline now
exists (`_find_superseding_row`), regardless of its current status. If so,
that row's id is returned instead of inserting a new one. The invariant
this guarantees: two concurrent `enqueue_job`/`run_job_sync` calls for the
same `dedupe_key` produce *at most one* new execution between them.

## The synchronous path (round-3 review, finding 4)

`?sync=1` and `TESTING` used to call the paid closure directly, bypassing
`background_jobs` -- and its dedupe_key protection -- entirely. `run_job_sync`
runs `fn` inline (this thread, this request) through the exact same
`_acquire_job_slot`/CAS lifecycle as the async path, so a sync call racing
a live async job (or another sync call) is refused rather than run twice:
it raises `JobAlreadyActive`, and the route answers 409 with the id of
whichever job already owns the key.

## Serializing the enqueue race (round-4 review, findings 1 and 2)

Round 3's baseline/supersession check closed the case where a competing
caller's replacement had already finished by the time a loser checked --
but a *check* is still not an *insert*, and nothing stopped another whole
race from playing out in the gap between them: caller B could pass both the
liveness and supersession checks, then caller A could reap the same
expired row, insert its own replacement, run it to completion, and only
*then* would B's own insert attempt run -- against a partial unique index
that, by then, had nothing active left to block it (finding 1). Separately,
`created_at` used to be a Python-side default (`default=utcnow` on the
model): two callers whose process clocks disagreed, or that simply landed
in the same millisecond, could each fail to recognize the other's row as
"newer" (finding 2).

`_dedupe_serialization` closes both by making the entire check -> reap ->
insert sequence for one `dedupe_key` a single atomic unit: a PostgreSQL
transaction-scoped advisory lock (`pg_advisory_xact_lock(hashtext(key))`),
taken as the first statement, so no other caller racing the *same* key can
observe this transaction's writes -- or make its own -- until it ends.
SQLite (tests only) stands this in with a process-local `threading.Lock`
per key. `created_at` is now a `server_default` (models.py), computed by
whichever database actually runs the INSERT, never by this process's own
clock; the baseline/supersession check from round 3 stays in place as
defense in depth, not as the primary guarantee any more.

## Domain writes and the terminal CAS share one transaction (round-4
## review, finding 4)

A job function (`fn`, one of the three AI-analysis closures in
routes/api_routes.py) used to commit its own domain writes -- an AI
analysis, a variant row -- independently of `_execute_job`'s own terminal
CAS write. If a reap raced ahead of a still-running `fn()` (an unusually
delayed heartbeat, an administrative intervention), `fn()`'s domain commit
could still land *after* the reap, overwriting whatever a legitimate
replacement job had already written for the same row -- even though the
reaped job's own terminal CAS write correctly failed and recorded nothing
extra.

`fn()` now only *stages* its domain writes (`db.session.add`/`.update()`,
never `.commit()`); `_execute_job` issues the terminal CAS UPDATE in that
same session and commits once, covering both. If the CAS matches zero rows
-- lost ownership, the same signal `_try_write` always used -- the whole
transaction rolls back, and `fn()`'s staged domain writes disappear with
it, exactly as if they had never run. `_upsert_property_ai_variant` and
`_upsert_land_ai_variant` (routes/api_routes.py) no longer commit either;
their own insert-vs-update race recovery runs inside a `SAVEPOINT`
(`Session.begin_nested()`), so a collision there rolls back only the
failed insert attempt, not the rest of what `fn()` staged.

## An ambiguous commit failure is not automatically a lost race (round-4
## review, finding 2)

`_try_write`'s own commit can raise *after* PostgreSQL has already
committed the write server-side -- a connection dropped on the way back,
not a failed transaction. The old code could not tell that apart from a
genuine failure: it returned `False`, a retry's own CAS then matched zero
rows (the write, after all, already happened), and that looked exactly
like "something else changed this row" -- lost ownership, `None`, stop.
For the `queued` -> `running` transition specifically, that meant a job
this process legitimately owned would never run: the async path left it
stuck until its lease expired, and the sync path answered 500 for work
that had, in fact, already started.

`_try_write` now treats *any* non-matching write -- whether from a raised
exception or a clean zero-row CAS -- as ambiguous rather than conclusive,
and asks the database itself to settle it (`_matches_our_own_write`,
through a brand-new session/connection, deliberately not the one whose
commit is in question): if the row's status is already exactly the value
this write was trying to record, with a lease that has not expired,
nothing else could plausibly have written that -- only this module's own
CAS writes ever set a job's status, and a reaper always writes a different
one (`interrupted`) -- so it must have been this write, or an earlier
attempt of it, landing invisibly. Only when the row shows something else
has ownership genuinely been lost.

## The insert-commit's own ambiguity (round-5 review)

Round 4's `_matches_our_own_write` disambiguates a *status-transition*
commit, in `_try_write` -- but `_acquire_job_slot`'s own INSERT of a fresh
`queued` row commits separately, through a plain `db.session.commit()`
this module's own `except IntegrityError:` branch was the only thing
catching. PostgreSQL can commit that INSERT server-side and still fail to
acknowledge it (the same dropped-connection shape as round 4, finding 2),
which raises something other than `IntegrityError` -- nothing violated a
constraint -- straight out of `_acquire_job_slot`, before
`enqueue_job`/`run_job_sync` ever registers ownership or dispatches
anything. The row lands as an orphaned `queued` insert nobody is running:
a retry's own liveness check (`_find_live_job_id`) finds it, defers to its
id (or answers 409 on the sync path), and nothing ever calls `fn()` for it
-- stuck until its lease expires and it is reaped.

`_matches_our_own_insert` mirrors `_matches_our_own_write` for this one
narrower case: on any exception from the insert's own commit other than
`IntegrityError`, a fresh session/connection re-reads the row by the id
this attempt generated for itself immediately before the insert (a fresh
`uuid.uuid4().hex`, so nothing else could plausibly hold it). `queued`
status with a live lease means the insert landed and only its
acknowledgement was lost -- `_acquire_job_slot` continues exactly as if
the commit had returned normally, so the caller still registers ownership
and dispatches `fn()`. No row under that id at all means the insert
genuinely never happened, and the original exception propagates.

## What this does not solve, honestly

If a job's own writes fail (every retry, and the fresh-session fallback) so
that neither `running` nor a terminal status is ever recorded, that row is
genuinely indistinguishable from a live one until its lease expires --
`LEASE_TTL_SECONDS` is the bound on how long that can last, not zero,
because no lease system can know a writer is dead before its lease says so.
The one exception is the combined domain-write/terminal-CAS commit
(`_finalize_success`): if *that* commit truly fails (not just loses its
acknowledgement -- see above), `fn()`'s already-computed result cannot be
recovered by retrying, because retrying would redo only the status column,
not the domain writes nothing tracks outside that one lost transaction. The
job is honestly recorded `error` instead of silently claiming `success`
over data that was never written.
"""

import contextlib
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

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

# _acquire_job_slot's own bound on insert/reap/retry cycles. Two concurrent
# callers racing one expired row settle in at most two rounds (see the module
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

# How often the owning process's heartbeat renews the leases of jobs it
# still holds -- comfortably smaller than LEASE_TTL_SECONDS so a live job
# never gets close to its own deadline.
HEARTBEAT_INTERVAL_S = int(os.environ.get("BACKGROUND_JOB_HEARTBEAT_INTERVAL_S", "60"))


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


# --- The heartbeat: renews every lease this process still owns -----------

_owned_job_ids: set = set()
_owned_job_ids_lock = threading.Lock()
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_thread_lock = threading.Lock()
_heartbeat_app = None  # the Flask app the heartbeat thread pushes contexts on


def _register_owned_job(job_id: str) -> None:
    with _owned_job_ids_lock:
        _owned_job_ids.add(job_id)


def _unregister_owned_job(job_id: str) -> None:
    with _owned_job_ids_lock:
        _owned_job_ids.discard(job_id)


def _renew_owned_leases(app_obj) -> int:
    """One UPDATE renewing the lease of every job this process still owns
    and that is still active. Called periodically by the heartbeat thread,
    and directly by tests to simulate a tick without waiting
    HEARTBEAT_INTERVAL_S seconds. A no-op (no query at all) when this
    process owns nothing right now.
    """
    with _owned_job_ids_lock:
        ids = list(_owned_job_ids)
    if not ids:
        return 0

    from app import db
    from models import BackgroundJob

    with app_obj.app_context():
        renewed = (
            db.session.query(BackgroundJob)
            .filter(
                BackgroundJob.id.in_(ids), BackgroundJob.status.in_(ACTIVE_STATUSES)
            )
            .update(
                {"lease_expires_at": _lease_expiry_expr(_dialect_name(db.session))},
                synchronize_session=False,
            )
        )
        db.session.commit()
    return renewed


def _heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_INTERVAL_S)
        app_obj = _heartbeat_app
        if app_obj is None:
            continue
        try:
            _renew_owned_leases(app_obj)
        except Exception:
            logger.exception("Background job heartbeat renewal failed")


def _ensure_heartbeat_started(app_obj) -> None:
    """Starts the one heartbeat daemon thread this process will ever have,
    on the first call. Every call -- not just the first -- updates which
    Flask app the thread pushes contexts against, so a later `enqueue_job`
    against a different app instance (a fresh test, in practice; production
    only ever has one) is what the next tick actually renews leases in,
    even though the thread itself started long before.
    """
    global _heartbeat_thread, _heartbeat_app
    _heartbeat_app = app_obj
    with _heartbeat_thread_lock:
        if _heartbeat_thread is None or not _heartbeat_thread.is_alive():
            _heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, daemon=True, name="background-jobs-heartbeat"
            )
            _heartbeat_thread.start()


# --- Serializing the enqueue-time check -> reap -> insert sequence -------
# (#190 review round 4, findings 1 and 2 -- see the module docstring)

_sqlite_dedupe_locks: Dict[str, threading.Lock] = {}
_sqlite_dedupe_locks_guard = threading.Lock()


def _sqlite_dedupe_lock(dedupe_key: str) -> threading.Lock:
    """A process-local lock per `dedupe_key`, standing in for PostgreSQL's
    transactional advisory lock on SQLite -- tests only; this table's only
    real deployment target is PostgreSQL, which is what actually needs the
    lock. Created once per key, on demand; the registry is small and
    bounded in practice (one entry per unit of work this process has ever
    raced to enqueue), so it is never pruned.
    """
    with _sqlite_dedupe_locks_guard:
        lock = _sqlite_dedupe_locks.get(dedupe_key)
        if lock is None:
            lock = threading.Lock()
            _sqlite_dedupe_locks[dedupe_key] = lock
        return lock


@contextlib.contextmanager
def _dedupe_serialization(db, dedupe_key: Optional[str]):
    """Serializes the entire check -> reap -> insert sequence in
    `_acquire_job_slot` for one `dedupe_key`, so no other caller racing the
    *same* key can observe -- or create -- anything in between (see the
    module docstring's "Serializing the enqueue race" section for why a
    check-then-insert, even with round 3's baseline/supersession guard, was
    not enough on its own).

    PostgreSQL: `pg_advisory_xact_lock`, a transaction-scoped advisory
    lock, taken as the very first statement of the transaction. SQLite
    (tests only) has no advisory locks, so a process-local `threading.Lock`
    per key stands in.

    `dedupe_key=None` needs no lock: nothing can ever collide with a job
    that was never asked to deduplicate against anything.
    """
    if dedupe_key is None:
        yield
        return

    dialect = _dialect_name(db.session)
    if dialect == "sqlite":
        lock = _sqlite_dedupe_lock(dedupe_key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
        return

    db.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": dedupe_key}
    )
    try:
        yield
    finally:
        # pg_advisory_xact_lock releases automatically at the transaction's
        # end -- but only once that end actually happens. Every path out of
        # the `with` block above (an early return after a read-only check,
        # a successful insert's own commit, or a caught IntegrityError's
        # own rollback) must leave no transaction open here, or the lock --
        # and the connection -- stays held for however long this caller
        # keeps going before its next database call. rollback() is always
        # a safe "close whatever is still open" in that role: it discards
        # nothing when the block already committed or rolled back for
        # itself, since there is nothing left pending by then.
        db.session.rollback()


# --- Claiming a dedupe_key: shared by the async and sync paths -----------


def _insert_queued_row(db, job_id: str, *, job_type: str, meta, dedupe_key) -> None:
    """Stages a fresh `queued` row -- does not commit. Called from inside
    `_acquire_job_slot`'s own `_dedupe_serialization` block, which commits
    once, together with whatever `_reap_expired_active_row` staged in the
    same attempt (#190 review round 4, finding 1)."""
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


def _baseline_snapshot(db, dedupe_key: str) -> Tuple[Optional[str], Optional[datetime]]:
    """(id, created_at) of the current newest row for `dedupe_key`, or
    (None, None) if nothing exists yet for it. Captured once, at the start
    of a caller's own attempt to claim the key -- the reference point
    `_find_superseding_row` later compares against to detect a competing
    caller's replacement, including one that has *already finished* by the
    time this caller checks (#190 review round 3, finding 3)."""
    from models import BackgroundJob

    row = (
        db.session.query(BackgroundJob.id, BackgroundJob.created_at)
        .filter(BackgroundJob.dedupe_key == dedupe_key)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
        .first()
    )
    return (row[0], row[1]) if row is not None else (None, None)


def _find_superseding_row(
    db, dedupe_key: str, baseline_created_at: Optional[datetime]
) -> Optional[str]:
    """id of the newest row for `dedupe_key` created strictly after
    `baseline_created_at` -- i.e. something a competing caller created
    since this call started racing for the key, whatever its status is now.
    None if nothing has appeared since the baseline."""
    from models import BackgroundJob

    query = db.session.query(BackgroundJob.id).filter(
        BackgroundJob.dedupe_key == dedupe_key
    )
    if baseline_created_at is not None:
        query = query.filter(BackgroundJob.created_at > baseline_created_at)
    row = query.order_by(
        BackgroundJob.created_at.desc(), BackgroundJob.id.desc()
    ).first()
    return row[0] if row is not None else None


def _reap_expired_active_row(db, dedupe_key: str) -> None:
    """If an active row for `dedupe_key` has an expired (or missing) lease,
    stage marking it `interrupted` -- does not commit.

    Called from inside `_acquire_job_slot`'s own `_dedupe_serialization`
    block (#190 review round 4, finding 1): every caller racing this
    `dedupe_key` is already serialized by that lock, so the CAS predicate
    below (`WHERE id = :id AND status = :seen_status AND (lease NULL or
    expired)`) exists to guard against a *different* kind of concurrent
    writer this lock does not cover -- the row's own owning worker, still
    genuinely alive and renewing its lease or recording its own outcome
    through `_write_status`, in a session this function never touches. A
    no-op, not an error, when there is no active row for this key at all,
    or its lease is still valid (nothing to reap). The caller (
    `_acquire_job_slot`) commits this together with the insert that
    follows it, as one atomic unit.
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
    if reaped:
        logger.warning(
            "Reaped expired lease on job %s (type=%s dedupe_key=%s)",
            candidate.id,
            candidate.job_type,
            dedupe_key,
        )


def _matches_our_own_insert(job_id: str) -> bool:
    """True if `job_id` -- the id `_acquire_job_slot` just tried to insert
    as a fresh `queued` row -- already exists with status `queued` and a
    lease that has not expired, read through a brand-new session/
    connection.

    The insert-path counterpart to `_matches_our_own_write` (#190 review
    round 5): PostgreSQL can commit an INSERT server-side and still fail to
    acknowledge it to this process (a connection dropped on the way back),
    which surfaces here as an exception from `db.session.commit()` that
    looks, on its face, identical to a transaction that never landed at
    all -- except this one is not even an `IntegrityError`, since nothing
    violated a constraint; the earlier round-4 fix only ever handled that
    one exception type. `job_id` is a fresh `uuid.uuid4().hex` this call
    generated for itself immediately before the insert, so nothing else
    could plausibly have created a row under that exact id -- an existing
    `queued` row with a live lease has no explanation other than "this
    insert, whose commit raised anyway, actually landed".
    """
    from app import db as _db
    from models import BackgroundJob

    try:
        with Session(bind=_db.engine) as fresh_session:
            row = (
                fresh_session.query(BackgroundJob.id)
                .filter(
                    BackgroundJob.id == job_id,
                    BackgroundJob.status == "queued",
                    BackgroundJob.lease_expires_at.isnot(None),
                    BackgroundJob.lease_expires_at >= _now_expr(),
                )
                .first()
            )
            return row is not None
    except Exception:
        logger.exception(
            "job_id=%s: re-read to disambiguate an ambiguous insert commit also failed",
            job_id,
        )
        return False


def _acquire_job_slot(
    db, *, job_type: str, meta: Optional[Dict[str, Any]], dedupe_key: Optional[str]
) -> Tuple[str, bool]:
    """Either claims a fresh row for a new job, or identifies the row a
    concurrent caller already claims (or already finished claiming) for the
    same `dedupe_key`.

    Returns `(job_id, is_new)`. When `is_new` is False, the caller must not
    run anything -- `job_id` already belongs (still active, or terminal) to
    someone else, and running again would pay for the same work twice.
    Shared by `enqueue_job` (async) and `run_job_sync` (`?sync=1`,
    `TESTING`) so both obey the same invariant: at most one new execution
    per race for a `dedupe_key` (#190 review round 3, findings 3 and 4).

    Every attempt below runs inside `_dedupe_serialization`: the liveness
    check, the supersession check, the reap of an expired-but-still-active
    row, and the insert are one atomic unit per `dedupe_key`, closing the
    gap round 3's checks alone left open (#190 review round 4, finding 1 --
    see the module docstring). The baseline/supersession check from round 3
    is kept as defense in depth, not as the primary guarantee any more.
    """
    baseline_id, baseline_created_at = (
        _baseline_snapshot(db, dedupe_key) if dedupe_key is not None else (None, None)
    )
    del baseline_id  # only the timestamp matters to _find_superseding_row

    job_id = None
    for _attempt in range(1, _ENQUEUE_MAX_ATTEMPTS + 1):
        with _dedupe_serialization(db, dedupe_key):
            if dedupe_key is not None:
                live_id = _find_live_job_id(db, dedupe_key)
                if live_id is not None:
                    return live_id, False

                superseding_id = _find_superseding_row(
                    db, dedupe_key, baseline_created_at
                )
                if superseding_id is not None:
                    return superseding_id, False

                # Nothing live, nothing superseding -- so the partial
                # unique index can only still be holding this key with an
                # ACTIVE row whose lease has expired (a genuinely dead
                # worker that was never reaped). Reap it now, still inside
                # this key's lock: no concurrent caller for the *same* key
                # can be running this section at the same time, so the
                # reap and the insert that follows share one commit as a
                # single atomic unit.
                _reap_expired_active_row(db, dedupe_key)

            job_id = uuid.uuid4().hex
            _insert_queued_row(
                db, job_id, job_type=job_type, meta=meta, dedupe_key=dedupe_key
            )
            try:
                db.session.commit()
                return job_id, True
            except IntegrityError:
                db.session.rollback()
                if dedupe_key is None:
                    raise  # the collision was on the primary key, not dedupe_key
                # Should not happen while every caller for this key is
                # serialized behind the same lock -- this is defense in
                # depth against a hashtext() collision between two
                # *different* keys sharing a lock id, not the primary
                # guarantee -- so loop back, take the lock fresh, and
                # re-check rather than assuming what happened.
            except Exception:
                # The INSERT may have actually committed server-side --
                # this exception could be a lost acknowledgement (a
                # connection dropped right after PostgreSQL committed it),
                # not a failed transaction (#190 review round 5). Unlike
                # the IntegrityError above, nothing here is conclusive on
                # its own: a genuinely failed insert and an
                # acknowledgement lost after a real commit raise the same
                # way. Resolved the same way `_try_write` resolves an
                # ambiguous status-write failure -- re-read, through a
                # brand-new session, by the id this attempt generated for
                # itself immediately before the insert.
                try:
                    db.session.rollback()
                except Exception:
                    logger.exception(
                        "job_id=%s: could not roll back after an ambiguous "
                        "insert-commit failure",
                        job_id,
                    )
                if _matches_our_own_insert(job_id):
                    logger.warning(
                        "job_id=%s: insert already landed under an earlier "
                        "attempt whose commit acknowledgement was lost; "
                        "continuing as if it had succeeded",
                        job_id,
                    )
                    return job_id, True
                raise

    raise RuntimeError(
        f"Could not acquire a job slot for job_type={job_type!r} "
        f"dedupe_key={dedupe_key!r} after {_ENQUEUE_MAX_ATTEMPTS} attempts"
    )


class JobAlreadyActive(Exception):
    """Raised by `run_job_sync` when `dedupe_key` is already claimed by
    another job -- live, or superseded by a competing caller's replacement.
    Callers (routes) catch this and answer 409 with `.job_id`, so the client
    polls the job that already owns the key instead of paying for a second
    execution (#190 review round 3, finding 4)."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"job {job_id} already claims this dedupe_key")


def enqueue_job(
    fn: Callable[[], Any],
    *,
    job_type: str,
    meta: Optional[Dict[str, Any]] = None,
    app=None,
    dedupe_key: Optional[str] = None,
) -> str:
    """Persist a queued job row and hand its execution to the thread pool.

    See the module docstring for the full model. In short: `dedupe_key`,
    when given, is enforced by the partial unique index
    `ux_background_jobs_active_dedupe_key` plus `_acquire_job_slot`'s own
    checks, so a second enqueue for a key already claimed -- live, or
    superseded by a just-finished replacement -- returns that job's id
    instead of scheduling a duplicate.
    """
    from app import db

    app_obj = app or current_app._get_current_object()

    with app_obj.app_context():
        job_id, is_new = _acquire_job_slot(
            db, job_type=job_type, meta=meta, dedupe_key=dedupe_key
        )
        if not is_new:
            logger.info(
                "Job type=%s dedupe_key=%s already claimed by %s; not queuing "
                "a duplicate",
                job_type,
                dedupe_key,
                job_id,
            )
            return job_id

    _register_owned_job(job_id)
    _ensure_heartbeat_started(app_obj)
    _EXECUTOR.submit(_run_job, app_obj, job_id, fn)
    return job_id


def run_job_sync(
    fn: Callable[[], Any],
    *,
    job_type: str,
    meta: Optional[Dict[str, Any]] = None,
    app=None,
    dedupe_key: Optional[str] = None,
) -> str:
    """Run `fn` inline (this thread, this request/call) through the same
    registry and CAS lifecycle `enqueue_job` uses for the async path --
    for `?sync=1` and `TESTING`, which used to call the paid closure
    directly, bypassing `background_jobs` (and its dedupe_key protection)
    entirely (#190 review round 3, finding 4).

    Unlike `enqueue_job`, this does not push its own app context. It must
    be called from within one already -- every route handler is -- and
    deliberately reuses that same context/session throughout: claiming the
    job slot, running `fn`, and recording its outcome all stay in the
    caller's own session, exactly like the pre-#190 sync path did. Pushing
    a fresh context here (the way the async worker thread must, since it
    starts with none of its own) would give `fn` a *different* session
    than the route's -- if the route (or a test) reads back what `fn` just
    wrote through its own, unrelated session afterward, Flask-SQLAlchemy's
    per-session identity map can still show the pre-write value, the same
    class of bug blocker 2 fixed for the land/Claude route.

    Raises `JobAlreadyActive` when `dedupe_key` is already claimed by
    another job; the caller (a route) should answer 409 with its id rather
    than running a second, paid execution.
    """
    from app import db

    app_obj = app or current_app._get_current_object()

    job_id, is_new = _acquire_job_slot(
        db, job_type=job_type, meta=meta, dedupe_key=dedupe_key
    )
    if not is_new:
        raise JobAlreadyActive(job_id)

    _register_owned_job(job_id)
    _ensure_heartbeat_started(app_obj)
    _execute_job(job_id, fn)
    return job_id


def _matches_our_own_write(job_id: str, fields: Dict[str, Any]) -> bool:
    """True if `job_id`'s row, read through a brand-new session/connection,
    already shows exactly the status a write was trying to record, with a
    lease that has not expired.

    The disambiguation `_try_write` needs for #190 review round 4, finding
    2: a CAS that matches zero rows, or a commit that raised, is genuinely
    ambiguous on its own -- it could mean this caller lost ownership to a
    reaper, or it could mean the write actually landed and only its
    acknowledgement was lost (a connection dropped on the way back, after
    PostgreSQL had already committed). A fresh session/connection is
    deliberate: the one whose commit is in question is exactly the session
    whose view of the world might be unreliable here. Only this module's
    own CAS writes ever set a job's status at all, and a reaper always
    writes a different one (`interrupted`) -- so an exact match, with a
    live lease, has no other explanation than "this write, or an earlier
    attempt of it, landed".
    """
    from app import db as _db
    from models import BackgroundJob

    target_status = fields.get("status")
    if target_status is None:
        return False
    try:
        with Session(bind=_db.engine) as fresh_session:
            row = (
                fresh_session.query(BackgroundJob.id)
                .filter(
                    BackgroundJob.id == job_id,
                    BackgroundJob.status == target_status,
                    BackgroundJob.lease_expires_at.isnot(None),
                    BackgroundJob.lease_expires_at >= _now_expr(),
                )
                .first()
            )
            return row is not None
    except Exception:
        logger.exception(
            "Job %s: re-read to disambiguate an ambiguous write also failed", job_id
        )
        return False


def _try_write(
    session, job_id: str, expected_status: str, fields: Dict[str, Any]
) -> Optional[bool]:
    """Compare-and-swap: `UPDATE ... WHERE id = :id AND status =
    :expected_status`, renewing the lease in the same statement.

    Returns `True` if the CAS matched and committed (or, per below, is
    confirmed to have already landed), `False` if the write itself failed
    and is worth retrying (a dropped connection, a lock held too long), or
    `None` if the row's status was not `expected_status` -- something else
    (a reaper) already changed it, and this caller has genuinely lost
    ownership. `None` is never retried and never overwritten: an
    `interrupted` row (or any other status a CAS miss reveals) is a
    terminal fact this worker no longer has standing to contest (#190
    review round 3, finding 2).

    A CAS miss and a commit exception are both routed through
    `_matches_our_own_write` before being treated as a loss (#190 review
    round 4, finding 2): if the row already shows exactly what this write
    was trying to record, an earlier attempt's own commit landed and only
    its acknowledgement was lost -- not a lost race. Only when the row
    genuinely shows something else is ownership treated as lost.

    On a miss (whether disambiguated to `True` or left as `None`), this
    also rolls back whatever else was staged in `session` -- a job
    function's own domain writes, when this is the combined success write
    -- rather than committing them anyway just because the CAS predicate
    happened to match zero rows (#190 review round 4, finding 4): a lost
    race must not let a `fn()` that is no longer this row's owner still
    land its own domain data.
    """
    from models import BackgroundJob

    try:
        full_fields = dict(fields)
        full_fields["lease_expires_at"] = _lease_expiry_expr(_dialect_name(session))
        updated = (
            session.query(BackgroundJob)
            .filter(BackgroundJob.id == job_id, BackgroundJob.status == expected_status)
            .update(full_fields, synchronize_session=False)
        )
        if not updated:
            session.rollback()
            if _matches_our_own_write(job_id, fields):
                logger.warning(
                    "Job %s: %s already landed under an earlier attempt whose "
                    "acknowledgement was lost; treating it as successful",
                    job_id,
                    fields,
                )
                return True
            logger.warning(
                "Job %s lost ownership before recording %s (expected status=%r)",
                job_id,
                fields,
                expected_status,
            )
            return None

        session.commit()
        return True
    except Exception:
        logger.exception("Job %s failed to record %s", job_id, fields)
        try:
            session.rollback()
        except Exception:
            logger.exception("Job %s could not roll back after a failed write", job_id)
            return False
        if _matches_our_own_write(job_id, fields):
            logger.warning(
                "Job %s: %s already landed despite a commit exception; "
                "treating it as successful",
                job_id,
                fields,
            )
            return True
        return False


def _write_status(job_id: str, *, expected_status: str, **fields) -> Optional[bool]:
    """Persist a CAS status transition, retrying transient failures, then
    falling back once to a fresh session, before giving up.

    Relies on `db.session` resolving to whichever app context is currently
    active -- it does not push one of its own. `_execute_job`'s caller is
    responsible for that: `_run_job` (the async entry point) pushes a fresh
    one, `run_job_sync` deliberately reuses the caller's own.

    Returns the same tri-state as `_try_write`: `True` (committed), `False`
    (every attempt failed transiently), or `None` (lost ownership -- a
    reaper won the row before this write could land). `None` is returned
    immediately, without retrying: a lost CAS cannot be won back by trying
    again, and retrying would risk overwriting whatever the row now
    honestly says.

    Every caller is already inside `except`/`else` handling of the job
    function itself; a second failure here must not propagate out of the
    ThreadPoolExecutor task, where nothing ever calls `.result()` on the
    Future to observe it -- that used to leave the row silently stuck
    `running` forever. Bounded retry against the normal scoped session
    covers a transient failure; the fresh-session fallback covers a broken
    scoped session specifically. If even that fails, the row keeps whatever
    lease it last had and expires on schedule -- `LEASE_TTL_SECONDS` bounds
    how long that can last, not zero.

    Used for the status-only transitions (`queued` -> `running`, and any
    write recording `error`) -- retrying, and falling back to a fresh
    session, is safe for these because there is nothing else staged in the
    session that a rollback or a session swap could silently drop. The
    combined success write, which *does* have a job function's own staged
    domain writes riding along, uses `_finalize_success` instead -- see its
    own docstring for why it deliberately does not retry.
    """
    from app import db as _db

    for attempt in range(1, _TRANSITION_MAX_ATTEMPTS + 1):
        outcome = _try_write(_db.session, job_id, expected_status, fields)
        if outcome is True:
            return True
        if outcome is None:
            return None
        if attempt < _TRANSITION_MAX_ATTEMPTS:
            time.sleep(_TRANSITION_RETRY_DELAY_S)

    try:
        with Session(bind=_db.engine) as fresh_session:
            outcome = _try_write(fresh_session, job_id, expected_status, fields)
            if outcome is True:
                logger.warning(
                    "Job %s recorded %s only after falling back to a fresh session",
                    job_id,
                    fields,
                )
                return True
            if outcome is None:
                return None
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


def _finalize_success(job_id: str, result: Any) -> Optional[bool]:
    """Records a job's success in the *same* transaction as whatever `fn()`
    staged without committing -- an AI analysis, a variant row -- and
    commits once, covering both (#190 review round 4, finding 4; see the
    module docstring's "Domain writes and the terminal CAS share one
    transaction" section).

    Deliberately does not retry and has no fresh-session fallback the way
    `_write_status` does: a failed commit here means rolling back to keep
    the CAS atomic with `fn()`'s staged writes (`_try_write` already does
    this on any miss), and retrying could only redo the status column --
    `fn()`'s own staged changes are gone the instant that rollback runs,
    nothing tracks them to redo, and a "successful" retry that quietly
    skipped them would be a worse bug than the one this fixes: a job
    reporting success over domain data that was never written.
    `_try_write`'s own ambiguity check (round 4, finding 2) already covers
    the one case a retry would otherwise have been needed for -- a commit
    that landed but whose acknowledgement was lost.
    """
    from app import db as _db

    return _try_write(
        _db.session,
        job_id,
        expected_status="running",
        fields={"status": "success", "result": result, "finished_at": _now()},
    )


def _execute_job(job_id: str, fn: Callable[[], Any]) -> None:
    """Run `fn`, recording start/success/error against `job_id` via CAS
    transitions, and unregistering `job_id` from the heartbeat's owned set
    on every exit path.

    Assumes an app context is already active -- `db.session` (inside
    `_write_status`/`_finalize_success`) resolves to whichever one is
    current; this does not push one of its own. That is deliberate:
    `run_job_sync` calls this directly, still inside the *caller's* (a
    route handler's) own app context, so `fn`'s reads/writes land in the
    same session the route itself uses -- the same class of bug blocker 2
    fixed for the land/Claude route (a mutation committed through a
    *different* session than the one a request goes on to read from), just
    at the enqueue/run-inline boundary instead of inside one route's own
    closure. `_run_job` is the async entry point that pushes a fresh
    context, since a worker thread starts with none of its own.
    """
    from app import db as _db

    try:
        outcome = _write_status(
            job_id, expected_status="queued", status="running", started_at=_now()
        )
        if outcome is not True:
            # Lost ownership before we even started (None), or could not
            # confirm we own it (False) -- either way, the (often paid)
            # work must not run.
            return

        try:
            result = fn()
        except Exception as exc:
            # This used to reach a route's own `except Exception:` handler
            # (which logs with a traceback) for the sync path, before
            # run_job_sync routed it through here too (#190 review round 3,
            # finding 4). Log it here so that stays true for both paths --
            # the DB row only gets str(exc).
            logger.exception("Job %s: fn() raised", job_id)
            # Whatever fn() staged before raising -- a partial domain write
            # -- must not reach the database either: the same principle
            # finding 4 established for the success path applies just as
            # much to a failure. This also leaves a known-good transaction
            # state for the error write that follows, rather than one
            # PostgreSQL may already have aborted.
            try:
                _db.session.rollback()
            except Exception:
                logger.exception(
                    "Job %s: rolling back after fn() raised also failed", job_id
                )
            _write_status(
                job_id,
                expected_status="running",
                status="error",
                error=str(exc),
                finished_at=_now(),
            )
        else:
            outcome = _finalize_success(job_id, result)
            if outcome is False:
                # A transient failure, not a lost race (that's `None`,
                # handled by simply not writing again below) -- still try
                # once more to at least flag failure honestly rather than
                # leaving the row silently "running". fn()'s own staged
                # domain writes are already gone by now (_try_write rolled
                # back before returning False), so this is a status-only
                # write -- _write_status's retry/fresh-session fallback is
                # safe here for exactly that reason.
                _write_status(
                    job_id,
                    expected_status="running",
                    status="error",
                    finished_at=_now(),
                    error=(
                        "The job finished but its result could not be "
                        "recorded after repeated attempts. Run it again."
                    ),
                )
    finally:
        _unregister_owned_job(job_id)


def _run_job(app_obj, job_id: str, fn: Callable[[], Any]) -> None:
    """Async entry point: pushes its own app context -- a `ThreadPoolExecutor`
    worker thread starts with none of its own -- then runs `_execute_job`.

    A module-level function rather than a closure, so it can be called
    directly (as `enqueue_job` submits it to `_EXECUTOR`) or patched in
    tests that need to observe its exact recording behaviour.
    """
    with app_obj.app_context():
        _execute_job(job_id, fn)


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

    Called at application startup (app.py), inside an app context. This
    only ever touches a row whose lease has actually run out, so it is safe
    to call from *any* `create_app()` -- the long-running web process at
    startup, or a one-shot utility script's own app instance built while
    that web process is still alive and holding a valid, heartbeat-renewed
    lease on its own job. A repeated call -- a second boot, a second
    gunicorn worker, a utility script running alongside the web process --
    matches only rows that are genuinely expired by then and updates
    nothing else.

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
