"""Refuse every write into the deployment's state directory during a test run
(issue #639).

On the machine that runs the app, the main checkout's `data/` is the directory
docker-compose.yml binds at `/app/data`: the mail cursor, the Google spend
ledger, the in-flight markers the deploy watcher reads, the score snapshots
that are the only way back from a re-score. `pytest` runs in that same
checkout. On 2026-11-09 an ingestion test that set no path of its own wrote a
fake mailbox's last UID (99) into the production cursor, and every scheduled
run re-read the oldest mail instead of the new.

#627 moved the two paths that had done damage -- the cursor and the ledger --
one at a time. That is a list, and a list is only as long as the incidents
behind it: measured on 2026-11-12, an ordinary full run of this suite still
left a `data/.inflight/` directory behind in the checkout, which on the mini is
the directory the watcher reads to decide what a deploy would kill. So this
guard does not name files. It names the directory, and refuses the operation:
any open-for-writing, create, rename, delete, truncate, chmod or utime whose
real path lands inside it fails on the line that does it.

How: `sys.addaudithook`. CPython raises an audit event from inside `open`,
`os.open`, `os.mkdir`, `os.rename`, `os.remove` and the rest before the system
call is made, whatever library sits on top -- `pathlib`, `shutil`, `tempfile`,
`json.dump` into an open handle -- so one hook sees them all, and raising from
it stops the call. Reads are not refused: the four committed reference files
(`docs/rules/reference-data.md`) live in the same directory and are read by
half the suite.

Like `tests/network_guard.py`, every refusal is *recorded* as well as raised.
`utils/google_spend.record_spend` is the worked example of why: it catches the
failure of its own write on purpose ("never fail a caller for trying"), so a
refused write there leaves a green test. The record survives that, and
`tests/conftest.py` fails the session and names the test.

What is protected:

* `<checkout>/data` -- where `Config.DATA_DIR` points when nothing overrides
  it, and where every cwd-relative `data/...` literal lands when pytest runs
  from the repository root;
* whatever `Config.DATA_DIR` was when the session started, if the process
  environment named another directory (`DATA_DIR`);
* `IDEALISTA_DATA_DIR` from the process environment, the host path of the
  state directory once it lives outside the checkout
  (`tools/autopilot/lib/data_dir.sh`).

What this does not cover, said rather than implied:

* a subprocess. An audit hook lives in one interpreter, so the shell harnesses
  and the tests that spawn `bash` or `python` are outside it; they run against
  throwaway roots of their own (`BACKFILL_ROOT`, `AUTOPILOT_REPO_DIR`).
* a write made from C that never passes through Python's `open` or `os`.
  `sqlite3.connect` is the one such caller this suite has, it raises its own
  audit event, and that event is checked here by name.
* a relative name given together with a directory file descriptor
  (`dir_fd=`): the event carries the descriptor's number, not its directory,
  so the name cannot be resolved and is let through rather than read against
  the current directory -- which is what refused pytest's own cleanup of a
  temporary `data/` here on the first run, `os.rmdir("data", dir_fd=...)`
  inside `shutil.rmtree`. `shutil.rmtree` is the standard-library user of that
  form and is checked at its own event, by the root it was handed. `os.open`
  is the one event that does not say whether a `dir_fd` was passed; a writing
  `os.open` of a relative name is therefore always read against the current
  directory, and nothing in this codebase opens that way.

There is no off switch. `PYTEST_ALLOW_NETWORK` exists because someone may
deliberately want a live API; nobody deliberately wants a test run to write the
deployment's state.
"""

from __future__ import annotations

import os
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The host-side variable of tools/autopilot/lib/data_dir.sh.
HOST_VARIABLE = "IDEALISTA_DATA_DIR"

# How many frames of this repository to name in a refusal: the helper that
# wrote, the code that called it, the test.
CALLER_FRAMES = 3

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

# event -> (index of a path the operation changes, index of the `dir_fd` that
# path is relative to, or None when the event carries none). Measured on
# CPython 3.11, not read off the documentation: `os.rename` and `os.replace`
# share an event and change both ends; a link's first argument is only read;
# an absent `dir_fd` arrives as -1 (as None from `shutil.rmtree`).
_PATH_ARGUMENTS: dict[str, tuple[tuple[int, int | None], ...]] = {
    "os.mkdir": ((0, 2),),
    "os.rmdir": ((0, 1),),
    "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),),
    "os.link": ((1, 3),),
    "os.truncate": ((0, None),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.utime": ((0, 3),),
    "shutil.rmtree": ((0, 1),),
    "sqlite3.connect": ((0, None),),
}


class DataDirWriteDuringTest(RuntimeError):
    """Raised in place of a write into the deployment's state directory.

    A `RuntimeError` rather than an `OSError` for the reason the network guard
    gives: the callers that write here catch `OSError` and degrade, and a
    refusal dressed as one would be handled as "the disk is full" by the very
    code whose write is the defect.
    """


@dataclass(frozen=True)
class Attempt:
    """One refused write, kept whether or not its caller re-raised."""

    nodeid: str
    operation: str
    path: str
    caller: str


_attempts: list[Attempt] = []
_current_nodeid = "<collection>"
_protected: tuple[str, ...] = ()
_hook_added = False
_active = False


def protected_roots(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The real paths no test may write under, most specific spelling first.

    `extra` is how conftest passes what `Config.DATA_DIR` was before it moved
    the attribute to a temporary directory.
    """
    candidates = [str(REPO_ROOT / "data"), *extra]
    host = os.environ.get(HOST_VARIABLE, "").strip()
    if host:
        candidates.append(host)
    roots: list[str] = []
    for candidate in candidates:
        if not candidate or not os.path.isabs(candidate):
            continue
        real = os.path.realpath(candidate)
        if real != os.path.sep and real not in roots:
            roots.append(real)
    return tuple(roots)


def _real(path: object) -> str | None:
    """The real path an argument names, or None when it names none.

    An integer is a file descriptor: `open(fd, "w")` re-wraps a file that some
    earlier call already opened, and that call was the one to judge. A relative
    path is read against the current directory, which is what the kernel will
    do with it -- so a `data/x.json` literal is protected when pytest runs from
    the repository root and is somebody's temporary directory after a
    `monkeypatch.chdir`. `realpath` resolves symlinks in whatever part of the
    path exists, so a link that points into the directory is a write into it.
    """
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fspath(path)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not text or text == ":memory:" or text.startswith("file:"):
        return None
    try:
        return os.path.realpath(text)
    except (OSError, ValueError):
        return None


def _relative_to_a_descriptor(
    args: tuple, index: int, dir_fd_index: int | None
) -> bool:
    """Whether this argument is a relative name resolved against a `dir_fd`.

    Such a name says nothing about where it is: the kernel reads it against
    the descriptor's directory, this hook could only read it against the
    current one, and those are different directories. An absolute path ignores
    the descriptor, so it is still judged.
    """
    if dir_fd_index is None or dir_fd_index >= len(args):
        return False
    dir_fd = args[dir_fd_index]
    if dir_fd is None or dir_fd == -1:
        return False
    try:
        text = os.fspath(args[index])
    except TypeError:
        return False
    return not os.path.isabs(text)


def _inside(real: str) -> bool:
    for root in _protected:
        if real == root or real.startswith(root + os.path.sep):
            return True
    return False


def _caller() -> str:
    """The lines of this repository that led to the write, innermost first.

    Same reading as the network guard's: frames outside the repository and
    inside the virtualenv name nothing anyone can fix.
    """
    chain: list[str] = []
    for frame in reversed(traceback.extract_stack()):
        if frame.filename.startswith("<"):
            continue
        path = Path(frame.filename)
        if path == Path(__file__):
            continue
        try:
            relative = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in {".venv", "site-packages"}:
            continue
        chain.append(f"{relative}:{frame.lineno}")
        if len(chain) == CALLER_FRAMES:
            break
    return " <- ".join(chain) if chain else "<outside this repository>"


def _refuse(operation: str, real: str) -> None:
    """Record, then raise. Both halves matter -- see the module docstring."""
    caller = _caller()
    _attempts.append(
        Attempt(nodeid=_current_nodeid, operation=operation, path=real, caller=caller)
    )
    raise DataDirWriteDuringTest(
        f"test tried to write the deployment's state directory: {operation} "
        f"{real} (from {caller}). On the machine that runs the app this is "
        "the directory mounted at /app/data. Give the code under test a path "
        "under tmp_path, or move the attribute it reads (issue #639)."
    )


def _hook(event: str, args: tuple) -> None:
    if not _active:
        return
    if event == "open":
        # (path, mode, flags). The flags are the ones the system call will be
        # made with, for `open()` and `os.open()` alike; the mode string is
        # absent for the latter, so it is not what is read.
        if len(args) < 3 or not isinstance(args[2], int):
            return
        if not args[2] & _WRITE_FLAGS:
            return
        real = _real(args[0])
        if real is not None and _inside(real):
            _refuse("open for writing", real)
        return
    arguments = _PATH_ARGUMENTS.get(event)
    if arguments is None:
        return
    for index, dir_fd_index in arguments:
        if index >= len(args):
            continue
        if _relative_to_a_descriptor(args, index, dir_fd_index):
            continue
        real = _real(args[index])
        if real is not None and _inside(real):
            _refuse(event, real)


def install(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Start refusing. Returns the protected roots, for the session header.

    An audit hook cannot be removed once added, so it is added once per
    interpreter and switched by a flag: `uninstall()` makes it a no-op.
    """
    global _protected, _hook_added, _active
    _protected = protected_roots(extra)
    if not _hook_added:
        sys.addaudithook(_hook)
        _hook_added = True
    _active = True
    return _protected


def installed() -> bool:
    return _active and bool(_protected)


def uninstall() -> None:
    global _active
    _active = False


def note_test(nodeid: str) -> None:
    """Attribute later attempts to `nodeid` (called from the runtest hooks)."""
    global _current_nodeid
    _current_nodeid = nodeid


def attempts() -> list[Attempt]:
    return list(_attempts)


@contextmanager
def capture_attempts():
    """Collect the attempts made in this block and leave the session record as
    it was. For the guard's own tests, which write on purpose and must not be
    reported as leaks by the run they are part of."""
    global _attempts
    outer, _attempts = _attempts, []
    try:
        yield _attempts
    finally:
        _attempts = outer


def summary_lines() -> list[str]:
    """The session report: one block per test that wrote, or tried to."""
    if not _attempts:
        return []
    by_test: dict[str, list[Attempt]] = {}
    for attempt in _attempts:
        by_test.setdefault(attempt.nodeid, []).append(attempt)

    lines = [
        f"{len(_attempts)} write(s) into the deployment's state directory were "
        f"refused, from {len(by_test)} test(s):",
        "",
    ]
    for nodeid, attempts_for_test in by_test.items():
        lines.append(nodeid)
        seen: set[tuple[str, str, str]] = set()
        for attempt in attempts_for_test:
            key = (attempt.operation, attempt.path, attempt.caller)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  -> {attempt.operation} {attempt.path}  ({attempt.caller})")
    lines.extend(
        [
            "",
            "Each of these would have changed a file of the running deployment "
            "had the suite been run in its checkout (issue #639). Point the "
            "code under test at tmp_path; the refusal was raised where the "
            "write was made, but a caller that catches and degrades can leave "
            "the test green, which is why it is reported here as well.",
        ]
    )
    return lines
