"""A test run cannot write the deployment's state directory (issue #639).

The main checkout's `data/` is the directory docker-compose.yml mounts at
`/app/data` on the machine that runs the app, and `pytest` runs in that
checkout. `tests/data_dir_guard.py` refuses every write whose real path lands
inside it; this file is the proof that it does, operation by operation, and
that a refusal the code under test swallows still fails the session.

Every scenario writes on purpose, so every scenario is built to do no damage
on the day the guard is broken: the names are probes that do not exist, the
destructive calls are aimed at nothing (`os.remove` of a missing file raises
`FileNotFoundError` and removes nothing), and a fixture clears whatever a
failed guard let through. A test of a guard that deletes a tracked file when
the guard regresses would be the incident it is written against.

Deliberately not gated on `installed()`: the guard has no off switch, so
"not installed" is a defect, and `test_the_guard_is_installed` says so by name.
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
PROBE_NAME = f".data-dir-guard-probe-{os.getpid()}"

# The four committed reference files; reading them is what half the suite does.
REFERENCE_FILE = CHECKOUT_DATA / "ine_municipal.json"


def _inside(path: str | os.PathLike, directory: str | os.PathLike) -> bool:
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    return os.path.commonpath([path, directory]) == directory


def _remove_leftover(path: Path) -> None:
    """Remove a probe that a broken guard let through.

    The guard is switched off for the length of the cleanup, so a half-working
    one cannot refuse it, and then put back exactly as it was -- NOT installed
    afresh: a cleanup that installed the guard would repair the very defect
    `test_the_guard_is_installed` exists to report, and every test after the
    first would pass against a conftest that never installed it (measured:
    with the `install()` call removed, 38 of 41 tests here still passed).
    """
    was_active = data_dir_guard._active
    data_dir_guard._active = False
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    finally:
        data_dir_guard._active = was_active


@pytest.fixture
def probe():
    """A path inside the checkout's state directory that does not exist, and
    still does not exist afterwards whatever the guard did."""
    path = CHECKOUT_DATA / PROBE_NAME
    assert not path.exists() and not path.is_symlink()
    yield path
    _remove_leftover(path)


@pytest.fixture
def refusals():
    """The attempts made by this test, kept out of the session's own record."""
    with data_dir_guard.capture_attempts() as attempts:
        yield attempts


def test_the_guard_is_installed():
    assert data_dir_guard.installed(), (
        "The state-directory guard is not installed, so this run does not "
        "refuse writes into the checkout's data/. Check that "
        "`pytest_configure` in tests/conftest.py still calls "
        "`data_dir_guard.install()`."
    )
    assert os.path.realpath(CHECKOUT_DATA) in data_dir_guard._protected


class TestRefusedOperations:
    def test_open_for_writing(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest) as raised:
            open(probe, "w")
        assert not probe.exists()
        assert str(probe) in str(raised.value)
        assert [a.operation for a in refusals] == ["open for writing"]
        assert refusals[0].path == os.path.realpath(probe)

    @pytest.mark.parametrize("mode", ["w", "a", "x", "r+", "wb", "ab"])
    def test_every_writing_mode(self, probe, refusals, mode):
        with pytest.raises(DataDirWriteDuringTest):
            open(probe, mode)
        assert not probe.exists()
        assert len(refusals) == 1

    def test_os_open_with_o_creat(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        assert not probe.exists()

    def test_pathlib_write_text_and_touch(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            probe.write_text("99\n")
        with pytest.raises(DataDirWriteDuringTest):
            probe.touch()
        assert not probe.exists()

    def test_a_relative_path_from_the_repository_root(
        self, probe, refusals, monkeypatch
    ):
        """The shape of the cwd-relative `data/...` literals in utils/."""
        monkeypatch.chdir(REPO_ROOT)
        with pytest.raises(DataDirWriteDuringTest):
            open(os.path.join("data", PROBE_NAME), "w")
        assert not probe.exists()

    def test_mkdir_and_makedirs(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            os.mkdir(probe)
        with pytest.raises(DataDirWriteDuringTest):
            os.makedirs(probe / "nested" / "deeper", exist_ok=True)
        assert not probe.exists()

    def test_a_rename_into_the_directory(self, probe, refusals, tmp_path):
        """`os.replace` is how the cursor and every snapshot are written."""
        source = tmp_path / "staged"
        source.write_text("99\n")
        with pytest.raises(DataDirWriteDuringTest):
            os.replace(source, probe)
        with pytest.raises(DataDirWriteDuringTest):
            os.rename(source, probe)
        assert not probe.exists()
        assert source.read_text() == "99\n"

    def test_a_rename_out_of_the_directory(self, probe, refusals, tmp_path):
        with pytest.raises(DataDirWriteDuringTest):
            os.rename(probe, tmp_path / "taken")
        assert not (tmp_path / "taken").exists()

    def test_remove_rmdir_and_rmtree(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            os.remove(probe)
        with pytest.raises(DataDirWriteDuringTest):
            os.rmdir(probe)
        with pytest.raises(DataDirWriteDuringTest):
            shutil.rmtree(probe)
        assert [a.operation for a in refusals] == [
            "os.remove",
            "os.rmdir",
            "shutil.rmtree",
        ]

    def test_metadata_changes(self, probe, refusals):
        for operation in (
            lambda: os.chmod(probe, 0o600),
            lambda: os.utime(probe, None),
            lambda: os.truncate(probe, 0),
        ):
            with pytest.raises(DataDirWriteDuringTest):
                operation()
        assert len(refusals) == 3

    def test_a_symlink_created_inside(self, probe, refusals, tmp_path):
        with pytest.raises(DataDirWriteDuringTest):
            os.symlink(tmp_path, probe)
        assert not probe.is_symlink()

    def test_a_write_through_a_symlink_that_points_inside(
        self, probe, refusals, tmp_path
    ):
        """A link outside the directory is a write into it once resolved."""
        link = tmp_path / "looks-harmless"
        link.symlink_to(CHECKOUT_DATA, target_is_directory=True)
        with pytest.raises(DataDirWriteDuringTest):
            open(link / PROBE_NAME, "w")
        assert not probe.exists()

    def test_sqlite_which_opens_from_c(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            sqlite3.connect(str(probe))
        assert not probe.exists()

    def test_an_absolute_path_ignores_a_directory_descriptor(
        self, probe, refusals, tmp_path
    ):
        """The kernel ignores `dir_fd` for an absolute path, so the guard may
        not use its presence as a reason to look away."""
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            with pytest.raises(DataDirWriteDuringTest):
                os.mkdir(probe, dir_fd=fd)
            with pytest.raises(DataDirWriteDuringTest):
                os.remove(probe, dir_fd=fd)
        finally:
            os.close(fd)
        assert not probe.exists()

    def test_the_directory_itself(self, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            os.chmod(CHECKOUT_DATA, 0o755)


class TestWhatIsNotRefused:
    def test_reading_a_committed_reference_file(self, refusals):
        with open(REFERENCE_FILE, encoding="utf-8") as handle:
            assert handle.read(1) == "{"
        assert os.listdir(CHECKOUT_DATA)
        assert refusals == []

    def test_writing_anywhere_else(self, refusals, tmp_path):
        target = tmp_path / "data" / "snapshot.json"
        target.parent.mkdir()
        target.write_text("{}")
        os.replace(target, tmp_path / "data" / "moved.json")
        assert refusals == []

    def test_a_sibling_whose_name_only_starts_the_same(self, refusals, tmp_path):
        """`<root>-backup` is not inside `<root>`."""
        root = tmp_path / "state"
        sibling = tmp_path / "state-backup"
        root.mkdir()
        sibling.mkdir()
        before = data_dir_guard._protected
        data_dir_guard._protected = (os.path.realpath(root),)
        try:
            (sibling / "kept.json").write_text("{}")
            with pytest.raises(DataDirWriteDuringTest):
                (root / "refused.json").write_text("{}")
        finally:
            data_dir_guard._protected = before
        assert [a.path for a in refusals] == [os.path.realpath(root / "refused.json")]

    def test_a_name_relative_to_a_directory_descriptor(
        self, refusals, tmp_path, monkeypatch
    ):
        """`os.rmdir("data", dir_fd=fd)` is how `shutil.rmtree` removes a
        temporary `data/`, and pytest's own cleanup does exactly that. Read
        against the current directory it would name the checkout's."""
        monkeypatch.chdir(REPO_ROOT)
        (tmp_path / "data").mkdir()
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            os.rmdir("data", dir_fd=fd)
            os.mkdir("data", dir_fd=fd)
            os.rename("data", "moved", src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        assert (tmp_path / "moved").is_dir()
        assert CHECKOUT_DATA.is_dir()
        assert refusals == []

    def test_a_relative_data_path_after_chdir(self, refusals, tmp_path, monkeypatch):
        """What a test that moved to its own directory is entitled to."""
        monkeypatch.chdir(tmp_path)
        os.makedirs(os.path.join("data", ".inflight"))
        with open(os.path.join("data", ".inflight", "job.json"), "w") as handle:
            handle.write("{}")
        assert refusals == []


class TestTheRecord:
    def test_a_swallowed_refusal_is_still_recorded(self, probe, refusals):
        """`record_spend` catches the failure of its own write on purpose."""
        try:
            probe.write_text("x")
        except Exception:
            pass
        assert len(refusals) == 1
        assert refusals[0].nodeid.endswith("test_a_swallowed_refusal_is_still_recorded")

    def test_the_refusal_names_the_line_that_wrote(self, probe, refusals):
        with pytest.raises(DataDirWriteDuringTest):
            open(probe, "w")
        assert refusals[0].caller.startswith("tests/test_data_dir_guard.py:")

    def test_the_report_names_the_test_and_the_path(self, probe, refusals):
        try:
            os.mkdir(probe)
        except DataDirWriteDuringTest:
            pass
        report = "\n".join(data_dir_guard.summary_lines())
        assert "1 write(s)" in report
        assert "test_the_report_names_the_test_and_the_path" in report
        assert f"os.mkdir {os.path.realpath(probe)}" in report

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
        backfill's `run()`; this is the call itself, from the repository root."""
        from utils import inflight

        monkeypatch.chdir(REPO_ROOT)
        with inflight.inflight("guard_probe", argv=[], resumable=False):
            written = list(inflight.inflight_dir().glob("guard_probe.*.json"))
            assert len(written) == 1
        assert refusals == []
        assert not (CHECKOUT_DATA / ".inflight").exists()


# How long the nested run may take: it imports the app's test environment from
# a cold start.
INNER_PYTEST_TIMEOUT_S = 300

SWALLOWING_TEST = """
import os


def test_a_write_the_code_under_test_swallowed():
    try:
        with open(os.path.join({data!r}, {probe!r}), "w") as handle:
            handle.write("99")
    except Exception:
        pass
"""


def test_a_swallowed_write_fails_the_session_and_is_named(tmp_path):
    """End to end, under the real conftest: the test body passes, the run does
    not, and the summary says which test and which file.

    In-suite this cannot be staged -- the guard would fail the run this test
    is part of -- so a one-test suite runs under a copy of the same conftest
    in a subprocess, the way tests/test_flask_state_isolation.py does it.
    """
    probe = PROBE_NAME + "-session"
    (tmp_path / "conftest.py").write_text(
        (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "test_swallows.py").write_text(
        SWALLOWING_TEST.format(data=str(CHECKOUT_DATA), probe=probe),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT,
        timeout=INNER_PYTEST_TIMEOUT_S,
    )
    output = result.stdout + result.stderr
    went_through = (CHECKOUT_DATA / probe).exists()
    _remove_leftover(CHECKOUT_DATA / probe)
    assert not went_through, "the write went through"
    assert "1 passed" in output, output
    assert result.returncode != 0, output
    assert "state directory guard: FAILED" in output, output
    assert "test_a_write_the_code_under_test_swallowed" in output, output
    assert os.path.realpath(CHECKOUT_DATA / probe) in output, output
