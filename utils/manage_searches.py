"""Preview owner search operations; apply only an exact reviewed snapshot.

No physical delete, paid call, or external portal change. A private, exclusive
receipt is required before a write. Original source rows and property IDs are
retained; the existing route_profile service owns the routing transaction.
"""

import argparse
import json
import os
from pathlib import Path

from flask import current_app, has_app_context

from app import create_app, db
from models import Property
from services import search_management


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=int, action="append", required=True)
    parser.add_argument("--target", type=int)
    parser.add_argument(
        "--action", choices=["merge", "remove", "promote", "technical"], default="merge"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected", help="Fingerprint printed by the preview")
    parser.add_argument(
        "--snapshot", type=Path, help="New private receipt, outside web-served paths"
    )
    args = parser.parse_args(argv)
    if args.apply and (not args.expected or not args.snapshot):
        parser.error("--apply requires --expected and --snapshot")
    if has_app_context() and current_app.extensions.get("sqlalchemy") is db:
        return _run(args)
    with create_app().app_context():
        return _run(args)


def _run(args):
    report = search_management.preview(args.source, args.target, args.action)
    summary = {
        key: report[key]
        for key in (
            "action",
            "fingerprint",
            "properties",
            "favorites",
            "reviews",
            "conflicts",
        )
    }
    summary["sources"] = [{"id": p.id, "name": p.name} for p in report["sources"]]
    summary["target"] = report["target"].id if report["target"] else None
    if args.apply:
        if report["fingerprint"] != args.expected:
            raise ValueError("stale_preview")
        # A second guarded preview inside apply refuses concurrent changes.
        # Do not overwrite a receipt from a prior attempt, successful or not.
        before = {
            "preview": summary,
            "profiles": [
                {c.name: getattr(p, c.name) for c in p.__table__.columns}
                for p in report["sources"]
            ],
            "properties": [
                {c.name: getattr(p, c.name) for c in p.__table__.columns}
                for p in Property.query.filter(
                    Property.search_profile_id.in_(args.source)
                ).order_by(Property.id)
            ],
        }
        if args.snapshot.resolve().is_relative_to(
            Path(current_app.static_folder).resolve()
        ):
            raise ValueError("snapshot must be outside web-served paths")
        fd = os.open(args.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(before, handle, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        # Settings conflicts must be acknowledged in the web preview. The CLI
        # never silently discards them to make a batch run convenient.
        search_management.apply(args.source, args.target, args.action, args.expected)
        summary["applied"] = True
    else:
        summary["applied"] = False
    print(json.dumps(summary, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
