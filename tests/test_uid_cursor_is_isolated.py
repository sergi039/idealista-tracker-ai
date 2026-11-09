"""No test may reach the checkout's mail cursor.

The cursor files default to `DATA_DIR`, which in the main checkout is the
directory the deployment mounts. An ingestion test that did not move the path
overwrote the production cursor with a fake mailbox's UID (99, found on
2026-11-09), and the scheduled runs stopped reaching new mail.
"""

import os

from config import Config
from services.property_imap_service import PropertyIMAPService
from utils.uid_cursor import read_uid_file

CURSOR_ATTRIBUTES = ("LAST_SEEN_UID_PATH", "LAST_SEEN_UID_PROPERTIES_PATH")


def _inside(path: str, directory: str) -> bool:
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    return os.path.commonpath([path, directory]) == directory


def test_cursor_paths_are_outside_the_data_directory():
    for name in CURSOR_ATTRIBUTES:
        path = getattr(Config, name)
        assert not _inside(path, Config.DATA_DIR), (
            f"Config.{name} = {path} is inside DATA_DIR; a test would write "
            "the deployment's cursor"
        )


def test_a_service_that_sets_no_path_saves_outside_the_data_directory():
    """The shape of the test that did the damage: no path of its own."""
    service = PropertyIMAPService()
    assert service._save_last_seen_uid(99) is True

    written = PropertyIMAPService._uid_file_path()
    assert read_uid_file(written) == 99
    assert not _inside(written, Config.DATA_DIR)
