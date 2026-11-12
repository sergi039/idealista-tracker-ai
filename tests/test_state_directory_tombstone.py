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


def test_the_suite_itself_is_not_checked(tmp_path, monkeypatch):
    """`TESTING` skips startup validation as a whole; this is not a new
    exemption, and the session's state directory is a temporary one anyway."""
    monkeypatch.setattr(Config, "DATA_DIR", str(tmp_path))
    (tmp_path / ".moved-to").write_text("moved_to=/elsewhere\n")
    _validate_config({"TESTING": True})
