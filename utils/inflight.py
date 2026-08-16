"""In-flight markers for long-running jobs (issue #283).

A backfill runs for hours inside `idealista-app`. The deploy chain recreates
that container within five minutes of a merge landing on main, and until #283
neither side knew about the other: the run died mid-flight and nothing said
so — `/api/healthz` answers "can the app serve", the watcher logged an
ordinary successful deploy, and the only record of what had completed was the
job's own per-row ledger. Observed twice on 2026-08-14.

A marker is the missing sentence. On start a job writes
`data/.inflight/<name>.<run_id>.json`; on a clean exit it removes it. `data/`
is bind-mounted (`./data:/app/data`), so the file a container process writes
is the file `tools/autopilot/deploy_watcher.sh` reads on the host — which is
the whole point, since the watcher cannot see inside the container's
filesystem any other way.

The identity in that filename is a run id (`uuid4().hex`), not the PID
(#359). A long job runs in its own container so a deploy cannot kill it,
which makes that job PID 1 — and every restart of the same module is PID 1
again. A PID-keyed filename then collides across restarts (a restart
overwrites its predecessor's marker, destroying the very evidence #283 exists
to keep) and a PID-keyed liveness check finds itself: the successor reads the
predecessor's marker, sees "pid 1 is alive" — because it *is*, it's the
reader — and concludes a run is still active when the only thing running is
itself. `pid` is still recorded in the body, for a human reading the file and
for the same-namespace liveness check, but it is no longer the marker's
identity.

Two readers, two different questions:

- **The watcher** already knows *whether* something is running (`docker top`
  is authoritative about live processes and needs no cooperation from the
  job). What it cannot learn from a process list is whether killing that
  process loses work. That is what `resumable` and `ledger` are for.
- **The next run of the same job** finds the marker its predecessor never got
  to remove, and says so. That is the "report what it lost" half: a marker
  left behind *is* the report of an interrupted run.

`resumable` is a claim about the job, not a wish: set it true only when an
interrupted run really does resume without losing or re-billing work — commit
per row, an idempotent scope that completed rows leave, and a ledger to
reconcile against. Default it to false and let the honest jobs opt in;
"unknown" and "not resumable" have to behave the same way, because the deploy
that reads them cannot tell the difference.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Relative to the repository root, which is also /app in the container, so the
# same literal is correct on both sides of the bind mount.
INFLIGHT_DIRNAME = os.path.join("data", ".inflight")


def inflight_dir(directory: Optional[str] = None) -> Path:
    return Path(directory) if directory else Path(INFLIGHT_DIRNAME)


def _marker_path(name: str, run_id: str, directory: Optional[str] = None) -> Path:
    return inflight_dir(directory) / f"{name}.{run_id}.json"


def _pid_is_alive(pid: int) -> bool:
    """Whether `pid` exists in *this* namespace.

    Only meaningful to a reader sharing the job's PID namespace — another
    process in the same container. The watcher runs on the host and must not
    call this: host PID 4711 is a different process from container PID 4711.
    It asks `docker top` instead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to someone else. Existing is the question.
        return True
    except OSError:
        return False
    return True


def _cmdline(pid: int) -> Optional[str]:
    """The process's command line, or None where it cannot be read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _marker_liveness(pid: int, module: str) -> str:
    """ "alive", "dead", or "unknown" for `pid` as the owner of `module` (#359).

    Not a boolean: a boolean forces "cannot tell" to read as either "still
    running" (which blocks the report #283 exists to produce) or "safe to
    clear" (which can delete the record of a run that is, in fact, still
    going) — both are a confident wrong answer manufactured out of a genuine
    unknown.

    "dead" when `pid` is not alive in this namespace, or when it is alive but
    plainly not this job (`/proc` is readable and its cmdline does not
    contain `module`).

    "dead" also, deliberately, when `pid == os.getpid()`. A containerised run
    is always PID 1, and every restart of the same module is PID 1 again: a
    reader that only checked "is this pid alive and running my module" would
    find itself and answer "yes" every time, which is precisely the defect
    this function exists to close. A pid that is *me* is never evidence of a
    concurrent run.

    "alive" when `pid` is alive, is not me, and (where `/proc` is readable)
    its cmdline contains `module`.

    "unknown" when `pid` is alive and not me, but `/proc` cannot be read
    (macOS, a hardened runtime) so cmdline cannot confirm or refute it. The
    caller must leave such a marker alone: it is neither proven concurrent
    nor proven interrupted.
    """
    if not _pid_is_alive(pid):
        return "dead"
    if pid == os.getpid():
        return "dead"
    cmdline = _cmdline(pid)
    if cmdline is None:
        return "unknown"
    return "alive" if module in cmdline else "dead"


def read_markers(directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every readable marker, each carrying the path it came from.

    A marker that does not parse is skipped with a warning rather than
    raising: a corrupt file must not be able to stop a deploy or a backfill.
    """
    base = inflight_dir(directory)
    found: List[Dict[str, Any]] = []
    try:
        entries = sorted(base.glob("*.json"))
    except OSError as exc:
        logger.warning("Cannot list %s: %s", base, exc)
        return found
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable in-flight marker %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("Ignoring in-flight marker %s: not an object", path)
            continue
        data["marker_path"] = str(path)
        found.append(data)
    return found


def describe(marker: Dict[str, Any]) -> str:
    """The command line as it would be typed, for a log a human reads."""
    module = marker.get("module") or "?"
    argv = marker.get("argv") or []
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        argv = []
    return " ".join(["python", "-m", f"utils.{module}", *(str(a) for a in argv)])


def report_interrupted(
    name: str, directory: Optional[str] = None, *, run_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Report — and clear — markers a previous run of `name` left behind.

    A marker outlives its process precisely when the process was killed, so
    finding one is the evidence that a run did not finish. Reporting it is the
    point; clearing it afterwards is what keeps the report from repeating
    forever and from telling the watcher a dead job is in flight.

    A marker whose owner is alive is a *concurrent* run, not a corpse: it is
    reported as such and left alone. Two runs of the same backfill would
    re-bill the same rows, so this deserves the warning it gets. A marker
    whose owner cannot be confirmed either way (`/proc` unreadable) is left
    alone too, with a warning that says so — see `_marker_liveness`.

    `run_id`, when given, names *this* run: a marker carrying that exact id
    can only be the one this run is about to write (or already wrote), never
    a predecessor's, so it is skipped before liveness is even asked. The
    default, `None`, skips nothing — there is no "own run" to exclude when
    this is called on its own, which is how every direct caller and the tests
    use it. `inflight()` generates one id and threads it through both this
    call and `write_marker`.
    """
    interrupted: List[Dict[str, Any]] = []
    for marker in read_markers(directory):
        if marker.get("module") != name:
            continue
        marker_run_id = marker.get("run_id")
        if run_id is not None and marker_run_id == run_id:
            continue
        pid = marker.get("pid")
        # `isinstance(True, int)` is True, and PID 1 is gunicorn inside the
        # container - a corrupt marker holding `true` must not read as "the
        # job is still running" and suppress the report.
        pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else -1
        liveness = _marker_liveness(pid, name)
        if liveness == "unknown":
            logger.warning(
                "cannot tell whether the earlier run of %s is still active "
                "(pid %s, started %s); leaving its marker in place.",
                name,
                pid,
                marker.get("started_at"),
            )
            continue
        if liveness == "alive":
            logger.warning(
                "Another run of %s appears to be active (pid %s, started %s). "
                "Two concurrent runs re-measure the same rows.",
                name,
                pid,
                marker.get("started_at"),
            )
            continue
        interrupted.append(marker)
        logger.warning(
            "A previous run of %s did not finish: started %s, pid %s, command: %s",
            name,
            marker.get("started_at"),
            pid,
            describe(marker),
        )
        ledger = marker.get("ledger")
        if ledger:
            logger.warning("  what it completed is recorded in %s", ledger)
        else:
            logger.warning(
                "  it recorded no ledger, so what it completed cannot be read back."
            )
        # Only the run that claimed resumability gets the reassurance. Saying
        # "already done rows are skipped" over a job that re-does them would
        # be the comfortable answer rather than the true one.
        if marker.get("resumable") is True:
            logger.warning(
                "  it reported itself resumable: rows it finished have left "
                "this run's scope and are not measured again."
            )
        else:
            logger.warning(
                "  it did not report itself resumable: this run covers those "
                "rows again, and any cost they carried is paid twice."
            )
        try:
            Path(marker["marker_path"]).unlink()
        except OSError as exc:
            logger.warning("  could not clear its marker: %s", exc)
    return interrupted


def write_marker(
    name: str,
    *,
    ledger: Optional[str] = None,
    resumable: bool = False,
    argv: Optional[Sequence[str]] = None,
    directory: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Path:
    """Write this run's marker and return its path.

    `run_id` is this marker's identity — its filename and its `run_id` field
    — not the PID (#359): inside a container every run is PID 1, so a second
    call with the same PID used to overwrite the first call's marker outright,
    destroying the very evidence #283 exists to keep. Left as `None` (the
    default), a fresh one is generated here with `uuid4().hex`, unique per
    call; a caller that already has one for this run (`inflight()` does)
    passes it through so the marker `report_interrupted` was told to skip is
    the one actually written. `pid` is still recorded in the body, for a
    human reading the file and for the same-namespace liveness check.

    Written with a temporary file and an atomic rename: the watcher polls
    every few minutes and must never read half a JSON object.
    """
    run_id = run_id or uuid.uuid4().hex
    base = inflight_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    path = _marker_path(name, run_id, directory)
    payload = {
        "module": name,
        "pid": os.getpid(),
        "run_id": run_id,
        "argv": list(argv if argv is not None else sys.argv[1:]),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "resumable": bool(resumable),
        "ledger": ledger,
    }
    tmp = path.with_suffix(f".{run_id}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def clear_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove the in-flight marker %s: %s", path, exc)


@contextmanager
def inflight(
    name: str,
    *,
    ledger: Optional[str] = None,
    resumable: bool = False,
    argv: Optional[Sequence[str]] = None,
    directory: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Iterator[Path]:
    """Announce a long job for as long as it runs.

    Reports anything a previous run left behind first — that report is the
    only place an interrupted run gets named — then writes this run's marker
    and removes it on the way out, including on an exception. A SIGKILL
    (which is how a container recreate ends) skips the removal by definition;
    that is what leaves the evidence for the next run to find.

    One `run_id` is generated here (or accepted from the caller, left as
    `None` by every real caller) and threaded through both calls below, so
    the marker `report_interrupted` is told is "mine" is the exact one
    `write_marker` then writes.
    """
    run_id = run_id or uuid.uuid4().hex
    report_interrupted(name, directory, run_id=run_id)
    path = write_marker(
        name,
        ledger=ledger,
        resumable=resumable,
        argv=argv,
        directory=directory,
        run_id=run_id,
    )
    try:
        yield path
    finally:
        clear_marker(path)
