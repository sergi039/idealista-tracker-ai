"""What the stored coordinates are good for, counted. Spends nothing.

Read-only by construction: it opens no external API, writes no column and takes
no lock. Google Geocoding is billed, so repairing what this finds is
`utils/refresh_property_accuracy.py` and needs the owner to ask for it in as
many words -- this only says how much there is to repair.

    python -m utils.report_coordinate_quality
    python -m utils.report_coordinate_quality --json
    python -m utils.report_coordinate_quality --clusters 10

Two questions, one command:

* how many rows carry a coordinate that is not the parcel, and how many of
  their sea distances and travel blocks are therefore unattributable;
* which coordinates are shared by more than one listing -- evidence about
  precision that the accuracy label alone does not carry, since 39 of the rows
  sharing a point are labelled `precise`.
"""

import argparse
import json
from collections import defaultdict
from typing import Any, Dict, List

from app import create_app
from models import Property
from services.coordinate_quality import is_precise, normalize_accuracy
from services.property_travel_service import (
    TRAVEL_STATE_APPROXIMATE_ORIGIN,
    effective_travel_state,
)
from services.sea_distance_service import (
    STATUS_APPROXIMATE_ORIGIN,
    parcel_measurement,
)


def collect(rows: List[Property], cluster_limit: int) -> Dict[str, Any]:
    by_accuracy: Dict[str, int] = defaultdict(int)
    points: Dict[Any, List[int]] = defaultdict(list)
    sea_unattributable = 0
    travel_unattributable = 0

    for prop in rows:
        by_accuracy[normalize_accuracy(prop.location_accuracy)] += 1
        # The stored value, exactly as written: two rows are on one point when
        # the database says they are, not when they round to it.
        points[(str(prop.location_lat), str(prop.location_lon))].append(prop.id)
        if parcel_measurement(prop).get("status") == STATUS_APPROXIMATE_ORIGIN:
            sea_unattributable += 1
        if effective_travel_state(
            prop
        ) == TRAVEL_STATE_APPROXIMATE_ORIGIN and isinstance(prop.travel, dict):
            travel_unattributable += 1

    shared = {point: ids for point, ids in points.items() if len(ids) > 1}
    shared_rows = sum(len(ids) for ids in shared.values())
    precise_yet_shared = sum(
        1
        for point, ids in shared.items()
        for prop in rows
        if prop.id in ids and is_precise(prop.location_accuracy)
    )

    clusters = sorted(shared.items(), key=lambda item: len(item[1]), reverse=True)
    return {
        "located_rows": len(rows),
        "by_accuracy": dict(sorted(by_accuracy.items())),
        "sea_distance_unattributable": sea_unattributable,
        "travel_blocks_unattributable": travel_unattributable,
        "shared_points": len(shared),
        "rows_sharing_a_point": shared_rows,
        "precise_yet_sharing_a_point": precise_yet_shared,
        "largest_clusters": [
            {"lat": lat, "lon": lon, "count": len(ids), "ids": ids}
            for (lat, lon), ids in clusters[:cluster_limit]
        ],
        # Said out loud rather than left to be inferred from the list length:
        # a truncated view that does not name its own truncation reads as the
        # whole picture.
        "clusters_not_shown": max(0, len(clusters) - cluster_limit),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--clusters", type=int, default=5, help="How many shared points to list."
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        rows = (
            Property.query.filter(
                Property.location_lat.isnot(None), Property.location_lon.isnot(None)
            )
            .order_by(Property.id)
            .all()
        )
        report = collect(rows, max(0, args.clusters))

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(f"Located rows: {report['located_rows']}")
    for label, count in report["by_accuracy"].items():
        print(f"  {label:<12} {count}")
    print(
        "Sea distances that cannot be attributed to the parcel: "
        f"{report['sea_distance_unattributable']}"
    )
    print(
        "Travel blocks measured from a locality centroid:        "
        f"{report['travel_blocks_unattributable']}"
    )
    print(
        f"Shared coordinates: {report['rows_sharing_a_point']} rows across "
        f"{report['shared_points']} points "
        f"({report['precise_yet_sharing_a_point']} of them labelled precise)"
    )
    for cluster in report["largest_clusters"]:
        ids = ", ".join(str(i) for i in cluster["ids"])
        print(f"  {cluster['lat']}, {cluster['lon']}  x{cluster['count']}  [{ids}]")
    if report["clusters_not_shown"]:
        print(f"  ... and {report['clusters_not_shown']} more (raise --clusters)")


if __name__ == "__main__":
    main()
