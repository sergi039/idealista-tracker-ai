"""A test run cannot write the deployment's state directory (issue #639).

The main checkout's `data/` is the directory docker-compose.yml mounts at
`/app/data` on the machine that runs the app, and `pytest` runs in that
checkout. `tests/data_dir_guard.py` refuses every write whose real path lands
inside a protected directory; this file is the proof that it does, form of
argument by form of argument, with the real operation each time.

None of it is aimed at the checkout's `data/`. Every operation here goes at a
stand-in directory under `tmp_path` that the guard is told to protect for the
length of the test, so on the day the guard is broken what gets written is a
temporary file - a test of a guard that damaged the deployment when the guard
regressed would be the incident it is written against. And nothing here
depends on what the real directory holds: on the machine that runs the app it
holds live state, and pytest runs there. Where the real directory is looked at
at all, the assertion is "the same before and after".
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from config import Config
from tests import data_dir_guard
from tests.data_dir_guard import DataDirWriteDuringTest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKOUT_DATA = REPO_ROOT / "data"

# One of the four committed reference files; reading them is what half the
# suite does.
REFERENCE_FILE = CHECKOUT_DATA / "ine_municipal.json"

CREATE = os.O_WRONLY | os.O_CREAT
PROBE_TABLE = "CREATE TABLE probe (id INTEGER)"


def _inside(path: str | os.PathLike, directory: str | os.PathLike) -> bool:
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    return os.path.commonpath([path, directory]) == directory


def _snapshot(directory: Path) -> dict[str, object]:
    """Every entry under a directory with what it holds: enough to say "the
    same before and after" of a file, a directory, a link or a fifo."""
    found: dict[str, object] = {}
    for parent, directories, files in os.walk(directory):
        for name in directories + files:
            path = Path(parent) / name
            key = str(path.relative_to(directory))
            status = path.lstat()
            if path.is_symlink():
                found[key] = ("link", os.readlink(path))
            elif path.is_file():
                found[key] = (
                    "file",
                    status.st_mode,
                    status.st_nlink,
                    path.read_bytes(),
                )
            else:
                found[key] = ("other", status.st_mode)
    return found


@pytest.fixture
def state(tmp_path):
    """A stand-in for the state directory, protected for the length of the
    test, beside an `outside` that is not. `state` holds a file, a directory,
    a database and a link that points out; `outside` holds a file to move in,
    a tree to copy in, a link that points in, and a directory called `state`
    of its own."""
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    (root / "kept.json").write_text("{}")
    (root / "sub" / "deep.json").write_text("{}")
    database = sqlite3.connect(root / "ro.sqlite")
    database.execute("CREATE TABLE t (id INTEGER)")
    database.commit()
    database.close()
    (outside / "tree").mkdir(parents=True)
    (outside / "state").mkdir()
    (outside / "tree" / "leaf.json").write_text("{}")
    (outside / "staged").write_text("99\n")
    os.symlink(root, outside / "link-in")
    os.symlink(outside, root / "link-out")
    with data_dir_guard.protect(root):
        yield root


@pytest.fixture
def refusals():
    """The attempts made by this test, kept out of the session's own record."""
    with data_dir_guard.capture_attempts() as attempts:
        yield attempts


def _by_descriptor(directory: Path, body):
    """Run `body(fd)` with a descriptor open on a directory."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        return body(fd)
    finally:
        os.close(fd)


def _reopen_for_writing(path: Path):
    """`open(fd, "w")` on a descriptor that was opened for reading."""
    fd = os.open(path, os.O_RDONLY)
    try:
        open(fd, "w", closefd=False)
    finally:
        os.close(fd)


def _sqlite(database, **options):
    """Connect and try to write: the reviewer's input was a real connection."""
    connection = sqlite3.connect(database, **options)
    try:
        connection.execute(PROBE_TABLE)
    finally:
        connection.close()


def _uri(text: str):
    _sqlite(text, uri=True)


# Each entry: (state, outside, chdir) -> a real operation that would change
# the state directory. `chdir` is monkeypatch.chdir.
WRITES = {
    # --- every way of opening for writing, every kind of path argument
    "open w": lambda s, o, cd: open(s / "new", "w"),
    "open a": lambda s, o, cd: open(s / "kept.json", "a"),
    "open x": lambda s, o, cd: open(s / "new", "x"),
    "open r+": lambda s, o, cd: open(s / "kept.json", "r+"),
    "open wb": lambda s, o, cd: open(s / "new", "wb"),
    "a str path": lambda s, o, cd: open(str(s / "new"), "w"),
    "a bytes path": lambda s, o, cd: open(os.fsencode(s / "new"), "w"),
    "os.open O_CREAT": lambda s, o, cd: os.open(s / "new", CREATE | os.O_APPEND, 0o600),
    "Path.write_text": lambda s, o, cd: (s / "new").write_text("99\n"),
    "Path.touch": lambda s, o, cd: (s / "new").touch(),
    "a relative path, from the parent": lambda s, o, cd: (
        cd(s.parent),
        open(os.path.join("state", "new"), "w"),
    ),
    "a relative path, from inside": lambda s, o, cd: (cd(s / "sub"), open("new", "w")),
    "a relative path with ..": lambda s, o, cd: (
        cd(o),
        open(os.path.join("..", "state", "new"), "w"),
    ),
    # --- directory entries: both ends of a rename and of a link
    "mkdir": lambda s, o, cd: os.mkdir(s / "new"),
    "makedirs": lambda s, o, cd: os.makedirs(s / "a" / "b", exist_ok=True),
    "replace into (second argument)": lambda s, o, cd: os.replace(
        o / "staged", s / "new"
    ),
    "rename into (second argument)": lambda s, o, cd: os.rename(
        o / "staged", s / "new"
    ),
    "rename out (first argument)": lambda s, o, cd: os.rename(
        s / "kept.json", o / "taken"
    ),
    "hard link into": lambda s, o, cd: os.link(o / "staged", s / "new"),
    "hard link out": lambda s, o, cd: os.link(s / "kept.json", o / "alias"),
    "symlink created inside": lambda s, o, cd: os.symlink(o, s / "new"),
    "remove": lambda s, o, cd: os.remove(s / "kept.json"),
    "rmdir": lambda s, o, cd: os.rmdir(s / "sub"),
    "mkfifo, which raises no audit event": lambda s, o, cd: os.mkfifo(s / "fifo"),
    # --- links: one outside that points in, one inside that points out
    "create through a link that points in": lambda s, o, cd: open(
        o / "link-in" / "new", "w"
    ),
    "overwrite through a link that points in": lambda s, o, cd: open(
        o / "link-in" / "kept.json", "w"
    ),
    "remove through a link that points in": lambda s, o, cd: os.remove(
        o / "link-in" / "kept.json"
    ),
    "remove a link inside that points out": lambda s, o, cd: os.remove(s / "link-out"),
    "replace a link inside that points out": lambda s, o, cd: os.replace(
        o / "staged", s / "link-out"
    ),
    # --- metadata, by path and by descriptor
    "chmod": lambda s, o, cd: os.chmod(s / "kept.json", 0o600),
    "chmod of the directory itself": lambda s, o, cd: os.chmod(s, 0o700),
    "utime": lambda s, o, cd: os.utime(s / "kept.json", (1, 1)),
    "truncate": lambda s, o, cd: os.truncate(s / "kept.json", 0),
    "chown": lambda s, o, cd: os.chown(s / "kept.json", -1, -1),
    "fchmod on a descriptor": lambda s, o, cd: _by_descriptor(
        s / "kept.json", lambda fd: os.fchmod(fd, 0o600)
    ),
    "utime on a descriptor": lambda s, o, cd: _by_descriptor(
        s / "kept.json", lambda fd: os.utime(fd, (1, 1))
    ),
    "open(fd, 'w') on a descriptor": lambda s, o, cd: _reopen_for_writing(
        s / "kept.json"
    ),
    # --- names relative to a directory descriptor
    "mkdir by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.mkdir("new", dir_fd=fd)
    ),
    "remove by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.remove("kept.json", dir_fd=fd)
    ),
    "rename within, by dir_fds": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.rename("kept.json", "moved", src_dir_fd=fd, dst_dir_fd=fd)
    ),
    "rename in, destination by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.rename(o / "staged", "new", dst_dir_fd=fd)
    ),
    "symlink by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.symlink("target", "new", dir_fd=fd)
    ),
    "os.open by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.open("new", CREATE, dir_fd=fd)
    ),
    "mkfifo by dir_fd": lambda s, o, cd: _by_descriptor(
        s, lambda fd: os.mkfifo("fifo", dir_fd=fd)
    ),
    "by dir_fd, from a directory with the same name": lambda s, o, cd: (
        cd(o),
        _by_descriptor(
            s.parent, lambda fd: os.mkdir(os.path.join("state", "new"), dir_fd=fd)
        ),
    ),
    "an absolute path ignores the dir_fd": lambda s, o, cd: _by_descriptor(
        o, lambda fd: os.mkdir(s / "new", dir_fd=fd)
    ),
    # --- shutil
    "shutil.copy into": lambda s, o, cd: shutil.copy(o / "staged", s / "new"),
    "shutil.copyfile over": lambda s, o, cd: shutil.copyfile(
        o / "staged", s / "kept.json"
    ),
    "shutil.copytree into": lambda s, o, cd: shutil.copytree(o / "tree", s / "tree"),
    "shutil.move into": lambda s, o, cd: shutil.move(str(o / "staged"), str(s / "new")),
    "shutil.move out": lambda s, o, cd: shutil.move(
        str(s / "kept.json"), str(o / "taken")
    ),
    "shutil.rmtree": lambda s, o, cd: shutil.rmtree(s / "sub"),
    # --- sqlite3, which opens from C: plain names
    "sqlite, a str path": lambda s, o, cd: _sqlite(str(s / "new.sqlite")),
    "sqlite, a bytes path": lambda s, o, cd: _sqlite(os.fsencode(s / "new.sqlite")),
    "sqlite, a PathLike": lambda s, o, cd: _sqlite(s / "new.sqlite"),
    "sqlite, a relative path": lambda s, o, cd: (
        cd(s.parent),
        _sqlite(os.path.join("state", "new.sqlite")),
    ),
    "sqlite, an existing database": lambda s, o, cd: _sqlite(s / "ro.sqlite"),
    # --- sqlite3 `file:` URIs. The first is the reviewer's input from #666.
    "sqlite URI, relative, mode=rwc": lambda s, o, cd: (
        cd(s.parent),
        _uri("file:state/new.sqlite?mode=rwc"),
    ),
    "sqlite URI, absolute, no mode": lambda s, o, cd: _uri(f"file:{s}/new.sqlite"),
    "sqlite URI, file:///": lambda s, o, cd: _uri(f"file://{s}/new.sqlite"),
    "sqlite URI, file://localhost/": lambda s, o, cd: _uri(
        f"file://localhost{s}/new.sqlite"
    ),
    "sqlite URI, percent-encoded": lambda s, o, cd: _uri(
        f"file:{s.parent}/%73tate/%6Eew.sqlite?mode=rwc"
    ),
    "sqlite URI, mode=rw on an existing database": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=rw"
    ),
    "sqlite URI, an encoded mode that is not ro": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=%72w"
    ),
    "sqlite URI, with a fragment": lambda s, o, cd: _uri(
        f"file:{s}/new.sqlite#x?mode=ro"
    ),
    # Only a query that is exactly `mode=ro` reads. The first is the reviewer's
    # input: SQLite takes the first `immutable`, a reader of the last let it by.
    "sqlite URI, immutable=0&immutable=1": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?immutable=0&immutable=1"
    ),
    "sqlite URI, mode=ro&mode=rwc": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=ro&mode=rwc"
    ),
    "sqlite URI, mode=rwc&mode=ro": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=rwc&mode=ro"
    ),
    "sqlite URI, mode=ro twice": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=ro&mode=ro"
    ),
    "sqlite URI, immutable=1": lambda s, o, cd: _uri(f"file:{s}/ro.sqlite?immutable=1"),
    "sqlite URI, mode=ro and another parameter": lambda s, o, cd: _uri(
        f"file:{s}/ro.sqlite?mode=ro&cache=shared"
    ),
    "sqlite URI, mode=memory under a name inside": lambda s, o, cd: _uri(
        f"file:{s}/new.sqlite?mode=memory&cache=shared"
    ),
}


@pytest.mark.parametrize("name", sorted(WRITES))
def test_a_write_into_the_state_directory_is_refused(
    state, refusals, monkeypatch, name
):
    outside = state.parent / "outside"
    before = _snapshot(state)
    with pytest.raises(DataDirWriteDuringTest):
        WRITES[name](state, outside, monkeypatch.chdir)
    assert _snapshot(state) == before, "the state directory changed"
    assert refusals, "the refusal was raised but not recorded"
    assert refusals[0].nodeid.endswith(f"[{name}]")


# Operations that are not a write into the state directory. Each returns what
# it produced, so "it was let through" is also "and it worked".
ALLOWED = {
    "reading a file inside": lambda s, o, cd: open(s / "kept.json").read(),
    "listing and stat": lambda s, o, cd: (os.listdir(s), os.stat(s / "kept.json")),
    "sqlite URI mode=ro reads": lambda s, o, cd: (
        sqlite3.connect(f"file:{s}/ro.sqlite?mode=ro", uri=True)
        .execute("SELECT count(*) FROM t")
        .fetchone()
    ),
    "sqlite :memory:": lambda s, o, cd: _sqlite(":memory:"),
    "sqlite URI :memory:": lambda s, o, cd: _uri("file::memory:?cache=shared"),
    "sqlite URI mode=memory, named outside": lambda s, o, cd: (
        cd(o),
        _uri("file:memdb?mode=memory&cache=shared"),
    ),
    "sqlite outside": lambda s, o, cd: (
        _sqlite(o / "a.sqlite"),
        _uri(f"file:{o}/b.sqlite"),
        _uri(f"file://localhost{o}/c.sqlite?mode=rwc"),
        _uri(f"file://{o.parent}/%6Futside/d.sqlite"),
    ),
    "writing outside": lambda s, o, cd: (
        (o / "new").write_text("{}"),
        os.replace(o / "new", o / "moved"),
        os.mkfifo(o / "fifo"),
        os.link(o / "staged", o / "alias"),
    ),
    "a sibling whose name only starts the same": lambda s, o, cd: (
        os.mkdir(str(s) + "-backup"),
        open(os.path.join(str(s) + "-backup", "kept.json"), "w").close(),
    ),
    # `os.rmdir("state", dir_fd=fd)` is how shutil.rmtree - and pytest's own
    # cleanup - removes a temporary directory. Read against the working
    # directory instead of the descriptor, this name is the protected one.
    "by dir_fd, a name the working directory would resolve inside": lambda s, o, cd: (
        cd(s.parent),
        _by_descriptor(
            o,
            lambda fd: (
                os.rmdir("state", dir_fd=fd),
                os.mkdir("state", dir_fd=fd),
                os.close(os.open(os.path.join("state", "new"), CREATE, dir_fd=fd)),
                os.rename("state", "moved", src_dir_fd=fd, dst_dir_fd=fd),
            ),
        ),
    ),
    "a relative path after chdir elsewhere": lambda s, o, cd: (
        cd(o),
        os.makedirs(os.path.join("state", ".inflight")),
        open(os.path.join("state", ".inflight", "job.json"), "w").close(),
    ),
    "writing through a link inside that points out": lambda s, o, cd: (
        s / "link-out" / "via-link"
    ).write_text("{}"),
    "a pipe, which is a descriptor with no path": lambda s, o, cd: (
        lambda r, w: (os.fdopen(w, "w").close(), os.close(r))
    )(*os.pipe()),
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_what_is_not_a_write_into_it_is_let_through(state, refusals, monkeypatch, name):
    before = _snapshot(state)
    ALLOWED[name](state, state.parent / "outside", monkeypatch.chdir)
    assert refusals == []
    assert _snapshot(state) == before


# Arguments the guard cannot turn into a path. For a writing operation that is
# a refusal, not a pass.
UNRESOLVABLE = {
    "a dir_fd that is not open": lambda s: os.mkdir("new", dir_fd=987654),
    "a sqlite URI on another host": lambda s: _uri("file://elsewhere/state/new.sqlite"),
}


@pytest.mark.parametrize("name", sorted(UNRESOLVABLE))
def test_what_cannot_be_resolved_is_refused_not_skipped(state, refusals, name):
    with pytest.raises(DataDirWriteDuringTest, match="cannot be resolved"):
        UNRESOLVABLE[name](state)
    assert len(refusals) == 1


def test_the_guard_is_installed():
    assert data_dir_guard.installed(), (
        "The state-directory guard is not installed, so this run does not "
        "refuse writes into the checkout's data/. Check that "
        "`pytest_configure` in tests/conftest.py still calls "
        "`data_dir_guard.install()`."
    )
    assert os.path.realpath(CHECKOUT_DATA) in data_dir_guard._protected
    # The three functions the audit trail does not cover are wrapped.
    for name in ("open", "mkfifo", "mknod"):
        if hasattr(os, name):
            assert hasattr(getattr(os, name), "__wrapped__"), name


def test_reading_the_checkouts_own_directory_is_not_refused(refusals):
    """Read-only, and no claim about what else is in there."""
    with open(REFERENCE_FILE, encoding="utf-8") as handle:
        assert handle.read(1) == "{"
    os.listdir(CHECKOUT_DATA)
    assert refusals == []


class TestTheRecord:
    def test_a_swallowed_refusal_is_still_recorded(self, state, refusals):
        """`record_spend` catches the failure of its own write on purpose."""
        try:
            (state / "new").write_text("x")
        except Exception:
            pass
        assert len(refusals) == 1
        assert refusals[0].operation == "open for writing"
        assert refusals[0].path == os.path.realpath(state / "new")
        assert refusals[0].nodeid.endswith("test_a_swallowed_refusal_is_still_recorded")

    def test_the_refusal_names_the_line_that_wrote(self, state, refusals):
        with pytest.raises(DataDirWriteDuringTest) as raised:
            open(state / "new", "w")
        assert refusals[0].caller.startswith("tests/test_data_dir_guard.py:")
        assert os.path.realpath(state / "new") in str(raised.value)

    def test_the_report_names_the_test_and_the_path(self, state, refusals):
        try:
            os.mkdir(state / "new")
        except DataDirWriteDuringTest:
            pass
        report = "\n".join(data_dir_guard.summary_lines())
        assert "1 write(s)" in report
        assert "test_the_report_names_the_test_and_the_path" in report
        assert f"os.mkdir {os.path.realpath(state / 'new')}" in report

    def test_the_report_is_empty_when_nothing_was_written(self, refusals):
        assert data_dir_guard.summary_lines() == []


class TestProtectedRoots:
    def test_the_checkout_directory_always(self, monkeypatch):
        monkeypatch.delenv(data_dir_guard.HOST_VARIABLE, raising=False)
        assert data_dir_guard.protected_roots() == (os.path.realpath(CHECKOUT_DATA),)

    def test_the_host_variable_and_the_original_config_value(
        self, monkeypatch, tmp_path
    ):
        outside = tmp_path / "IdealistaRank-data"
        configured = tmp_path / "from-DATA_DIR"
        outside.mkdir()
        configured.mkdir()
        monkeypatch.setenv(data_dir_guard.HOST_VARIABLE, str(outside))
        roots = data_dir_guard.protected_roots(extra=(str(configured),))
        assert roots == (
            os.path.realpath(CHECKOUT_DATA),
            os.path.realpath(configured),
            os.path.realpath(outside),
        )

    @pytest.mark.parametrize("value", ["", "   ", "relative/path", "/"])
    def test_a_value_that_names_no_directory_protects_nothing_extra(
        self, monkeypatch, value
    ):
        """Never the filesystem root: that would refuse every write there is."""
        monkeypatch.setenv(data_dir_guard.HOST_VARIABLE, value)
        assert data_dir_guard.protected_roots() == (os.path.realpath(CHECKOUT_DATA),)


class TestTheSessionRedirect:
    """`Config.DATA_DIR` itself has left the checkout (tests/conftest.py)."""

    def test_the_state_attributes_are_outside_the_checkout(self):
        for name in ("DATA_DIR", "ATTACHMENTS_DIR"):
            value = getattr(Config, name)
            assert not _inside(value, CHECKOUT_DATA), (
                f"Config.{name} = {value} is inside the checkout's data/"
            )

    def test_what_the_services_compute_from_them(self):
        from services import attachments
        from utils import google_spend

        assert not _inside(attachments.attachments_dir(), CHECKOUT_DATA)
        assert not _inside(google_spend.ledger_path(), CHECKOUT_DATA)

    def test_the_in_flight_markers_which_do_not_go_through_data_dir(self):
        """`utils.inflight` names `data/.inflight` relative to the working
        directory, so it has to be moved by name. From the repository root --
        where pytest runs -- the unmoved name is the checkout's own."""
        from utils import inflight

        directory = inflight.inflight_dir()
        assert directory.is_absolute(), directory
        assert not _inside(directory, CHECKOUT_DATA)

    def test_a_job_that_announces_itself_writes_no_marker_in_the_checkout(
        self, refusals, monkeypatch
    ):
        """The thirteen tests the guard caught all reach this call through a
        backfill's `run()`; this is the call itself, from the repository root.
        The checkout's own `data/.inflight` may exist and may hold a real
        job's markers; what is asserted is that this job added none."""
        from utils import inflight

        def ours() -> set[str]:
            return {p.name for p in (CHECKOUT_DATA / ".inflight").glob("guard_probe.*")}

        before = ours()
        monkeypatch.chdir(REPO_ROOT)
        with inflight.inflight("guard_probe", argv=[], resumable=False):
            written = list(inflight.inflight_dir().glob("guard_probe.*.json"))
            assert len(written) == 1
            assert ours() == before
        assert refusals == []
        assert ours() == before


# How long the nested run may take: it imports the app's test environment from
# a cold start.
INNER_PYTEST_TIMEOUT_S = 300

SWALLOWING_TEST = """
import os


def test_a_write_the_code_under_test_swallowed():
    try:
        with open(os.path.join(os.environ["IDEALISTA_DATA_DIR"], "probe"), "w") as handle:
            handle.write("99")
    except Exception:
        pass
"""


def test_a_swallowed_write_fails_the_session_and_is_named(tmp_path):
    """End to end, under the real conftest: the test body passes, the run does
    not, and the summary says which test and which file.

    In-suite this cannot be staged -- the guard would fail the run this test
    is part of -- so a one-test suite runs under a copy of the same conftest
    in a subprocess, the way tests/test_flask_state_isolation.py does it. The
    directory it writes at is a temporary one, protected the way a state
    directory outside the checkout is: by `IDEALISTA_DATA_DIR`.
    """
    suite = tmp_path / "suite"
    host_state = tmp_path / "host-state"
    suite.mkdir()
    host_state.mkdir()
    (suite / "conftest.py").write_text(
        (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (suite / "test_swallows.py").write_text(SWALLOWING_TEST, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            data_dir_guard.HOST_VARIABLE: str(host_state),
        },
        cwd=REPO_ROOT,
        timeout=INNER_PYTEST_TIMEOUT_S,
    )
    output = result.stdout + result.stderr

    assert list(host_state.iterdir()) == [], "the write went through"
    assert "1 passed" in output, output
    assert result.returncode != 0, output
    assert "state directory guard: FAILED" in output, output
    assert "test_a_write_the_code_under_test_swallowed" in output, output
    assert os.path.realpath(host_state / "probe") in output, output
