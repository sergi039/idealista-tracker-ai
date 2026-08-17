"""Take back a claim the importer never had the evidence for (STATUS-002, #265).

An out-of-band import script wrote `listing_status_source = 'manual'` on every
row it created, reasoning that the row was entered by hand. That column answers
a different question -- *who established that this listing is live* -- and
`services/listing_verification.py` reads it as such. So 324 listings nobody had
ever opened were reported as verified, and the coverage line above the table
counted them.

One word, two meanings. `source_email_id = 'manual:…'` is the correct
bookkeeping fact and stays; the copy of that word into a provenance column is
the claim about the world, and it goes.

**The condition is narrower than the defect**, deliberately. `manual` is also
what the owner's own status button writes, and that verdict is real. Measured
against production on 2026-08-17:

    listing_status_source = 'manual'                                332
      ... and source_email_id LIKE 'manual:%'  (the importer's)     324
      ... and source_email_id NOT LIKE 'manual:%'  (the button's)     8
    of the 324, rows carrying a listing_last_checked                  0
    of the 324, rows whose status is not 'active'                     0

The eight are the owner's, made since the ticket was written, and this must not
touch them. The two zeroes are the corroboration: a real check stamps
`listing_last_checked`, and not one of the 324 has one -- so the narrowing is
not merely plausible, it is confirmed by a column the importer never wrote.

NULL rather than `ingest`, because the app did not ingest them either. NULL is
how this schema says nobody knows, and `read_verdict` renders it `unchecked`.

Reports and exits unless `--apply` is given. Writes a snapshot of exactly what
it is about to overwrite first -- `--no-backup` is a thing you say out loud,
never a default -- because rolling the app back does not undo a data rewrite.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# What the importer wrote into `source_email_id`. The prefix is what separates
# its rows from the owner's hand-set verdicts, and it is the whole safety of
# this script.
IMPORTER_PREFIX = "manual:"

DEFAULT_SNAPSHOT_DIR = "data"


def _rows_to_repair():
    from models import Property

    return (
        Property.query.filter(
            Property.listing_status_source == "manual",
            Property.source_email_id.like(f"{IMPORTER_PREFIX}%"),
        )
        .order_by(Property.id)
        .all()
    )


def _protected_rows():
    """The hand-set verdicts this must leave alone -- reported, never touched."""
    from models import Property

    return (
        Property.query.filter(
            Property.listing_status_source == "manual",
            ~Property.source_email_id.like(f"{IMPORTER_PREFIX}%"),
        )
        .order_by(Property.id)
        .all()
    )


def _snapshot(rows) -> Dict[str, Any]:
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "reason": "STATUS-002: importer wrote 'manual' into listing_status_source",
        "column": "listing_status_source",
        "rows": [
            {
                "id": row.id,
                "source_email_id": row.source_email_id,
                "listing_status": row.listing_status,
                "listing_status_source": row.listing_status_source,
                "listing_last_checked": row.listing_last_checked.isoformat()
                if row.listing_last_checked
                else None,
            }
            for row in rows
        ],
    }


def _write_snapshot(payload: Dict[str, Any], path: str) -> None:
    """Write the snapshot, then fsync, then rename into place.

    The rollback for a data rewrite must not itself be a partial file: a
    truncated JSON is indistinguishable from a missing one exactly when it is
    needed. Temporary file in the target directory so the rename is atomic.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


def restore(path: str, apply: bool = False) -> Dict[str, Any]:
    """Put a snapshot back. The way out, and it is tested."""
    from app import db
    from models import Property

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    rows: List[Dict[str, Any]] = payload.get("rows") or []
    restored = 0
    missing = 0
    for entry in rows:
        prop = db.session.get(Property, entry["id"])
        if prop is None:
            missing += 1
            continue
        prop.listing_status_source = entry.get("listing_status_source")
        restored += 1

    if apply:
        db.session.commit()
    else:
        db.session.rollback()

    return {"restored": restored, "missing": missing, "applied": bool(apply)}


def repair(
    apply: bool = False, snapshot_path: str = "", backup: bool = True
) -> Dict[str, Any]:
    from app import db

    rows = _rows_to_repair()
    protected = _protected_rows()

    summary: Dict[str, Any] = {
        "to_repair": len(rows),
        "protected_hand_set": len(protected),
        "protected_ids": [row.id for row in protected],
        "with_a_recorded_check": sum(
            1 for row in rows if row.listing_last_checked is not None
        ),
        "not_active": sum(
            1 for row in rows if (row.listing_status or "active") != "active"
        ),
        "applied": False,
        "snapshot": None,
    }

    if not rows:
        return summary

    if apply and backup:
        path = snapshot_path or os.path.join(
            DEFAULT_SNAPSHOT_DIR,
            f"status_source_repair_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json",
        )
        _write_snapshot(_snapshot(rows), path)
        summary["snapshot"] = path

    if not apply:
        return summary

    for row in rows:
        row.listing_status_source = None
    db.session.commit()
    summary["applied"] = True
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write; otherwise only report"
    )
    parser.add_argument("--snapshot", default="", help="where to write the snapshot")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the snapshot (there is then no way back)",
    )
    parser.add_argument("--restore", default="", help="put a snapshot back")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from app import create_app

    app = create_app()
    with app.app_context():
        if args.restore:
            outcome = restore(args.restore, apply=args.apply)
            logger.info(json.dumps(outcome, indent=2, ensure_ascii=False))
            if not args.apply:
                logger.info("Report only. Re-run with --apply to restore.")
            return 0

        outcome = repair(
            apply=args.apply,
            snapshot_path=args.snapshot,
            backup=not args.no_backup,
        )
        logger.info(json.dumps(outcome, indent=2, ensure_ascii=False))
        if not args.apply:
            logger.info(
                "Report only. %s row(s) would lose a claim nobody made; "
                "%s hand-set verdict(s) would be left alone. "
                "Re-run with --apply to write.",
                outcome["to_repair"],
                outcome["protected_hand_set"],
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
