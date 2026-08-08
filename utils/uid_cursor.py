"""Crash-safe persistence and batch bookkeeping for the IMAP `last_seen_uid`.

Both ingestion services (`services/imap_service.py`,
`services/property_imap_service.py`) used to persist `max(uids)` right after the
IMAP fetch, before `run_ingestion()` had written a single row. Any crash, deploy
restart or DB failure between fetch and commit made those emails invisible
forever: the cursor claimed they were done (issue #24).

The pieces here fix the three parts of that bug in one place:

* `read_uid_file` distinguishes "no cursor yet" from "cursor unreadable" instead
  of turning corruption into a silent full-mailbox reprocess.
* `write_uid_file` writes through a temp file in the target directory and
  `os.replace`, so a crash mid-write cannot leave a truncated cursor behind.
* `UidBatchCursor` only advances over UIDs whose work is finished, so a
  mid-batch failure leaves the unprocessed tail to be re-fetched next run.
"""

import logging
import os
import tempfile
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def read_uid_file(path: str) -> Optional[int]:
    """Return the UID stored in ``path``, or ``None`` when there is no file.

    An empty file reads as ``0`` (a cursor that was reset). Anything else that
    cannot be read as a non-negative integer raises: swallowing it would reset
    the cursor and reprocess the whole mailbox without any signal.
    """
    if not path or not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        raw = (f.read() or "").strip()

    if not raw:
        return 0

    try:
        uid = int(raw)
    except ValueError as e:
        raise ValueError(f"last_seen_uid file {path} is not a valid UID") from e

    if uid < 0:
        raise ValueError(f"last_seen_uid file {path} holds a negative UID: {uid}")

    return uid


def write_uid_file(path: str, uid: int) -> None:
    """Persist ``uid`` to ``path`` atomically (temp file + fsync + os.replace)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".last_seen_uid.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(int(uid)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class UidBatchCursor:
    """Tracks which UIDs of one fetched batch are finished.

    Call `resolve(uid)` once a UID needs no further work — its rows are
    committed, or it was deliberately skipped. `watermark` is then the highest
    UID with no unresolved predecessor inside the batch, i.e. the highest cursor
    value that cannot hide an email whose DB write never landed.
    """

    def __init__(self, uids: Iterable[int], start: int = 0):
        self._uids = sorted({int(u) for u in uids})
        self._pending = set(self._uids)
        self._resolved: set[int] = set()
        self._position = 0
        self._watermark = int(start)

    @property
    def watermark(self) -> int:
        return self._watermark

    @property
    def pending(self) -> set[int]:
        """UIDs of this batch that are still unresolved (failed or untouched)."""
        return set(self._pending)

    def resolve(self, uid: Optional[int]) -> bool:
        """Mark ``uid`` as finished; return True when the watermark moved on.

        Unknown or unparseable UIDs are ignored — a caller that never fetched
        the batch (a patched fetch in tests, a partial fetch failure) must not
        be able to push the cursor past emails nobody looked at.
        """
        if uid is None:
            return False
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return False

        if uid not in self._pending:
            return False

        self._pending.discard(uid)
        self._resolved.add(uid)

        advanced = False
        while (
            self._position < len(self._uids)
            and self._uids[self._position] in self._resolved
        ):
            candidate = self._uids[self._position]
            if candidate > self._watermark:
                self._watermark = candidate
                advanced = True
            self._position += 1

        return advanced
