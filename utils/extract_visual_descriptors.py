"""Bounded, explicit visual descriptor extraction.  Never runs automatically."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


PROMPT = """Describe only visible real-estate appearance evidence in the attached photos.
Return the requested JSON. A photo cannot establish parcel geometry, dimensions,
boundaries, legal facts, or what is absent outside the frame. Keep those unknown
with a concrete limitation. Every observation must cite exactly one supplied image.

Use only these canonical values for a non-unknown observation:
- visual_appeal: traditional_stone, painted_facade, weathered_facade, modern_finish, plain_finish
- house_character: old_farmhouse, stone_house, rural_house, modern_house, mixed_style
- house_condition: well_maintained, weathered, damp, major_renovation, ruined
- neighbor_privacy: neighbours_visible, screened_view
- nearby_buildings: few_visible, several_visible, dense_visible
- agricultural_context: agricultural_structures_visible, cultivated_land_visible
- room_scale: small, medium, large
plot_outline must always be unknown with value null. Unknown observations must
also use value null; do not infer privacy, absent neighbours, boundaries, or
anything outside the visible frame."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", type=int, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="persist validated results"
    )
    parser.add_argument("--max-rows", type=int, default=3)
    parser.add_argument("--max-images", type=int, default=3)
    parser.add_argument("--max-calls", type=int, default=3)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.max_rows < 1
        or args.max_images < 1
        or args.max_calls < 1
        or len(args.ids) > args.max_rows
    ):
        raise SystemExit(
            "ids and all caps must be positive; ids may not exceed --max-rows"
        )

    from app import create_app, db
    from models import Property
    from services import taste_descriptors, visual_input

    app = create_app()
    calls = 0
    with app.app_context():
        rows = (
            Property.query.filter(Property.id.in_(args.ids)).order_by(Property.id).all()
        )
        found = {row.id for row in rows}
        missing = sorted(set(args.ids) - found)
        if missing:
            raise SystemExit(f"property ids not found: {missing}")
        for prop in rows:
            images = visual_input.download_portal_photo_inputs(
                prop, max_images=min(args.max_images, visual_input.MAX_IMAGES)
            )
            if not images:
                images = visual_input.download_dossier_photo_inputs(
                    prop, max_images=min(args.max_images, visual_input.MAX_IMAGES)
                )
            if not images:
                print(
                    f"{prop.id}: skipped (no eligible photo input from captured "
                    "portal metadata or the stored dossier)"
                )
                continue
            envelope = visual_input.build_visual_input(images)
            property_fingerprint = taste_descriptors.input_fingerprint(prop)
            print(
                f"{prop.id}: {len(images)} image(s), input={envelope['input_fingerprint'][:12]}, "
                f"property={property_fingerprint[:12]}"
            )
            if not args.apply:
                continue
            if calls >= args.max_calls:
                print(f"{prop.id}: not called (max calls reached)")
                continue
            calls += 1
            extracted = visual_input.extract_visual_observations(PROMPT, images)

            # No database lock is held over either the download or model call.
            # Lock only for the final freshness check and small JSON write.
            current = (
                Property.query.filter_by(id=prop.id)
                .populate_existing()
                .with_for_update()
                .one()
            )
            if taste_descriptors.input_fingerprint(current) != property_fingerprint:
                db.session.rollback()
                print(
                    f"{prop.id}: discarded (property inputs changed during extraction)"
                )
                continue
            if extracted["input_fingerprint"] != envelope["input_fingerprint"]:
                db.session.rollback()
                print(f"{prop.id}: discarded (image inputs changed during extraction)")
                continue
            taste = dict(current.taste or {})
            persisted = dict(extracted)
            persisted["property_fingerprint"] = property_fingerprint
            taste["visual_descriptor"] = persisted
            current.taste = taste
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(current, "taste")
            db.session.commit()
            print(
                f"{prop.id}: stored {len(extracted['visual_observations'])} observation(s)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(run())
