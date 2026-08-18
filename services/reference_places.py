"""Places answered from a local register instead of a billed Google search.

The owner's invoice for 1-18 August 2026 was **EUR 190** on a project that
ingests about seven listings a day, and the whole of it is enrichment: seven
Places calls and ~26 Distance Matrix elements per listing, with travel runs
lining up day by day against the invoice spikes. The Places half is 63% of
that, and the *hospital* preset is the piece this repository already owns a
better answer for.

`data/hospitals_cnh.json` is the Ministry of Health's own Catálogo Nacional de
Hospitales, imported by `utils/import_cnh_hospitals.py`: 42 hospitals across
the five watched provinces (Asturias 14, A Coruña 10, Pontevedra 8, Lugo 5,
Ourense 5), every one with a coordinate, beds, teaching status and high-tech
equipment counts. Reading it costs nothing and no request leaves the machine.

It is also **more accurate than the search it replaces**, which is why this is
a step forward rather than a cost compromise. Google's `hospital` type is not
the word as a house-hunter means it: measured across 188 geocoded listings the
nearest one was named "hospital" for only 12, and Google indexes a hospital
campus room by room, so thirteen departments of San Agustín sorted ahead of
San Agustín itself (#323, #325). Every rule this repository grew to survive
that -- the name patterns, the ward and day-unit refusals, the wide Text
Search fallback for a town that crowds the real hospital off page one -- is
answering a question the register answers directly. A building either is in
the national hospital register or it is not.

**Nothing here is a fallback to a paid search.** A listing outside the
register's coverage gets an honest refusal, not a Google call: the point of
the change is that this preset stops spending, and a fallback would put the
spending back exactly where the register is thinnest. The refusals travel as
failures rather than as "nothing nearby", because a register that does not
cover Alicante has not established that Alicante has no hospitals (#98).

The reader is `QualityOfLifeService.hospitals()` and not a second copy of it:
that method already owns the file's path, its cache, the grouping filter and
the 150 km coverage limit, and two readers of one register is how they come to
disagree about what it says.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

# What a preset puts in `reference_source` to be answered from a local file.
REFERENCE_CNH_HOSPITALS = "cnh_hospitals"

# Why a register produced no place. These are refusals, never "nothing here".
REASON_NO_REFERENCE_DATA = "reference_data_missing"
REASON_OUTSIDE_COVERAGE = "outside_reference_coverage"
REASON_NO_COORDINATES = "no_coordinates"

_STATUS_TO_REASON = {
    "no_reference_data": REASON_NO_REFERENCE_DATA,
    "outside_reference_coverage": REASON_OUTSIDE_COVERAGE,
    "no_coordinates": REASON_NO_COORDINATES,
}


@dataclass
class ReferenceLookup:
    """A place from a register, or the reason there is none.

    Deliberately the same shape as a Places lookup so the caller does not grow
    a second branch: `place` is a Google-shaped dict, `reason` is set when
    there is no place and is always a *refusal* rather than an empty answer.
    """

    place: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


def nearest_reference_place(
    source: str, lat: float, lon: float
) -> Optional[ReferenceLookup]:
    """The nearest place this register knows, or None if it owns no such source.

    `None` means "this preset is not answered locally" -- the caller then does
    what it always did. It is distinct from a `ReferenceLookup` carrying a
    reason, which means "the register was asked and could not answer", and the
    two must not be collapsed: the first falls through to Google, the second
    must not, or a coverage hole silently becomes a bill.
    """
    if source != REFERENCE_CNH_HOSPITALS:
        return None

    from services.quality_of_life_service import QualityOfLifeService

    verdict = QualityOfLifeService().hospitals(lat, lon)
    status = verdict.get("status")
    if status != "ok":
        return ReferenceLookup(
            reason=_STATUS_TO_REASON.get(str(status), REASON_NO_REFERENCE_DATA)
        )

    nearest = verdict.get("nearest") or {}
    entries = [entry for entry in nearest.values() if isinstance(entry, dict)]
    entries = [
        entry
        for entry in entries
        if entry.get("lat") is not None and entry.get("lon") is not None
    ]
    if not entries:
        return ReferenceLookup(reason=REASON_NO_REFERENCE_DATA)

    # `hospitals()` answers with the nearest of each grouping; the nearest
    # hospital is the nearest of those. The grouping rides along rather than
    # deciding: all three are hospitals in the register's own terms, and a
    # page that wants to say "teaching hospital" can read it.
    best = min(entries, key=lambda entry: entry.get("distance_km", float("inf")))
    return ReferenceLookup(
        place={
            # No `place_id`: this is not a Google place, and inventing one
            # would send the maps link to a listing that does not exist. The
            # link builder falls back to the coordinate, which is right.
            "name": best.get("name"),
            "lat": best.get("lat"),
            "lon": best.get("lon"),
            "source": REFERENCE_CNH_HOSPITALS,
            "municipality": best.get("municipality"),
            "beds": best.get("beds"),
            "teaching": best.get("teaching"),
            "straight_line_km": best.get("distance_km"),
        }
    )
