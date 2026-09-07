"""Withdraw a `precise` the stored geocoding record refutes. Dry-run first (#535).

`services/address_agreement.py` is the rule and `PropertyLocationService`
applies it to every geocode from now on. This applies the same reading to the
rows geocoded before it existed, from what they stored -- `query` and
`formatted_address` -- because re-geocoding them is billed and would answer the
same thing: the query is deterministic, and so is Google's answer to it.

    python -m utils.audit_precise_accuracy                           # report only
    python -m utils.audit_precise_accuracy --apply --snapshot data/precise_535.json
    python -m utils.audit_precise_accuracy --restore data/precise_535.json
    python -m utils.audit_precise_accuracy --ids 1379,1680,1445      # a named set

**Scope** is a row whose column says `precise` AND whose geocoding record says
the geocoder wrote it and nothing has overruled it since. A row a person
located is skipped and named, whichever shape the finding took -- a hand-set
block (`enrichment["location"]`), one of the ad-hoc provenance blocks that
predate it, or a coordinate standing on the parcel's own cadastre reference
point (row 774). For those the record describes a point the row no longer
carries, and relabelling the row from that record would be the STATUS-002
mistake pointed the other way.

**What it writes**, per refuted row: `location_accuracy = approximate`; the
record's `accuracy` likewise, with `answered_accuracy: precise` kept beside it
so Google's own word stays legible (the shape `_keep_portal_pin` already uses),
`address_check` naming the state, and `precise_withdrawn` naming this tool and
the time. The coordinate does not move -- it is the same street or village it
was, only worth what that is worth. No score is recomputed: the scorer and the
templates read the row's *current* accuracy (docs/rules/coordinates.md), so the
slack applies on the next read.

Each row is written under its own `FOR UPDATE` through
`services/enrichment_write.locked_write`, the rule for every writer of
`enrichment` (#339), and the verdict is re-read inside the lock. `--apply`
without `--snapshot` is refused: the snapshot is the way back, and
`--restore` is the restore half of `utils/refresh_property_accuracy.py`,
which already refuses to overwrite a location a person set after the snapshot
was taken.

It does not announce itself through `utils.inflight`: it makes no network
call and finishes in well under a second over the whole table, so a deploy
cannot kill it half-way in any way that matters -- every row commits on its
own, and a re-run finds nothing left to do.
"""

import argparse
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from services.address_agreement import (
    earns_precise,
    house_number_agreement,
    house_number_in_formatted,
)
from services.coordinate_quality import PRECISE, is_precise, manual_coordinate

logger = logging.getLogger(__name__)

ACTOR = "utils.audit_precise_accuracy"

# Metres are what `coordinate_quality` compares pins in; a reference point is
# compared in degrees here because it is stored that way and 1e-5 degrees is
# about a metre, far below any move a person makes on purpose.
_SAME_POINT_DEG = 1e-5


def geocoder_record(prop: Any) -> Optional[dict]:
    """The geocoding record, if it is what wrote this row's `precise`.

    A record that says `kept` describes a geocode that was NOT stored, and
    one that says `refused` describes a row with no coordinate; neither is
    the label's origin, whatever its `accuracy` field says.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    record = enrichment.get("geocoding")
    if not isinstance(record, dict) or not is_precise(record.get("accuracy")):
        return None
    if record.get("kept") or record.get("refused"):
        return None
    return record


def _on_point(prop: Any, point: Any) -> bool:
    try:
        lat, lon = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    if prop.location_lat is None or prop.location_lon is None:
        return False
    return (
        abs(float(prop.location_lat) - lat) <= _SAME_POINT_DEG
        and abs(float(prop.location_lon) - lon) <= _SAME_POINT_DEG
    )


def person_established(prop: Any) -> Optional[str]:
    """Why this row's coordinate is a person's rather than the geocoder's."""
    if manual_coordinate(prop) is not None:
        return "hand-set location"
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    if isinstance(enrichment.get("coordinate_provenance"), dict):
        return "ad-hoc provenance block"
    cadastre = enrichment.get("cadastre")
    point = cadastre.get("reference_point") if isinstance(cadastre, dict) else None
    if _on_point(prop, point):
        return "standing on the cadastre reference point"
    return None


def read_verdict(prop: Any) -> Dict[str, Any]:
    """What this tool would do with the row, and why. Reads, never writes."""
    if not is_precise(prop.location_accuracy):
        return {"action": "skip", "why": "not precise", "record": None}
    record = geocoder_record(prop)
    if record is None:
        return {
            "action": "skip",
            "why": "the label is not the geocoder's",
            "record": None,
        }
    who = person_established(prop)
    if who:
        return {"action": "skip", "why": f"person-established: {who}", "record": record}
    state = house_number_agreement(
        record.get("query"), house_number_in_formatted(record.get("formatted_address"))
    )
    action = "keep" if earns_precise(state) else "withdraw"
    return {"action": action, "why": state, "record": record}


def withdraw(prop: Any, *, now: Optional[datetime] = None) -> bool:
    """Relabel one row under its lock. True if it was withdrawn, False if the
    locked re-read said there was nothing to withdraw."""
    from services.enrichment_write import check_writable, locked_write

    locked = check_writable(prop, True)
    with locked_write(prop, locked=locked, commit=True):
        verdict = read_verdict(prop)
        if verdict["action"] != "withdraw":
            return False
        record = dict(verdict["record"])
        record["answered_accuracy"] = PRECISE
        record["accuracy"] = "approximate"
        record["address_check"] = verdict["why"]
        record["precise_withdrawn"] = {
            "by": ACTOR,
            "at": (now or datetime.now(timezone.utc)).isoformat(),
        }
        enrichment = dict(prop.enrichment or {})
        enrichment["geocoding"] = record
        prop.location_accuracy = "approximate"
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Withdraw a `precise` the stored geocoding record refutes (#535)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Without it, report only."
    )
    parser.add_argument(
        "--snapshot",
        help="Write a rollback snapshot here first. Required with --apply.",
    )
    parser.add_argument("--restore", help="Restore a snapshot and exit.")
    parser.add_argument(
        "--ids", help="Comma-separated property ids instead of every precise row."
    )
    args = parser.parse_args(argv)
    if args.apply and not args.snapshot:
        parser.error(
            "--apply needs --snapshot PATH: this rewrites labels, and the "
            "snapshot is the only way back"
        )

    # Reuse a context the caller already opened rather than standing up a
    # second application over a second database -- `utils/route_profile.py`'s
    # rule, for the reason it gives: a CLI that insists on its own
    # `create_app()` cannot be driven in-process.
    from flask import current_app, has_app_context

    if has_app_context() and current_app.extensions.get("sqlalchemy") is _db():
        return _run(args)

    from app import create_app

    with create_app().app_context():
        return _run(args)


def _db():
    from app import db

    return db


def _run(args) -> int:
    from app import db
    from models import Property
    from utils.refresh_property_accuracy import (
        _parse_ids,
        _restore,
        _snapshot_row,
        _write_snapshot,
    )

    if args.restore:
        count = _restore(args.restore)
        logger.info("Restored %d row(s) from %s", count, args.restore)
        return 0

    query = db.session.query(Property).filter(Property.location_accuracy == PRECISE)
    if args.ids:
        query = query.filter(Property.id.in_(_parse_ids(args.ids)))
    rows: List[Property] = query.order_by(Property.id).all()

    verdicts = [(prop, read_verdict(prop)) for prop in rows]
    actions = Counter(verdict["action"] for _, verdict in verdicts)
    logger.info(
        "%d precise row(s) in scope: %s",
        len(rows),
        ", ".join(f"{k}={v}" for k, v in sorted(actions.items())) or "none",
    )
    for prop, verdict in verdicts:
        if verdict["action"] == "skip":
            logger.info("  id=%s skip: %s", prop.id, verdict["why"])
    for prop, verdict in verdicts:
        if verdict["action"] == "withdraw":
            record = verdict["record"]
            logger.info(
                "  id=%s WITHDRAW (%s): %r -> %r",
                prop.id,
                verdict["why"],
                record.get("query"),
                record.get("formatted_address"),
            )

    to_write = [prop for prop, verdict in verdicts if verdict["action"] == "withdraw"]
    if not args.apply:
        logger.info(
            "Dry run: nothing written. %d row(s) would be withdrawn; re-run with "
            "--apply --snapshot PATH.",
            len(to_write),
        )
        return 0

    _write_snapshot([_snapshot_row(prop) for prop in to_write], args.snapshot)
    done = sum(1 for prop in to_write if withdraw(prop))
    logger.info(
        "Withdrew `precise` from %d row(s); the way back is --restore %s",
        done,
        args.snapshot,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
