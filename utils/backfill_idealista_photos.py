"""Fill only existing Idealista rows from listing photos in retained emails.

Read-only IMAP, no UID cursor, no portal fetch, no paid API. Run the liveness
check first. Writes reuse the existing enrichment row lock, preserve captured
or measured-empty photos, and commit per row. Restart skips completed rows.
"""

import argparse
from collections import Counter
import email
import json

from imapclient import IMAPClient

from app import create_app
from config import Config
from models import Property
from services.enrichment_write import locked_write
from services.portal_photos import from_idealista_email, read_photos
from services.property_imap_service import PropertyIMAPService
from utils.inflight import inflight


def captures_from_mail():
    reader = PropertyIMAPService()
    captures = {}
    with IMAPClient(
        Config.IMAP_HOST, port=Config.IMAP_PORT, ssl=Config.IMAP_SSL, timeout=60
    ) as client:
        client.login(Config.IMAP_USER, Config.IMAP_PASSWORD)
        client.select_folder(
            "[Gmail]/All Mail" if "gmail" in Config.IMAP_HOST.lower() else "INBOX",
            readonly=True,
        )
        uids = sorted(client.search(["FROM", "noresponder@idealista.com"]))
        for start in range(0, len(uids), 50):
            for _uid, data in sorted(
                client.fetch(uids[start : start + 50], ["BODY.PEEK[]"]).items()
            ):
                message = email.message_from_bytes(data[b"BODY[]"])
                body = "\n".join(reader._extract_html_parts(message))
                captures.update(from_idealista_email(body))
    return captures


def fill(captures, *, apply=False):
    counts = Counter()
    for listing_id, photos in captures.items():
        # Every subscription copy, not only the first row with this portal id.
        rows = Property.query.filter_by(idealista_property_id=listing_id).all()
        for row in rows:
            if read_photos(row)["state"] != "not_captured":
                counts["already"] += 1
                continue
            if apply:
                with locked_write(row, locked=True, commit=True):
                    enrichment = dict(row.enrichment or {})
                    block = dict(enrichment.get("import") or {})
                    if "photos" in block:
                        counts["already"] += 1
                        continue
                    block["photos"] = photos
                    enrichment["import"] = block
                    row.enrichment = enrichment
            counts["filled" if apply else "would_fill"] += 1
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    app = create_app()
    with app.app_context(), inflight("backfill_idealista_photos", resumable=True):
        captures = captures_from_mail()
        print(
            json.dumps(
                {"mail_listings": len(captures), **fill(captures, apply=args.apply)}
            )
        )


if __name__ == "__main__":
    main()
