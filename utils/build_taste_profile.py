"""Build (or rebuild) the owner's taste profile from their review comments.

    python -m utils.build_taste_profile            # report what would be built
    python -m utils.build_taste_profile --apply    # one bridge call, one insert

Free of Google by construction — the only transport is the subscription
bridge (issue #498; the owner's standing cost order of 2026-08-30). One run
is ONE bridge call. The default is a report, because the repository's rule
for tools that write data the app cannot recompute is report-and-exit
(`utils/restore_score_snapshot.py`); `--apply` is the explicit step.

A rebuild inserts a new `taste_profile` row (the ledger is insert-only, the
version is the primary key) and every previously scored listing becomes
*stale by version* — visible on the surfaces, and re-scored by
`python -m utils.backfill_taste --stale`. A failed build inserts nothing.
"""

import argparse
import json
import logging

from app import create_app
from services import taste_service

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distill the owner's taste profile from review comments "
        "(one subscription-bridge call; no Google).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call the bridge and insert the profile. Default reports only.",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "openai"],
        default="claude",
        help="Bridge provider (default claude — the repository's primary).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    app = create_app()
    with app.app_context():
        signals = taste_service.collect_signals()
        usable = [s for s in signals if s["usable"]]
        skipped = [s for s in signals if not s["usable"]]
        current = taste_service.load_current_profile()

        print(f"signals: {len(usable)} usable ({[s['property_id'] for s in usable]})")
        if skipped:
            print(
                f"skipped (verdict without a reason): "
                f"{[s['property_id'] for s in skipped]}"
            )
        if current:
            print(
                f"current profile: v{current['version']} built {current['built_at']} "
                f"from {current['source'].get('signals') and len(current['source']['signals'])} signals"
            )
            if current["signals_fingerprint"] == taste_service.signals_fingerprint(
                signals
            ):
                print(
                    "NOTE: the signal basis is unchanged since the current build — "
                    "a rebuild would re-ask the same question."
                )
        else:
            print("current profile: none")

        if not args.apply:
            print("\nDry run — nothing built. Re-run with --apply.")
            return

        outcome = taste_service.build_profile(provider=args.provider)
        if outcome.get("status") != "ok":
            print(f"\nBUILD FAILED: {outcome.get('error')}")
            raise SystemExit(1)
        data = outcome["data"]
        print(f"\nbuilt taste profile v{data['version']} (model {data['model']})")
        print(json.dumps(data["profile"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
