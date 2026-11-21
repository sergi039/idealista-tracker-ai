"""The app does not start on the directory its state was moved out of
(issue #639).

`tools/move_data_dir.sh` moves the deployment's state out of the checkout and
leaves `data/.moved-to` behind. If a container is ever started on that
directory again -- a hand `docker compose up` after the `IDEALISTA_DATA_DIR`
line was lost from `.env` -- it finds four reference files and no mail cursor,
and ingestion re-reads the mailbox from the beginning: the incident of
2026-11-09, reached by a different road.

Nothing about that state is an error to the app unless the app says so. So
`_validate_config` refuses it at startup, and a refused start is loud: the
container never answers healthz.
"""

from __future__ import annotations

import os
import threading

import pytest

from app import _state_directory_problem, _validate_config
from config import Config

VALID = {"TESTING": False, "DATABASE_URL": "sqlite://"}


def test_the_tombstone_name_is_the_one_the_tools_write():
    """One spelling on both sides of the bind mount: the mover and the shell
    reader on the host, this check in the container."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert Config.STATE_TOMBSTONE_NAME == ".moved-to"
    reader = (root / "tools" / "autopilot" / "lib" / "data_dir.sh").read_text(
        encoding="utf-8"
    )
    assert 'IDEALISTA_DATA_DIR_TOMBSTONE=".moved-to"' in reader


def test_an_ordinary_state_directory_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".last_seen_uid_properties").write_text("4711\n")
    assert _state_directory_problem(str(tmp_path)) is None
    _validate_config(dict(VALID))


def test_a_directory_that_does_not_exist_yet_is_not_a_tombstone(tmp_path):
    assert _state_directory_problem(str(tmp_path / "not-created-yet")) is None


def test_a_tombstoned_directory_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".moved-to").write_text(
        "moved_to=/Users/ss/IdealistaRank-data\nmoved_at=20261112T000000Z\n"
    )
    with pytest.raises(ValueError) as raised:
        _validate_config(dict(VALID))
    message = str(raised.value)
    assert ".moved-to" in message
    assert "/Users/ss/IdealistaRank-data" in message
    assert "IDEALISTA_DATA_DIR" in message
    assert "--rollback" in message


def test_an_empty_tombstone_is_still_one(tmp_path, monkeypatch):
    """Its presence is the fact; what it records is a convenience."""
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".moved-to").write_text("")
    with pytest.raises(ValueError, match="another directory"):
        _validate_config(dict(VALID))


def test_an_unreadable_tombstone_is_still_one(tmp_path):
    (tmp_path / ".moved-to").mkdir()
    problem = _state_directory_problem(str(tmp_path))
    assert problem is not None and ".moved-to" in problem


def test_a_dangling_link_of_that_name_is_still_one(tmp_path):
    """`os.path.exists` follows the link and says nothing is there."""
    (tmp_path / ".moved-to").symlink_to(tmp_path / "gone")
    problem = _state_directory_problem(str(tmp_path))
    assert problem is not None and ".moved-to" in problem


# The tombstone is the FACT that the entry exists; what it holds is a hint.
# No kind of entry may turn the refusal into a wait or into another error
# (review of #700).


def _problem_within(seconds, data_dir):
    """`_state_directory_problem` on a thread: a startup check that waits on
    the tombstone is a red test, not a run that never ends."""
    answer = []
    worker = threading.Thread(
        target=lambda: answer.append(_state_directory_problem(str(data_dir))),
        daemon=True,
    )
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        # Let the reader go, so the thread does not outlive the test.
        try:
            os.close(os.open(data_dir / ".moved-to", os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        pytest.fail("the startup check is still waiting on the tombstone")
    return answer[0]


@pytest.mark.parametrize("through_a_link", [False, True], ids=["fifo", "link to fifo"])
def test_a_fifo_of_that_name_stops_the_start_and_is_not_read(tmp_path, through_a_link):
    """`open()` on a FIFO with no writer never returns: read for its hint, the
    tombstone would hang every start instead of refusing it."""
    if through_a_link:
        os.mkfifo(tmp_path / "pipe")
        (tmp_path / ".moved-to").symlink_to(tmp_path / "pipe")
    else:
        os.mkfifo(tmp_path / ".moved-to")
    problem = _problem_within(10, tmp_path)
    assert problem is not None and ".moved-to" in problem
    assert "another directory" in problem


def test_a_tombstone_that_is_not_text_is_still_one(tmp_path, monkeypatch):
    """One byte that is not UTF-8 used to raise UnicodeDecodeError past the
    OSError handler: the start died without saying what was wrong."""
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".moved-to").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="another directory"):
        _validate_config(dict(VALID))


def test_a_tombstone_that_may_not_be_read_is_still_one(tmp_path):
    tombstone = tmp_path / ".moved-to"
    tombstone.write_text("moved_to=/elsewhere\n")
    tombstone.chmod(0)
    problem = _state_directory_problem(str(tmp_path))
    assert problem is not None and ".moved-to" in problem
    if os.geteuid() != 0:  # root reads it anyway
        assert "/elsewhere" not in problem and "another directory" in problem


def test_what_the_tombstone_says_reaches_the_message_as_one_clean_line(tmp_path):
    """The message goes to the container's log. A hint with escape codes,
    tabs and line breaks is reduced to its printable characters."""
    (tmp_path / ".moved-to").write_bytes(b"moved_to=/x\x1b[31m\ty\r\nmoved_at=1\n")
    problem = _state_directory_problem(str(tmp_path))
    assert "moved to /x[31my and" in problem
    assert not any(ch in problem for ch in "\x1b\t\r\n")


def test_only_the_head_of_a_large_tombstone_is_read(tmp_path):
    (tmp_path / ".moved-to").write_bytes(b"a" * 8192 + b"\nmoved_to=/late\n")
    problem = _state_directory_problem(str(tmp_path))
    assert "/late" not in problem and "another directory" in problem


def test_the_suite_itself_is_not_checked(tmp_path, monkeypatch):
    """`TESTING` skips startup validation as a whole; this is not a new
    exemption, and the session's state directory is a temporary one anyway."""
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".moved-to").write_text("moved_to=/elsewhere\n")
    _validate_config({"TESTING": True})
