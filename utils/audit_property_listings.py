"""Paced, read-only listing audit, followed by an explicit snapshot-backed apply.

Run on the mini, after tools/backfill_status.sh. No billed enrichment, ingestion,
UID cursor changes or deletion. Refusals never become listing observations.
The JSONL report records the exact baseline; apply re-reads under the existing
row-lock writer and refuses stale/conflicting results. Keep report and snapshot
in data/ (untracked). A second apply cannot replay over its own checked stamp.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import random
import time
from urllib.parse import urlsplit, urlunsplit

from app import create_app, db
from models import Property
from services.enrichment_write import locked_write
from services.listing_status_service import ListingStatusService
from services.portal_photos import read_photos
from utils.inflight import inflight

HOSTS = {
    "www.idealista.com",
    "www.fotocasa.es",
    "www.milanuncios.com",
    "www.yaencontre.com",
    "idealista.com",
    "fotocasa.es",
    "milanuncios.com",
    "yaencontre.com",
}


def listing_url(url):
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def baseline(row):
    return {
        "url": listing_url(row.url),
        "status": row.listing_status,
        "source": row.listing_status_source,
        "checked": row.listing_last_checked.isoformat()
        if row.listing_last_checked
        else None,
        "removed": row.listing_removed_date.isoformat()
        if row.listing_removed_date
        else None,
    }


def audit(report):
    service = ListingStatusService()
    rows = Property.query.order_by(Property.id).all()
    counts = Counter()
    seen = {}
    for row in rows:
        before = baseline(row)
        url = before["url"]
        if urlsplit(url).hostname not in HOSTS:
            counts["unsupported"] += 1
            continue
        if url not in seen:
            backing_off = service.breakers.for_url(url).should_skip()
            observation = service.observe(url)
            seen[url] = (
                observation,
                service.captured_photos,
                datetime.now(timezone.utc).isoformat(),
            )
            if not backing_off:
                time.sleep(random.uniform(2, 4))
        observation, photos, checked = seen[url]
        entry = {
            "id": row.id,
            "baseline": before,
            "observed_at": checked,
            "status": observation.status,
            "refusal": observation.refusal,
            "removed_date": observation.removed_date,
            "photos": photos,
        }
        report.write(json.dumps(entry) + "\n")
        report.flush()
        counts[observation.status] += 1
        if row.id % 25 == 0:
            print(json.dumps({"through_id": row.id, **counts}), flush=True)
    print(
        json.dumps({"total": len(rows), "unique_urls": len(seen), **counts}), flush=True
    )


def apply_entry(entry):
    """Apply only a recent answer to the exact row version that was audited."""
    if entry["status"] not in ("active", "removed", "sold") or entry.get("refusal"):
        return "unresolved"
    observed = datetime.fromisoformat(entry["observed_at"])
    age = datetime.now(timezone.utc) - observed
    if age < timedelta(0) or age > timedelta(days=1):
        return "expired"
    row = db.session.get(Property, entry["id"])
    if row is None:
        return "missing"
    with locked_write(row, locked=True, commit=True):
        if baseline(row) != entry["baseline"] or row.listing_status_source == "manual":
            return "conflict"
        ListingStatusService()._apply_observed_status(
            row, entry["status"], entry.get("removed_date")
        )
        row.listing_last_checked = observed
        photos = entry.get("photos")
        if photos and read_photos(row)["state"] == "not_captured":
            enrichment = dict(row.enrichment or {})
            block = dict(enrichment.get("import") or {})
            if "photos" not in block:
                block["photos"] = photos
                enrichment["import"] = block
                row.enrichment = enrichment
    return "applied"


def private_output(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply an existing report; makes no network requests",
    )
    args = parser.parse_args()
    app = create_app()
    with app.app_context(), inflight("audit_property_listings", resumable=False):
        if not args.apply:
            with private_output(args.report) as report:
                audit(report)
        else:
            counts = Counter()
            # Durable before-images exist before the first DB write, including
            # photos, and are only the fields this tool can change.
            entries = [
                json.loads(line) for line in args.report.read_text().splitlines()
            ]
            with private_output(str(args.report) + ".before.jsonl") as snapshot:
                for entry in entries:
                    row = db.session.get(Property, entry["id"])
                    if row is not None:
                        block = (row.enrichment or {}).get("import") or {}
                        snapshot.write(
                            json.dumps(
                                {
                                    "id": row.id,
                                    "baseline": baseline(row),
                                    "had_photos": "photos" in block,
                                    "photos": block.get("photos"),
                                }
                            )
                            + "\n"
                        )
                snapshot.flush()
                os.fsync(snapshot.fileno())
            for entry in entries:
                counts[apply_entry(entry)] += 1
            print(json.dumps(counts))


if __name__ == "__main__":
    main()
