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

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

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
    _write_atomically(path, str(int(uid)))


def _write_atomically(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".last_seen_uid.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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


class HeldUids:
    """Emails that could not be stored, waiting for another try.

    One refused email used to hold the cursor behind it, and every later run
    re-read the same oldest batch and never reached new mail: measured on
    production 2026-11-10, a fotocasa page that answered "blocked" for UID
    494344 kept 273 newer emails unread. The email still must not be lost, so
    it moves here -- the cursor passes it and each run retries a bounded number
    of the least recently tried.

    A missing file is an empty list; an unreadable one raises, for the reason
    `read_uid_file` does: reading it as empty would forget every held email
    without a signal.
    """

    def __init__(self, path: str):
        self.path = path
        self._entries: Dict[int, Dict[str, object]] = {}
        if not path or not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
            self._entries = {int(uid): dict(entry) for uid, entry in data.items()}
        except (ValueError, TypeError, AttributeError) as e:
            raise ValueError(f"held-UID file {path} is not readable") from e

    def __contains__(self, uid: object) -> bool:
        return uid in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def uids(self) -> List[int]:
        return sorted(self._entries)

    def due(self, limit: int) -> List[int]:
        """Up to ``limit`` held UIDs, least recently tried first."""
        ordered = sorted(
            self._entries,
            key=lambda uid: (str(self._entries[uid].get("last_attempt_at")), uid),
        )
        return sorted(ordered[: max(0, int(limit))])

    def hold(self, uid: int, reason: str) -> None:
        """Record one more failed try of ``uid`` and persist; raises on failure."""
        now = datetime.now(timezone.utc).isoformat()
        previous = self._entries.get(uid)
        entry = dict(previous or {"first_held_at": now, "attempts": 0})
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_attempt_at"] = now
        entry["reason"] = reason
        self._entries[uid] = entry
        try:
            self._save()
        except BaseException:
            if previous is None:
                del self._entries[uid]
            else:
                self._entries[uid] = previous
            raise

    def release(self, uids: Iterable[int]) -> None:
        """Forget ``uids`` (stored at last, or gone from the mailbox)."""
        gone = [uid for uid in uids if uid in self._entries]
        if not gone:
            return
        kept = dict(self._entries)
        for uid in gone:
            del self._entries[uid]
        try:
            self._save()
        except BaseException:
            self._entries = kept
            raise

    def _save(self) -> None:
        payload = {str(uid): self._entries[uid] for uid in sorted(self._entries)}
        _write_atomically(self.path, json.dumps(payload, indent=1))
