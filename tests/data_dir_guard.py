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

How an argument becomes a path, since that is where a guard like this leaks:
every form is resolved to where the kernel would act, and a form that cannot
be resolved is refused rather than let through.

* text, bytes and `os.PathLike` alike; a relative name against the current
  directory, or - when the call carries a `dir_fd` - against that descriptor's
  directory, read from the kernel (`F_GETPATH`, `/proc/self/fd`);
* a file descriptor as the target (`os.fchmod`, `open(fd, "w")`), by the path
  it is open on; one that has no path is a pipe or a socket;
* both the fully resolved target and the directory entry itself, so a link
  outside that points in and a link inside that points out are both seen;
* either end of a rename or a hard link: a hard link out of the directory is a
  second name for the same bytes, which nothing here could follow afterwards;
* for `sqlite3.connect`, whose event does not say whether `uri=True` was
  passed, both readings - the literal file name and the `file:` URI, percent
  escapes decoded. Only a query that is exactly `mode=ro` opens for reading;
  only the path `:memory:` is not on disk. Every other query is a write.

Three `os` functions are wrapped as well, because the audit trail does not
cover them on CPython 3.11: `os.open` raises its event without the `dir_fd`,
`os.mkfifo` and `os.mknod` raise none.

What this does not cover, said rather than implied:

* a subprocess. An audit hook lives in one interpreter, so the shell harnesses
  and the tests that spawn `bash` or `python` are outside it; they run against
  throwaway roots of their own (`BACKFILL_ROOT`, `AUTOPILOT_REPO_DIR`).
* a write made from C that never passes through Python's `open` or `os`: a
  file named inside SQL (`ATTACH DATABASE`, `VACUUM INTO`), `dbm`, an
  extension module. `sqlite3.connect` itself raises an event and is checked.
* a hard link made before the run, and a descriptor inherited already open.

There is no off switch. `PYTEST_ALLOW_NETWORK` exists because someone may
deliberately want a live API; nobody deliberately wants a test run to write the
deployment's state.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

try:  # POSIX only; without it descriptors are resolved through /proc.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

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
# share an event and change both ends; an absent `dir_fd` arrives as -1 (as
# None from `shutil.rmtree`); `os.ftruncate`, `os.fchmod` and `os.fchown` raise
# the path events with the descriptor where the path would be. Both ends of
# `os.link` are judged - see the module docstring.
_PATH_ARGUMENTS: dict[str, tuple[tuple[int, int | None], ...]] = {
    "os.mkdir": ((0, 2),),
    "os.rmdir": ((0, 1),),
    "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),),
    "os.link": ((0, 2), (1, 3)),
    "os.truncate": ((0, None),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.utime": ((0, 3),),
    "os.chflags": ((0, None),),
    "os.setxattr": ((0, None),),
    "os.removexattr": ((0, None),),
    "shutil.rmtree": ((0, 1),),
}


class _Unresolvable(Exception):
    """An argument that cannot be turned into a path. Refused, not skipped."""


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
# The path object of an `os.open(..., dir_fd=...)` the wrapper has already
# judged against its descriptor, for the length of that one call. The `open`
# event the call raises carries the same object and no descriptor; judged again
# it would be read against the working directory, which is another directory.
_judged = threading.local()


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


def _descriptor_path(fd: int) -> str | None:
    """The path an open descriptor is open on, or None when it has none - a
    pipe, a socket, a number that is not open."""
    try:
        if fcntl is not None and hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            text = os.fsdecode(raw.split(b"\0", 1)[0])
        else:
            text = os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError, OverflowError):
        return None
    return text if os.path.isabs(text) else None


def _places(argument: object, dir_fd: object = None) -> tuple[str, ...]:
    """Every real path an operation could change through this argument.

    Two for a path: where it leads once every link is followed, and the
    directory entry itself (its directory resolved, its own name kept). An
    operation on a link outside the directory that points in changes the
    first; one on a link inside it that points out changes the second.
    """
    if argument is None:
        return ()
    if isinstance(argument, int):
        path = _descriptor_path(argument)
        return () if path is None else (os.path.realpath(path),)
    try:
        text = os.fspath(argument)
        if isinstance(text, bytes):
            text = os.fsdecode(text)
    except (TypeError, ValueError) as error:
        raise _Unresolvable(repr(argument)) from error
    if not text:
        return ()
    try:
        if not os.path.isabs(text):
            if dir_fd is None or dir_fd == -1:
                base = os.getcwd()
            else:
                base = _descriptor_path(dir_fd) if isinstance(dir_fd, int) else None
                if base is None:
                    raise _Unresolvable(f"{text!r} relative to descriptor {dir_fd!r}")
            text = os.path.join(base, text)
        target = os.path.realpath(text)
        head, tail = os.path.split(text.rstrip(os.path.sep))
        if tail in ("", ".", ".."):
            return (target,)
        return (target, os.path.join(os.path.realpath(head), tail))
    except (OSError, ValueError) as error:
        raise _Unresolvable(repr(argument)) from error


def _sqlite_places(database: object) -> tuple[str, ...]:
    """Where `sqlite3.connect(database)` could write. The event does not carry
    `uri=`, so a name that starts with `file:` is read both ways."""
    try:
        text = os.fspath(database) if not isinstance(database, str) else database
        if isinstance(text, bytes):
            text = os.fsdecode(text)
    except (TypeError, ValueError) as error:
        raise _Unresolvable(repr(database)) from error
    if text in ("", ":memory:"):
        return ()
    places = _places(text)
    if not text.startswith("file:"):
        return places

    path, _, query = text[len("file:") :].split("#", 1)[0].partition("?")
    if path.startswith("//"):
        authority, slash, rest = path[2:].partition("/")
        if authority not in ("", "localhost"):
            raise _Unresolvable(text)
        path = slash + rest
    path = os.fsdecode(unquote_to_bytes(path))
    if not path or path == ":memory:":
        return places
    # One way to say "read", and it has to be said exactly: the whole query is
    # the literal `mode=ro`. SQLite's own reading of a query - which of two
    # `immutable=` wins, what an escaped or unknown parameter does - is not
    # re-implemented here; anything else on a path is treated as a write.
    if query == "mode=ro":
        return places
    return places + _places(path)


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


def _judge(operation: str, places) -> None:
    """Refuse when any place the operation could change is protected, or when
    working the places out failed."""
    try:
        resolved = places()
    except _Unresolvable as error:
        _refuse(operation, f"<a path that cannot be resolved: {error}>")
    for real in resolved:
        if _inside(real):
            _refuse(operation, real)


def _hook(event: str, args: tuple) -> None:
    if not _active:
        return
    if event == "open":
        # (path, mode, flags). The flags are the ones the system call will be
        # made with, for `open()` and `os.open()` alike; the mode string is
        # absent for the latter, so it is not what is read.
        if len(args) < 3 or not isinstance(args[2], int):
            return
        if args[2] & _WRITE_FLAGS and args[0] is not getattr(_judged, "path", None):
            _judge("open for writing", lambda: _places(args[0]))
        return
    if event == "sqlite3.connect":
        if args:
            _judge(event, lambda: _sqlite_places(args[0]))
        return
    arguments = _PATH_ARGUMENTS.get(event)
    if arguments is None:
        return
    for index, dir_fd_index in arguments:
        if index >= len(args):
            continue
        dir_fd = None
        if dir_fd_index is not None and dir_fd_index < len(args):
            dir_fd = args[dir_fd_index]
        _judge(event, lambda i=index, d=dir_fd: _places(args[i], d))


def _wrap(name: str, always: bool) -> None:
    """Put a check in front of an `os` function the audit trail leaves a hole
    in. `always` for one that raises no event at all; otherwise only the form
    its event cannot describe - a writing call with a `dir_fd`."""
    original = getattr(os, name, None)
    if original is None:
        return

    @functools.wraps(original)
    def guarded(path, *args, dir_fd=None, **kwargs):
        if _active and (always or dir_fd is not None):
            flags = args[0] if args else kwargs.get("flags", 0)
            if always or (isinstance(flags, int) and flags & _WRITE_FLAGS):
                _judge(f"os.{name}", lambda: _places(path, dir_fd))
        if dir_fd is None:
            return original(path, *args, **kwargs)
        _judged.path = path
        try:
            return original(path, *args, dir_fd=dir_fd, **kwargs)
        finally:
            _judged.path = None

    if original in os.supports_dir_fd:
        os.supports_dir_fd.add(guarded)
    setattr(os, name, guarded)


@contextmanager
def protect(*directories: str | os.PathLike):
    """Protect more directories for the length of a block. For the guard's own
    tests: they aim real operations at a temporary directory, so that on the
    day the guard is broken nothing of the deployment is what gets written."""
    global _protected
    before = _protected
    _protected = before + tuple(os.path.realpath(d) for d in directories)
    try:
        yield
    finally:
        _protected = before


def install(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Start refusing. Returns the protected roots, for the session header.

    An audit hook cannot be removed once added, so it is added once per
    interpreter and switched by a flag: `uninstall()` makes it a no-op.
    """
    global _protected, _hook_added, _active
    _protected = protected_roots(extra)
    if not _hook_added:
        sys.addaudithook(_hook)
        _wrap("open", always=False)
        _wrap("mkfifo", always=True)
        _wrap("mknod", always=True)
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
