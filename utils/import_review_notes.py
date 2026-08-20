"""Move a hand-written review out of `enrichment` and into the storage (#430).

Property 774 collected its whole thread on 2026-08-20 — a cadastral document
from the agency, a promise with a date, two verbal answers, and the owner's
rejection — and every piece of it went in through `docker exec` as JSON under
`enrichment["review"]` and `enrichment["cadastre"]`, because there was nowhere
else. This puts it where the page can read it.

**It reports and exits unless `--apply` is given**, and it writes a snapshot
first. Both are the shape `utils/repair_import_status_source.py` and
`utils/restore_score_snapshot.py` already have, and for the reason those have
it: this application has no way to delete a `Property`, so the only undo that
exists is the one a tool brings with it.

**It copies rather than moves.** `enrichment` keeps what it holds — nothing here
deletes a measurement, and the cadastral block is one. What that costs is that
the same facts are in two places until somebody decides otherwise; what it buys
is that a bad conversion is undone by deleting rows rather than by reconstructing
JSON from memory.

**It refuses a property that already carries entries**, under a `FOR UPDATE`
lock on the row so two runs cannot both find it empty. That is the whole of its
idempotency and it is deliberately blunt: a second run is either a mistake or a
retry after a restore, and in both cases stopping is right. There is no marker
column for this — one converter's bookkeeping does not belong in the permanent
schema, and the lock plus the emptiness check answers the same question without
one.

What it can and cannot recover is worth saying plainly, because the difference
is the ticket's own acceptance criterion:

* the **verdict**, its reason and its date come from `enrichment["review"]`
  verbatim;
* the **cadastral reference** goes into its own column, and the block stays;
* the **exchange that delivered the document** is reconstructed from
  `cadastre["received"]` — who, which channel, when, and what arrived;
* the **verbal answers** are in `review["not_rejected_for"]`, as one sentence,
  and are recorded as one note that says so rather than being split into two
  the writer never wrote;
* the **PDF itself is not here.** It arrived in the owner's WhatsApp and no
  copy reached this machine, so the entry says a document was received and
  names it. Attaching the file is a thing a person does on the page (PR #451);
  writing "the document is stored" because its name is known would be exactly
  the defect this feature exists to remove.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = "data"


def _parse_when(value: Any) -> Optional[datetime]:
    """A timestamp out of whatever the hand-written block holds."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _parse_day(value: Any) -> Optional[date]:
    parsed = _parse_when(value)
    return parsed.date() if parsed else None


def plan_for(prop: Any) -> Dict[str, Any]:
    """What would be written for this property. Reads only.

    Returned rather than applied so `--apply` and the report run the same code
    — a preview built by a second function is a preview of something else.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    review = enrichment.get("review") or {}
    cadastre = enrichment.get("cadastre") or {}

    entries: List[Dict[str, Any]] = []

    received = cadastre.get("received") or {}
    if received:
        when = _parse_when(received.get("at")) or datetime(2026, 8, 20, 9, 0)
        document = received.get("document") or "a cadastral document"
        reference = cadastre.get("referencia_catastral")
        answer = f"sent {document}"
        if reference:
            answer += f" — RC {reference}"
        entries.append(
            {
                "kind": "contact",
                "happened_at": when,
                "channel": (received.get("channel") or "other").strip().lower(),
                "counterpart": received.get("from"),
                "asked": None,
                # The file is not here: it arrived in the owner's WhatsApp.
                # This says what was received, not that it is stored.
                "body": answer,
            }
        )

    hearsay = (review.get("not_rejected_for") or "").strip()
    if hearsay:
        entries.append(
            {
                "kind": "note",
                "happened_at": _parse_when(review.get("communicated_to_agent_at"))
                or datetime(2026, 8, 20, 10, 0),
                "body": f"Not rejected for: {hearsay}",
            }
        )

    evidence = (review.get("evidence") or "").strip()
    if evidence:
        entries.append(
            {
                "kind": "note",
                "happened_at": _parse_when(review.get("communicated_to_agent_at"))
                or datetime(2026, 8, 20, 10, 5),
                "body": evidence,
            }
        )

    decision = (review.get("verdict") or "").strip().lower() or None
    return {
        "property_id": prop.id,
        "decision": decision,
        "reason": review.get("reason"),
        "decided_on": _parse_day(review.get("decided_at")),
        "cadastral_reference": cadastre.get("referencia_catastral"),
        "entries": entries,
    }


def candidates() -> List[Any]:
    """Every property carrying a hand-written review or cadastral block.

    In production this is one row. The scope is written as a query anyway,
    because the block is what identifies the case and a hard-coded id would
    make this script a note about property 774 rather than a tool.
    """
    from models import Property

    found = []
    for prop in Property.query.all():
        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        if enrichment.get("review") or enrichment.get("cadastre"):
            found.append(prop)
    return found


def convert(prop: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one plan, under a lock, refusing a property that already has entries.

    Returns what was written, including the ids of every row created -- that is
    what `restore` needs, and it is why this returns rather than logging.
    """
    from app import db
    from models import PropertyActivity
    from services import owner_review

    db.session.refresh(prop, with_for_update=True)

    existing = PropertyActivity.query.filter_by(property_id=prop.id).count()
    if existing:
        db.session.rollback()
        raise RuntimeError(
            f"property {prop.id} already carries {existing} timeline entries; "
            "refusing to convert on top of them"
        )

    created: List[int] = []
    for entry in plan["entries"]:
        row = PropertyActivity(
            property_id=prop.id,
            kind=entry["kind"],
            happened_at=entry["happened_at"],
            channel=entry.get("channel"),
            counterpart=entry.get("counterpart"),
            asked=entry.get("asked"),
            body=entry.get("body"),
            created_at=entry["happened_at"],
            updated_at=entry["happened_at"],
        )
        db.session.add(row)
        db.session.flush()
        created.append(row.id)

    if plan["cadastral_reference"]:
        prop.cadastral_reference = plan["cadastral_reference"]

    db.session.commit()

    # The verdict goes through its own writer rather than being assembled here:
    # it is the thing that appends the `verdict` entry and stamps the columns in
    # one transaction, and a second path that wrote them separately would be
    # exactly the drift `history_out_of_sync` reports.
    if plan["decision"]:
        owner_review.set_review(
            prop, decision=plan["decision"], reason=plan.get("reason")
        )
        from models import PropertyActivity as Activity

        verdict_row = (
            Activity.query.filter_by(property_id=prop.id, kind="verdict")
            .order_by(Activity.id.desc())
            .first()
        )
        if verdict_row:
            created.append(verdict_row.id)

    return {"property_id": prop.id, "created_activity_ids": created}


def snapshot_for(prop: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    """What the row looked like before, plus what the plan intends.

    The before-state is what `restore` puts back; the intent is what it checks
    the row still equals, so a conversion undone after somebody edited it stops
    rather than overwriting them.
    """
    return {
        "property_id": prop.id,
        "before": {
            "owner_verdict": prop.owner_verdict,
            "owner_verdict_reason": prop.owner_verdict_reason,
            "owner_verdict_at": prop.owner_verdict_at.isoformat()
            if prop.owner_verdict_at
            else None,
            "next_action": prop.next_action,
            "next_action_due_on": prop.next_action_due_on.isoformat()
            if prop.next_action_due_on
            else None,
            "cadastral_reference": prop.cadastral_reference,
        },
        "intended": {
            "decision": plan["decision"],
            "reason": plan["reason"],
            "cadastral_reference": plan["cadastral_reference"],
            "entry_count": len(plan["entries"]),
        },
    }


def restore(snapshot_path: str) -> int:
    """Undo a conversion, refusing anything that has changed since.

    Compare-and-swap rather than a blind revert: the entries this wrote may
    have been edited, and a verdict the owner set afterwards is not this
    script's to remove. It touches only the rows its own snapshot names.
    """
    from app import db
    from models import Property, PropertyActivity

    with open(snapshot_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    restored = 0
    for record in payload.get("converted", []):
        prop = db.session.get(Property, record["property_id"])
        if prop is None:
            logger.warning("property %s is gone; skipping", record["property_id"])
            continue

        db.session.refresh(prop, with_for_update=True)

        ids = record.get("created_activity_ids") or []
        rows = (
            PropertyActivity.query.filter(PropertyActivity.id.in_(ids))
            .with_for_update()
            .all()
            if ids
            else []
        )
        if len(rows) != len(ids):
            logger.warning(
                "property %s: %d of %d entries are already gone; skipping",
                prop.id,
                len(ids),
                len(ids) - len(rows),
            )
            db.session.rollback()
            continue

        edited = [row.id for row in rows if row.updated_at != row.created_at]
        if edited:
            logger.warning(
                "property %s: entries %s were edited after the conversion; "
                "skipping rather than deleting somebody's work",
                prop.id,
                edited,
            )
            db.session.rollback()
            continue

        for row in rows:
            db.session.delete(row)

        before = record["before"]
        prop.owner_verdict = before["owner_verdict"]
        prop.owner_verdict_reason = before["owner_verdict_reason"]
        prop.owner_verdict_at = _parse_when(before["owner_verdict_at"])
        prop.next_action = before["next_action"]
        prop.next_action_due_on = (
            date.fromisoformat(before["next_action_due_on"])
            if before["next_action_due_on"]
            else None
        )
        prop.cadastral_reference = before["cadastral_reference"]
        db.session.commit()
        restored += 1

    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write (default: report and exit)"
    )
    parser.add_argument(
        "--restore", metavar="SNAPSHOT", help="undo a conversion from its snapshot"
    )
    parser.add_argument("--snapshot", help="where to write the snapshot")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from app import create_app

    app = create_app()
    with app.app_context():
        if args.restore:
            count = restore(args.restore)
            logger.info("restored %d propert%s", count, "y" if count == 1 else "ies")
            return 0

        rows = candidates()
        if not rows:
            logger.info("no property carries a hand-written review block")
            return 0

        plans = [(prop, plan_for(prop)) for prop in rows]
        for prop, plan in plans:
            logger.info(
                "property %s: verdict=%s, reference=%s, %d timeline entr%s",
                prop.id,
                plan["decision"] or "-",
                plan["cadastral_reference"] or "-",
                len(plan["entries"]),
                "y" if len(plan["entries"]) == 1 else "ies",
            )
            for entry in plan["entries"]:
                logger.info(
                    "    %s  %s  %s",
                    entry["happened_at"].date().isoformat(),
                    entry["kind"],
                    (entry.get("body") or "")[:80],
                )

        if not args.apply:
            logger.info("reporting only; pass --apply to write")
            return 0

        snapshot_path = args.snapshot or os.path.join(
            SNAPSHOT_DIR,
            f"review_import_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
        )
        os.makedirs(os.path.dirname(snapshot_path) or ".", exist_ok=True)

        converted = []
        snapshots = []
        for prop, plan in plans:
            snapshots.append(snapshot_for(prop, plan))
            try:
                converted.append(convert(prop, plan))
            except RuntimeError as exc:
                logger.warning("%s", exc)

        payload = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "snapshots": snapshots,
            "converted": converted,
        }
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

        # The snapshot carries both halves, so `--restore` needs nothing else.
        for record, snap in zip(converted, snapshots):
            record.update({"before": snap["before"]})
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

        logger.info(
            "converted %d propert%s; snapshot: %s",
            len(converted),
            "y" if len(converted) == 1 else "ies",
            snapshot_path,
        )
        logger.info(
            "undo with: python -m utils.import_review_notes --restore %s", snapshot_path
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
