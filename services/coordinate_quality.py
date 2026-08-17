"""What a stored coordinate is good enough to answer.

`Property.location_accuracy` records what Google said about the point it
returned (#321): `precise` means it matched an address, anything else means it
matched a locality and handed back its centroid. Four listings on four
different streets of Santa María del Mar share one such centroid, and it sits
23.8 m from the coastline -- so every derived measurement taken from it
describes a point on the beach, including the one whose query was
`San Miguel de Quiloño s/n`, ten kilometres inland.

That is the fourth way a geocode goes wrong, and the three existing guards in
`services/property_location_service.py` all pass it: the result is not a coarse
`type` (#331), it is in the row's own province (#348), and its accuracy label
is one the whitelist accepts (#321). Right place, wrong precision.

`services/sea_view_service.py` already refuses to answer from such a point, and
already carries the one idea that makes an exception honest: an approximate
coordinate may still decide a question when the answer cannot change anywhere
inside the error. This module is that policy, in the one place every consumer
can import it from -- the slack, what counts as precise, and the bounds a
measurement really supports.

It deliberately does *not* re-geocode anything, and nothing here spends money:
repairing the rows is `utils/refresh_property_accuracy.py`, which needs the
owner to ask because Google Geocoding is billed.
"""

from typing import Any, List, Optional, Tuple

# The one accuracy label that means "this point is the property". Everything
# else -- `approximate`, `unknown`, an empty column -- means a centroid until
# proven otherwise, which is the reading `sea_view_service` has always used.
PRECISE = "precise"

# How far the real parcel may sit from an approximate coordinate. A locality
# centroid is kilometres from the edges of the locality it names; 5 km is the
# figure `sea_view_service` has carried since it started refusing to compute a
# view from one, and it lives here now so that sea distance, travel and the
# view verdict cannot drift into three different numbers.
APPROXIMATE_COORD_SLACK_M = 5_000


def normalize_accuracy(value: Any) -> str:
    """The accuracy label, lower-cased, with a missing one named `unknown`."""
    text = str(value).strip().lower() if value is not None else ""
    return text or "unknown"


def is_precise(value: Any) -> bool:
    """True only for a coordinate Google matched to an address."""
    return normalize_accuracy(value) == PRECISE


def coordinate_slack_m(value: Any) -> float:
    """Metres the real parcel may sit from a coordinate with this accuracy."""
    return 0.0 if is_precise(value) else float(APPROXIMATE_COORD_SLACK_M)


def improves_on(new_accuracy: Any, old_accuracy: Any) -> bool:
    """Is `new_accuracy` worth replacing `old_accuracy` with?

    Only `precise` improves on anything, because `precise` is the only label
    this module treats as different: it is the one that grants zero slack and
    unlocks a paid travel run. `approximate` replacing `approximate` buys
    nothing at all -- every consumer already reads both the same way -- so a
    swap between them is not an upgrade, it is a coin toss with the row's
    location.
    """
    if is_precise(new_accuracy):
        return not is_precise(old_accuracy)
    return False


def portal_coordinate(record: Any) -> Optional[Tuple[float, float, str]]:
    """The coordinate the source portal published for this listing, if any.

    Written by an importer into `enrichment["import"]["coordinate"]` and read
    here, because this is where "which of two coordinates should a row keep"
    already lives.

    It matters because the two coordinates a row can carry are not the same
    kind of thing. A portal pin is placed for *this advert*; a geocode is
    derived from whatever the title says, and
    `PropertyLocationService._build_geocoding_queries` reads the text after
    "in" -- which for a plot is usually a district or a village and not a
    street. Measured on property 733 (2026-08-17): re-geocoding
    "Land for sale in Llaranes, Avilés" answered with the Llaranes district
    centroid, 2447 m from fotocasa's own pin, still `approximate`, and the
    advert text places the plot in Valliniello rather than Llaranes at all --
    so the query named the wrong neighbourhood as well. See issue #393.

    Returns `(lat, lon, source)` or None. A block that does not parse is None
    rather than an exception: it is provenance, and a row with a malformed one
    should still geocode.
    """
    enrichment = getattr(record, "enrichment", None)
    if not isinstance(enrichment, dict):
        return None
    block = enrichment.get("import")
    if not isinstance(block, dict):
        return None
    coordinate = block.get("coordinate")
    if not isinstance(coordinate, dict):
        return None
    try:
        lat = float(coordinate["lat"])
        lon = float(coordinate["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    source = str(coordinate.get("source") or "portal")
    return lat, lon, source


def distance_bounds_m(
    measured_m: Optional[float], slack_m: float
) -> Tuple[Optional[float], Optional[float]]:
    """The range a measured distance really supports, given the slack.

    The parcel is somewhere within `slack_m` of the point that was measured, so
    a measured distance `d` to a fixed feature bounds the parcel's own distance
    to `[d - slack, d + slack]` -- never below zero, since a negative distance
    is not a place. With no slack both bounds are the measurement itself, which
    is what makes a precise row take the same code path as an approximate one
    instead of a second branch that can rot.
    """
    if measured_m is None:
        return None, None
    return max(0.0, measured_m - slack_m), measured_m + slack_m


def shared_coordinate_peers(prop: Any, limit: int = 25) -> List[int]:
    """Ids of other listings stored at exactly this coordinate.

    Computed rather than stored: it is a fact about the *set* of rows, so it
    changes whenever any other row is geocoded, and a column would need a
    writer on every geocode to stay true. 652 rows answer this in milliseconds.

    It is evidence, not a verdict, and nothing scores on it. Two flats in one
    building legitimately share a point, and the coordinate alone cannot tell
    that apart from four plots sharing a village centroid -- so this is shown
    to the reader, who can see the addresses, rather than used to refuse a
    measurement. What refuses a measurement is `location_accuracy`, and 39 of
    the 229 rows sharing a point are labelled `precise`.
    """
    lat = getattr(prop, "location_lat", None)
    lon = getattr(prop, "location_lon", None)
    prop_id = getattr(prop, "id", None)
    if lat is None or lon is None or prop_id is None:
        return []

    from models import Property

    rows = (
        Property.query.with_entities(Property.id)
        .filter(
            Property.location_lat == lat,
            Property.location_lon == lon,
            Property.id != prop_id,
        )
        .order_by(Property.id)
        .limit(limit)
        .all()
    )
    return [row[0] for row in rows]
