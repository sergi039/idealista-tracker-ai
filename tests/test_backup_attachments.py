"""The backup takes the database first and the bytes second (#430).

Two commands in the wrong order produce a restore that looks fine and is not:
a file uploaded between them has a row in the dump and no bytes in the archive,
which after a restore is a download that 404s and is indistinguishable from a
file somebody deleted. The other order leaves an orphan file, which is inert
and which the sweeper reclaims.

So the order is the thing under test, and it is tested by running the script
with `pg_dump`/`docker`/`tar` stubs that record when they were called — the
shape `tests/test_post_merge_hook.py` uses, and for its reason: a test that
greps the source for two strings passes over a script that calls them in either
sequence.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "tools" / "backup_attachments.sh"
READER = SCRIPT.parent / "autopilot" / "lib" / "data_dir.sh"


def _stand_in_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway checkout holding the script and the one reader it asks.

    The script takes its checkout from its own location and asks that
    checkout's reader where the state is (#639). Run in place it would ask
    about the tree the suite runs in: the real `.env` where there is one, and
    a refusal wherever that tree is a linked git worktree.
    """
    project = tmp_path / "checkout"
    lib = project / "tools" / "autopilot" / "lib"
    lib.mkdir(parents=True)
    script = project / "tools" / "backup_attachments.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (lib / "data_dir.sh").write_text(
        READER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return project, script


def _write_stub(path: Path, body: str) -> None:
    # The shebang has to be at byte 0 (issue #284): a blank first line makes
    # execve return ENOEXEC, bash re-executes the stub with itself, and the
    # failure names a binary that was never involved.
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _write_stub(
        bin_dir / "docker",
        f'echo "docker $*" >> {log}\n'
        'if [[ "$*" == *pg_dump* ]]; then echo "PGDUMP-BYTES"; fi\n',
    )
    # `tar -czf <archive> -C <dir> <name>`: the archive is $2. Taking $3 makes
    # the stub run `touch -C`, which fails, and `set -e` then stops the script
    # before it writes anything -- a stub that breaks the thing under test.
    _write_stub(bin_dir / "tar", f'echo "tar $*" >> {log}\ntouch "$2"\n')

    attachments = tmp_path / "data" / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "ab").mkdir()
    (attachments / "ab" / "file.pdf").write_bytes(b"%PDF-1.7\n")

    destination = tmp_path / "backups"
    destination.mkdir()
    project, script = _stand_in_checkout(tmp_path)

    return {
        "project": project,
        "script": script,
        "bin": bin_dir,
        "log": log,
        "attachments": attachments,
        "destination": destination,
    }


def _run(harness, *args, **env_extra):
    env = {k: v for k, v in os.environ.items() if k != "IDEALISTA_DATA_DIR"}
    env["PATH"] = f"{harness['bin']}:{env['PATH']}"
    env["ATTACHMENTS_DIR"] = str(harness["attachments"])
    env["DOCKER_BIN"] = "docker"
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(harness["script"]), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_the_dump_happens_before_the_archive(harness):
    result = _run(harness, str(harness["destination"]))
    assert result.returncode == 0, result.stderr

    calls = harness["log"].read_text().splitlines()
    dump_at = next(i for i, line in enumerate(calls) if "pg_dump" in line)
    tar_at = next(i for i, line in enumerate(calls) if line.startswith("tar"))
    # A file uploaded between the two is then an orphan file rather than an
    # orphan row -- the recoverable half of the asymmetry.
    assert dump_at < tar_at, calls


def test_both_artefacts_are_written(harness):
    _run(harness, str(harness["destination"]))
    produced = sorted(p.name for p in harness["destination"].iterdir())
    assert any(name.endswith(".dump") for name in produced), produced
    assert any(name.endswith(".tar.gz") for name in produced), produced


def test_it_refuses_without_a_destination(harness):
    result = _run(harness)
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
    assert not list(harness["destination"].iterdir())


def test_it_refuses_a_destination_that_does_not_exist(harness):
    result = _run(harness, str(harness["destination"] / "nope"))
    assert result.returncode == 2
    # It does not decide where backups live, and it does not create the place.
    assert not (harness["destination"] / "nope").exists()


def test_a_missing_attachment_directory_is_said_out_loud(harness, tmp_path):
    result = _run(
        harness,
        str(harness["destination"]),
        ATTACHMENTS_DIR=str(tmp_path / "not-here"),
    )
    assert result.returncode == 0
    # "No attachments yet" and "the directory moved" look identical in a log
    # that stays silent about it.
    assert "nothing to archive" in result.stdout
    assert any(p.name.endswith(".dump") for p in harness["destination"].iterdir())


def test_it_deletes_nothing(harness):
    _run(harness, str(harness["destination"]))
    assert (harness["attachments"] / "ab" / "file.pdf").exists()


class TestWhereTheBytesAre:
    """The bytes live in the directory docker-compose.yml binds at /app/data,
    and on the host one reader says where that is (#639). What the reader
    refuses, the backup must not paper over: an archive of the wrong directory
    reports success and holds none of the files the dump's rows point at."""

    def _run(self, harness, *args):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"ATTACHMENTS_DIR", "IDEALISTA_DATA_DIR"}
        }
        env["PATH"] = f"{harness['bin']}:{env['PATH']}"
        env["DOCKER_BIN"] = "docker"
        return subprocess.run(
            ["bash", str(harness["script"]), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def _nothing_was_backed_up(self, harness, result):
        assert result.returncode != 0, result.stdout
        # Before the dump: a dump without its bytes is not half a backup.
        assert (
            not harness["log"].exists() or "pg_dump" not in harness["log"].read_text()
        )
        assert not list(harness["destination"].iterdir())

    def test_the_checkouts_data_directory_is_the_one_archived(self, harness):
        project = harness["project"]
        (project / "data" / "attachments").mkdir(parents=True)
        result = self._run(harness, str(harness["destination"]))
        assert result.returncode == 0, result.stderr
        assert f"archiving {project}/data/attachments" in result.stdout

    def test_a_state_directory_the_reader_refuses_stops_the_backup(
        self, harness, tmp_path
    ):
        """docker-compose.yml does not read IDEALISTA_DATA_DIR yet, so the
        reader refuses a `.env` that names it. The backup stops there, before
        the dump - it archives neither the directory the variable names nor,
        on a guess, the checkout's."""
        state = tmp_path / "state"
        (state / "attachments").mkdir(parents=True)
        project = harness["project"]
        (project / ".env").write_text(f"DB_PASSWORD=x\nIDEALISTA_DATA_DIR={state}\n")
        (project / "data" / "attachments").mkdir(parents=True)
        result = self._run(harness, str(harness["destination"]))
        assert "does not read the variable" in result.stderr
        self._nothing_was_backed_up(harness, result)

    def test_a_refusal_stops_the_backup_whatever_directory_was_named_by_hand(
        self, harness
    ):
        """ATTACHMENTS_DIR names the bytes by hand, but the dump still comes
        from the deployment the reader could not place."""
        (harness["project"] / ".env").write_text("IDEALISTA_DATA_DIR=/elsewhere\n")
        result = _run(harness, str(harness["destination"]))
        self._nothing_was_backed_up(harness, result)

    def test_a_missing_reader_stops_the_backup(self, harness):
        (harness["project"] / "tools" / "autopilot" / "lib" / "data_dir.sh").unlink()
        (harness["project"] / "data" / "attachments").mkdir(parents=True)
        result = self._run(harness, str(harness["destination"]))
        assert "nothing was backed up" in result.stderr
        self._nothing_was_backed_up(harness, result)

    @pytest.mark.parametrize("answer", ["", "echo data"], ids=["nothing", "relative"])
    def test_a_reader_that_names_no_absolute_path_stops_the_backup(
        self, harness, answer
    ):
        """Exit 0 and no path is not an answer: taken as one, the archive
        would be of `/attachments` or of whatever `data` is from here."""
        reader = harness["project"] / "tools" / "autopilot" / "lib" / "data_dir.sh"
        reader.write_text(f"#!/bin/bash\n{answer}\n")
        (harness["project"] / "data" / "attachments").mkdir(parents=True)
        result = self._run(harness, str(harness["destination"]))
        assert "not an absolute path" in result.stderr
        self._nothing_was_backed_up(harness, result)

    def test_a_linked_git_worktree_is_not_backed_up(self, harness):
        """A linked worktree has a data/ of its own that no container mounts:
        its attachments directory is empty or absent, and the backup would be
        a dump with none of the bytes its rows point at."""
        project = harness["project"]
        (project / ".git").write_text("gitdir: /somewhere/.git/worktrees/issue-1\n")
        (project / "data" / "attachments").mkdir(parents=True)
        result = self._run(harness, str(harness["destination"]))
        assert "linked git worktree" in result.stderr
        self._nothing_was_backed_up(harness, result)
