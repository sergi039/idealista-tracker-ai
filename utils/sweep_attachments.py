"""Reclaim attachment bytes nothing references any more (#430).

Deleting an attachment is soft — the row is marked and the file stays — for two
reasons. One file can be linked from several rows, because storage is
content-addressed and the same document may be attached to two exchanges; and a
document removed by mistake is not recomputable, unlike every other thing this
application stores. So the delete button takes it off the page and this takes
the bytes, later, only when nothing on the page could still want them.

**It reports and exits unless `--apply` is given**, the shape every destructive
utility here has. What it deletes is unrecoverable, so the default has to be
the one that cannot lose anything.

Three refusals are the whole design, and each of them is a way to delete
something that is not garbage:

* a file whose hash **any live row** references is kept — not "the row that
  wrote it", any row, because deduplication means the file has no owner;
* a file **younger than 48 hours** is kept whatever the rows say. `store()`
  writes the bytes and commits the row afterwards, so between those two moments
  the file is a real upload with no row yet — and a sweep in that window would
  delete what the request is about to reference. The grace period dominates any
  HTTP transaction by four orders of magnitude;
* a file in `tmp/`, or named with the in-progress prefix, is not looked at at
  all: that is an upload in flight, not a leftover.

And it moves rather than deletes. `--apply` puts the bytes under
`attachments/.swept/<date>/` and says so; emptying that directory is a separate
decision somebody makes with the file list in front of them.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

# Long enough that no request can straddle it. An upload holds one gunicorn
# thread against a 30 s timeout; this is 5,760 times that.
MIN_AGE_HOURS = 48

SWEPT_DIRNAME = ".swept"


def _walk_files(root: str) -> List[str]:
    """Every stored file, relative to the attachment root.

    `tmp/` and the already-swept directory are skipped: the first is uploads in
    flight, the second is what a previous run moved aside.
    """
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ("tmp", SWEPT_DIRNAME)]
        for name in filenames:
            if name.startswith("."):
                continue
            absolute = os.path.join(dirpath, name)
            found.append(os.path.relpath(absolute, root))
    return found


def survey(root: str, *, now: datetime = None) -> Dict[str, List[str]]:
    """What is on disk, and which of it nothing references any more.

    Returns the three lists a person needs to read before pressing `--apply`:
    what is referenced, what is unreferenced but too young, and what is
    genuinely collectable.
    """
    from models import PropertyAttachment

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=MIN_AGE_HOURS)

    referenced = {
        row.storage_path
        for row in PropertyAttachment.query.filter(
            PropertyAttachment.deleted_at.is_(None)
        ).all()
    }

    kept: List[str] = []
    too_young: List[str] = []
    collectable: List[str] = []

    for relative in _walk_files(root):
        if relative in referenced:
            kept.append(relative)
            continue
        absolute = os.path.join(root, relative)
        try:
            modified = datetime.utcfromtimestamp(os.path.getmtime(absolute))
        except OSError:
            # A file that vanished between the walk and the stat is somebody
            # else's business, not this run's.
            continue
        if modified > cutoff:
            too_young.append(relative)
        else:
            collectable.append(relative)

    return {"kept": kept, "too_young": too_young, "collectable": collectable}


def sweep(root: str, paths: List[str], *, now: datetime = None) -> str:
    """Move the named files aside. Returns the directory they went to."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    destination = os.path.join(root, SWEPT_DIRNAME, now.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(destination, exist_ok=True)

    for relative in paths:
        source = os.path.join(root, relative)
        target = os.path.join(destination, relative.replace(os.sep, "_"))
        shutil.move(source, target)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move the collectable files aside (default: report and exit)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from app import create_app, db  # noqa: F401
    from services import attachments as attachments_service

    app = create_app()
    with app.app_context():
        root = attachments_service.attachments_dir()
        if not os.path.isdir(root):
            logger.info("no attachment directory at %s -- nothing to sweep", root)
            return 0

        result = survey(root)
        logger.info(
            "%s: %d referenced, %d unreferenced but younger than %dh, %d collectable",
            root,
            len(result["kept"]),
            len(result["too_young"]),
            MIN_AGE_HOURS,
            len(result["collectable"]),
        )
        for relative in result["collectable"]:
            logger.info("  collectable: %s", relative)

        if not result["collectable"]:
            return 0
        if not args.apply:
            logger.info("reporting only; pass --apply to move these aside")
            return 0

        destination = sweep(root, result["collectable"])
        logger.info(
            "moved %d file(s) to %s -- delete that directory when you are sure",
            len(result["collectable"]),
            destination,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
