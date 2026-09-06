"""The photograph a yaencontre alert email carries, for rows already here.

Free, and the only path that reaches these rows at all. yaencontre answers
DataDome to every request from both machines, so the alert email has always
been the only source for them -- and #548 captures the card's photograph only
for listings ingested since it shipped. Measured on production 2026-09-06: 737
yaencontre rows, **26 with any description** and 82 with a photograph, so 655
rows can be judged from neither a picture nor a sentence. The emails that
carried those pictures are still in the mailbox.

This creates nothing and fetches nothing from any portal. One read-only IMAP
pass over the yaencontre senders, `cards_in_email` on each body, and a locked
write of `enrichment["import"]["photos"]` on rows that already exist and carry
none.

Five things are deliberate.

**It does not touch the UID cursor.** That cursor is the ingester's, and
moving it would make the ingester skip mail it has not read -- the one way a
read-only tool could lose listings. Nothing here writes it, and the mailbox is
opened `readonly=True`.

**It never creates a row.** A card naming a listing this database does not hold
is counted and skipped: creating it would be ingestion, which belongs to
`services/ingest_policy.py` and to the machine that owns the mailbox, and a
row created here would arrive without the profile resolution, the dedup key
and the advertiser rules that `fotocasa_import.build_property` applies.

**It never overwrites a photograph.** A row that already carries one got it
from the ingester, from the same email, at the moment the listing arrived --
which is at least as good as what this pass would find and possibly better.

**The write is locked** (`services/enrichment_write.locked_write`, #339): this
writes `enrichment`, which is one JSON column, so every write is a
read-modify-write of the whole of it and an unlocked one loses whatever
another process committed in between.

**Announce it before running it on the mini**, and run `tools/
backfill_status.sh` first: `busy` and `unknown` are a stop, not an input to a
judgement (owner decision 2026-08-17).

    python -m utils.backfill_yaencontre_photos             # report the scope
    python -m utils.backfill_yaencontre_photos --apply     # write
"""

import argparse
import email
import logging
from typing import Any, Dict, List, Optional

from imapclient import IMAPClient
from sqlalchemy.orm.attributes import flag_modified

from app import create_app
from config import Config
from services import fotocasa_import, portal_photos, yaencontre_source
from services.property_imap_service import PropertyIMAPService
from services.enrichment_write import locked_write
from utils.inflight import inflight

logger = logging.getLogger(__name__)


def _bodies(limit: int) -> List[str]:
    """Every yaencontre alert body in the mailbox, oldest first, read-only.

    Its own connection and its own query rather than `PropertyIMAPService`:
    that class exists to ingest, it filters by the UID cursor and it advances
    it, and neither is wanted here. What is shared is the sender list, so a
    portal renamed in configuration is renamed for this tool too.
    """
    senders = [
        s.strip()
        for s in (getattr(Config, "YAENCONTRE_ALERT_SENDERS", "") or "").split(",")
        if s.strip()
    ]
    if not senders:
        raise SystemExit(
            "YAENCONTRE_ALERT_SENDERS is empty: this machine is configured not "
            "to read yaencontre mail, and there is nothing to back fill from."
        )

    host = Config.IMAP_HOST
    reader = PropertyIMAPService()
    bodies: List[str] = []
    with IMAPClient(
        host, port=Config.IMAP_PORT, ssl=Config.IMAP_SSL, timeout=60
    ) as client:
        client.login(Config.IMAP_USER, Config.IMAP_PASSWORD)
        if "gmail" in (host or "").lower():
            try:
                client.select_folder("[Gmail]/All Mail", readonly=True)
            except Exception:
                client.select_folder("INBOX", readonly=True)
            query = " OR ".join(f"from:{s}" for s in senders)
            uids = client.search(["X-GM-RAW", query])
        else:
            client.select_folder("INBOX", readonly=True)
            uids = client.search(["ALL"])
        uids = sorted(uids)
        if limit:
            uids = uids[:limit]
        logger.info("%d yaencontre message(s) to read", len(uids))
        if not uids:
            return []
        for uid, data in client.fetch(uids, ["RFC822"]).items():
            raw = data.get(b"RFC822")
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            # The ingester's own decoding, not a third copy of it: HTML first,
            # because the card markup is where the photograph is.
            body = "\n".join(reader._extract_html_parts(message)) or (
                reader._extract_text_parts(message)
            )
            if body:
                bodies.append(body)
    return bodies


def _capture_of(card: Any) -> Optional[Dict[str, Any]]:
    capture = getattr(card, "photos", None)
    if not isinstance(capture, dict):
        return None
    if not capture.get("items"):
        return None
    return capture


def fill_from_bodies(
    bodies: List[str], *, apply: bool = False, limit: int = 0
) -> Dict[str, int]:
    """Fill what the given alert bodies know, on rows that already exist.

    Separate from `main` so the rules below are exercised where they live: the
    caller owns the app context and the transaction boundary, and this takes no
    argv, opens no mailbox and creates no application.
    """
    seen: Dict[int, Dict[str, Any]] = {}
    for body in bodies:
        for card in yaencontre_source.cards_in_email(body):
            capture = _capture_of(card)
            if capture and card.listing_id not in seen:
                seen[card.listing_id] = capture

    counts = {"written": 0, "already": 0, "no_row": 0, "cards": len(seen)}
    for listing_id, capture in sorted(seen.items()):
        row = fotocasa_import.existing_by_listing_id(
            listing_id, yaencontre_source.SOURCE_NAME
        )
        if row is None:
            # Creating it would be ingestion: the profile resolution, the dedup
            # key and the advertiser rules all live in
            # `fotocasa_import.build_property`, and the mailbox belongs to the
            # machine `services/ingest_policy.py` names.
            counts["no_row"] += 1
            continue
        if portal_photos.read_photos(row)["state"] != "not_captured":
            # Either the ingester already captured one from this same email --
            # at least as good as this -- or the payload was read and named
            # none, which is a measurement this must not overwrite.
            counts["already"] += 1
            continue
        if not apply:
            counts["written"] += 1
            if limit and counts["written"] >= limit:
                break
            continue
        with locked_write(row, locked=True, commit=True):
            enrichment = dict(row.enrichment or {})
            block = dict(enrichment.get("import") or {})
            # Re-read under the lock: another process may have written one
            # between the scope pass and here (#339).
            if portal_photos.ENRICHMENT_KEY in block:
                counts["already"] += 1
                continue
            block[portal_photos.ENRICHMENT_KEY] = capture
            enrichment["import"] = block
            row.enrichment = enrichment
            flag_modified(row, "enrichment")
        counts["written"] += 1
        logger.info("property %s: %d photograph(s)", row.id, len(capture["items"]))
        if limit and counts["written"] >= limit:
            break
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill yaencontre photographs from the alert emails "
        "(free: IMAP only, no portal request, no Google).",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Default reports and exits."
    )
    parser.add_argument(
        "--max-emails", type=int, default=0, help="0 = every message found."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Stop after this many rows written."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = create_app()
    with app.app_context():
        # Finished rows leave the scope -- they gain the key this looks for --
        # so an interrupted run resumes by construction.
        with inflight("backfill_yaencontre_photos", resumable=True):
            bodies = _bodies(args.max_emails)
            logger.info("%d message(s) read", len(bodies))
            counts = fill_from_bodies(bodies, apply=args.apply, limit=args.limit)
            verb = "written" if args.apply else "would be written"
            logger.info(
                "done: %d %s, %d already answered for, %d card(s) name a "
                "listing this database does not hold (%d listing(s) carried a "
                "photograph)",
                counts["written"],
                verb,
                counts["already"],
                counts["no_row"],
                counts["cards"],
            )
            if not args.apply:
                logger.info("Dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
