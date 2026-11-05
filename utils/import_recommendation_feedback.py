"""Import a completed owner appeal review, with a scoped rollback receipt.

Dry-run by default; no network or model calls. A successful import is followed
by a deterministic profile refresh; interrupted refreshes can be retried alone.
"""

import argparse
import json
from pathlib import Path

from app import create_app
from services import recommendation_feedback, taste_service
from utils.review_json import valid_timestamp


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("input exceeds the 2 MiB review limit")
    return json.loads(raw)


def completed_feedback(checkpoint):
    """Incomplete or QA forms must never be silently treated as owner ratings."""
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("ok") is not True
        or not checkpoint.get("completed_at")
        or not valid_timestamp(checkpoint["completed_at"])
        or not isinstance(checkpoint.get("feedback"), dict)
    ):
        raise ValueError("a completed owner checkpoint is required")
    feedback = checkpoint["feedback"]
    if not feedback.get("answers") or any(
        answer.get("answer") not in ("yes", "no") for answer in feedback["answers"]
    ):
        raise ValueError("every card must be answered before importing")
    return feedback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--answers")
    parser.add_argument(
        "--snapshot", help="New rollback receipt path (required for apply)"
    )
    parser.add_argument("--restore", help="Restore one receipt using compare-and-swap")
    parser.add_argument("--refresh-profile", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.restore:
        if args.manifest or args.answers or args.snapshot:
            parser.error("restore cannot be combined with import")
    elif args.manifest or args.answers:
        if not (args.manifest and args.answers and args.profile and args.profile > 0):
            parser.error(
                "import requires paired --manifest/--answers and positive --profile"
            )
        if args.apply and not args.snapshot:
            parser.error("apply requires --snapshot")
    elif not args.refresh_profile:
        parser.error("provide a completed import, --restore or --refresh-profile")
    app = create_app()
    with app.app_context():
        if args.restore:
            result = recommendation_feedback.restore_batch(
                read_json(args.restore), apply=args.apply
            )
            print(json.dumps(result))
        elif args.answers:
            feedback = completed_feedback(read_json(args.answers))
            result = recommendation_feedback.import_batch(
                read_json(args.manifest),
                feedback,
                profile_id=args.profile,
                apply=args.apply,
                snapshot_path=args.snapshot,
            )
            print(json.dumps(result))
        if args.refresh_profile and args.apply:
            result = taste_service.refresh_feedback_snapshot()
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "changed": result["changed"],
                        "profile_version": result["data"]["version"],
                    }
                )
            )
        if not args.apply:
            print("Dry run; no writes, model calls or network lookups.")


if __name__ == "__main__":
    main()
