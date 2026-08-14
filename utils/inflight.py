"""In-flight markers for long-running jobs (issue #283).

A backfill runs for hours inside `idealista-app`. The deploy chain recreates
that container within five minutes of a merge landing on main, and until #283
neither side knew about the other: the run died mid-flight and nothing said
so — `/api/healthz` answers "can the app serve", the watcher logged an
ordinary successful deploy, and the only record of what had completed was the
job's own per-row ledger. Observed twice on 2026-08-14.

A marker is the missing sentence. On start a job writes
`data/.inflight/<name>.<pid>.json`; on a clean exit it removes it. `data/` is
bind-mounted (`./data:/app/data`), so the file a container process writes is
the file `tools/autopilot/deploy_watcher.sh` reads on the host — which is the
whole point, since the watcher cannot see inside the container's filesystem
any other way.

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


def _marker_path(name: str, pid: int, directory: Optional[str] = None) -> Path:
    return inflight_dir(directory) / f"{name}.{pid}.json"


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


def _same_job_is_running(pid: int, module: str) -> bool:
    """Whether `pid` is *this* job, rather than merely a live process.

    Liveness alone is not enough. A marker is read by the next run of the same
    job, and after a container recreate that run starts in a fresh PID
    namespace where numbers begin at 1 — so the dead run's PID can easily
    belong to something unrelated by then. Reading it as "another run is
    active" would report the opposite of what happened and leave the marker
    in place.

    `/proc` settles it inside the container. Where it cannot be read (macOS,
    a hardened runtime) liveness is all there is, and the answer degrades to
    what it was.
    """
    if not _pid_is_alive(pid):
        return False
    cmdline = _cmdline(pid)
    if cmdline is None:
        return True
    return module in cmdline


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
    name: str, directory: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Report — and clear — markers a previous run of `name` left behind.

    A marker outlives its process precisely when the process was killed, so
    finding one is the evidence that a run did not finish. Reporting it is the
    point; clearing it afterwards is what keeps the report from repeating
    forever and from telling the watcher a dead job is in flight.

    A marker whose PID is still alive is a *concurrent* run, not a corpse: it
    is reported as such and left alone. Two runs of the same backfill would
    re-bill the same rows, so this deserves the warning it gets.
    """
    interrupted: List[Dict[str, Any]] = []
    for marker in read_markers(directory):
        if marker.get("module") != name:
            continue
        pid = marker.get("pid")
        # `isinstance(True, int)` is True, and PID 1 is gunicorn inside the
        # container - a corrupt marker holding `true` must not read as "the
        # job is still running" and suppress the report.
        pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else -1
        if _same_job_is_running(pid, name):
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
) -> Path:
    """Write this run's marker and return its path.

    Written with a temporary file and an atomic rename: the watcher polls
    every few minutes and must never read half a JSON object.
    """
    base = inflight_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    path = _marker_path(name, os.getpid(), directory)
    payload = {
        "module": name,
        "pid": os.getpid(),
        "argv": list(argv if argv is not None else sys.argv[1:]),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "resumable": bool(resumable),
        "ledger": ledger,
    }
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
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
) -> Iterator[Path]:
    """Announce a long job for as long as it runs.

    Reports anything a previous run left behind first — that report is the
    only place an interrupted run gets named — then writes this run's marker
    and removes it on the way out, including on an exception. A SIGKILL
    (which is how a container recreate ends) skips the removal by definition;
    that is what leaves the evidence for the next run to find.
    """
    report_interrupted(name, directory)
    path = write_marker(
        name, ledger=ledger, resumable=resumable, argv=argv, directory=directory
    )
    try:
        yield path
    finally:
        clear_marker(path)
