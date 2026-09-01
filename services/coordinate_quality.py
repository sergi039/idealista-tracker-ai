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

import math

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from services.population import Population

# The one accuracy label that means "this point is the property". Everything
# else -- `approximate`, `unknown`, an empty column -- means a centroid until
# proven otherwise, which is the reading `sea_view_service` has always used.
PRECISE = "precise"

# Every label this column is allowed to carry. `services/property_location_service.py`
# narrowed a geocoder's answer to exactly these three and kept the set inline;
# it lives here because a hand-set accuracy has to be validated against the
# same three, and two copies of a whitelist is one copy that eventually
# accepts a label the rest of the app reads as `unknown`.
KNOWN_ACCURACIES = (PRECISE, "approximate", "unknown")

# The block a person's own finding is written to, and the `source` that says a
# person wrote it. It is deliberately **not** `enrichment["import"]["coordinate"]`
# -- that key means "the pin the source portal published", and putting a
# conclusion drawn from the cadastre there would pass an inference off as a
# portal's published fact, which is the mistake
# `utils/repair_import_status_source.py` exists to undo (STATUS-002 in #265).
MANUAL_LOCATION_KEY = "location"
SOURCE_MANUAL = "manual"

# How far the real parcel may sit from an approximate coordinate. A locality
# centroid is kilometres from the edges of the locality it names; 5 km is the
# figure `sea_view_service` has carried since it started refusing to compute a
# view from one, and it lives here now so that sea distance, travel and the
# view verdict cannot drift into three different numbers.
APPROXIMATE_COORD_SLACK_M = 5_000

# ---------------------------------------------------------------- tiers -----
#
# `approximate` means two different things and the scorer has to assume the
# worse of them (#493). A locality centroid is shared by every listing in the
# village -- 21 of them on the worst point on production -- while a pin a
# portal or a person placed for *this advert* is not shared by anything.
# Measured 2026-09-01 over the 1226 located rows: 51.9% of the 879 geocoded
# rows sit on a point another listing also occupies, against **0.0% of the 183
# rows carrying a pin**. Those are two populations, and one slack for both
# takes the worse.
#
# What the middle tier is worth was measured against the strongest ground
# truth available here: eight rows carry BOTH a location a person established
# from the cadastre and a portal or map pin, and the distance between the two
# is the pin's own error --
#
#     68, 102, 107, 122, 124, 174, 195, 1150 metres
#
# -- median 123, seven of eight at or under 195, **observed maximum 1150**.
# The method is validated on one of those rows by a person rather than by
# arithmetic: property 421 carries, in its own import block, the note
# "EXACT per portal, but the pin is a meadow 170 m S of the house", and that
# row's cadastre-versus-pin distance computes to 174 m. Agreement to 4 m. It
# is deliberately not counted as a ninth sample -- it is the same property,
# and one measurement taken twice is not two observations.
#
# That row also settles which way a portal's own exactness claim points: the
# one declared-exact pin anybody has checked was wrong by 170 m. A portal
# saying `is_exact` is evidence for this tier and against `precise`.
#
# **n is 8.** Eight observations do not bound an error, and an independent
# review said so; 2000 m is the observed maximum with a margin, not a proven
# ceiling, and it is written here with its sample so the next person can widen
# it rather than inherit a number with no provenance. Re-derive it as the
# sample grows -- the query is every row carrying both an
# `enrichment["location"]` and an `enrichment["import"]["coordinate"]`.
#
# What the tier does NOT do is publish a point estimate from a band. Every
# consumer still refuses unless the answer is the same at both ends of the
# slack, so an underestimate here narrows a *disclosed band* rather than
# printing a score nobody measured. That is the whole reason the constant is
# allowed to rest on eight rows.
LISTING_PIN_SLACK_M = 2_000

# Metres, not decimal places: a pin is stored as a decimal string with between
# 4 and 12 places (measured across the nine writers) while the columns are
# `Numeric(10, 7)`, so `"40.123456789012"` and a stored `40.1234568` are the
# same pin and have to compare equal. One metre is far below the smallest
# error this tier is about and far above the rounding.
PIN_MATCH_EPSILON_M = 1.0

TIER_ADDRESS = "address"
TIER_LISTING_PIN = "listing_pin"
TIER_LOCALITY = "locality"


#: Built per call rather than held as a module-level dict. The constants above
#: are the single home of this policy, and a table frozen at import time stops
#: honouring them the moment one is patched or reassigned -- which is exactly
#: what `tests/test_hazard_proximity.py::test_the_coordinate_policy_is_the_shared_one`
#: exists to catch, and did.
def _tier_slack_table() -> dict:
    return {
        TIER_ADDRESS: 0.0,
        TIER_LISTING_PIN: float(LISTING_PIN_SLACK_M),
        TIER_LOCALITY: float(APPROXIMATE_COORD_SLACK_M),
    }


def normalize_accuracy(value: Any) -> str:
    """The accuracy label, lower-cased, with a missing one named `unknown`."""
    text = str(value).strip().lower() if value is not None else ""
    return text or "unknown"


def is_precise(value: Any) -> bool:
    """True only for a coordinate Google matched to an address."""
    return normalize_accuracy(value) == PRECISE


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metres between two coordinates, on a sphere.

    Local to this module and deliberately not imported from a service: the
    tier is consulted by four of them, and importing a distance helper from one
    would make `coordinate_quality` -- which every consumer imports -- depend on
    a consumer.
    """
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _pin_is_the_stored_coordinate(record: Any, pin: Tuple[float, float]) -> bool:
    """Does the row's own coordinate still stand where the pin was placed?

    A pin block says where a portal or a person put this advert. It does not
    say where the row *is*: direct SQL through `docker exec` is a supported
    workflow here, `utils/set_property_location.py` writes one column pair and
    a different block, and `_apply_geocode_outcome` defends a portal pin only
    on the path that runs through it. A block that no longer agrees with the
    columns describes a coordinate the row has stopped carrying, so it earns
    the row nothing.
    """
    lat = getattr(record, "location_lat", None)
    lon = getattr(record, "location_lon", None)
    if lat is None or lon is None:
        return False
    try:
        return (
            _haversine_m(float(lat), float(lon), pin[0], pin[1]) <= PIN_MATCH_EPSILON_M
        )
    except (TypeError, ValueError):
        return False


def coordinate_tier(record: Any) -> str:
    """Which of the three kinds of coordinate this row carries.

    Provenance, not accuracy: the tier says who placed the point and for what,
    and `_TIER_SLACK_M` says what that is worth. The order is decisive --
    `precise` first, because a hand-set location that a person labelled
    `precise` has already been written to the column and must not be demoted to
    the pin tier by carrying its own provenance block.

    **The `source` string is deliberately not read.** It has already drifted
    across nine spellings on production -- `fotocasa`, `fotocasa_pin`,
    `fotocasa payload`, `idealista`, `idealista_map`, `idealista map pin`,
    `idealista_pin`, `milanuncios`, `pisos_pin` -- so a table keyed on it would
    give the wide slack to `fotocasa_pin` and the narrow one to `fotocasa`: a
    partial rule that reads as complete, which is the shape of defect this
    repository keeps removing. What the tier asserts is only "this point was
    placed for this advert", and that is true of all nine.

    What it therefore cannot tell is a pin from a portal that publishes a
    locality centroid as its pin. The measurement above says that is not what
    the table holds today (0 of 183 pin rows share a point), and the honest
    limit is that this is evidence rather than proof -- which is why nothing
    downstream may publish a point estimate out of the resulting band.
    """
    if isinstance(record, str):
        # This function used to take the accuracy label, and a label still
        # answers every `getattr` below with `None` -- so a caller that was not
        # migrated would quietly receive the locality slack for every row,
        # including precise ones. Silent and safe-looking is exactly the
        # failure `SeaDistanceService.measure` refused a default for; raise.
        raise TypeError(
            "coordinate_tier takes the row, not its accuracy label; "
            "pass the Property (see issue #493)"
        )

    if is_precise(getattr(record, "location_accuracy", None)):
        return TIER_ADDRESS

    hand_set = manual_coordinate(record)
    if hand_set is not None and _pin_is_the_stored_coordinate(
        record, (hand_set.lat, hand_set.lon)
    ):
        return TIER_LISTING_PIN

    portal = portal_coordinate(record)
    if portal is not None and _pin_is_the_stored_coordinate(
        record, (portal[0], portal[1])
    ):
        return TIER_LISTING_PIN

    return TIER_LOCALITY


def slack_for_tier(tier: str) -> float:
    """Metres the real parcel may sit from a coordinate of this tier.

    Split from `coordinate_slack_m` for the callers that do not have the row in
    their hands -- `SeaDistanceService.measure` takes a bare `lat, lon` and the
    sea-view geometry takes a coordinate and an accuracy label. Those receive
    the tier from the caller that *does* have the row, rather than each
    re-deriving a rule from the accuracy string, which is how
    `sea_view_service` came to carry its own copy of the old one.
    """
    return _tier_slack_table().get(tier, float(APPROXIMATE_COORD_SLACK_M))


def coordinate_slack_m(record: Any) -> float:
    """Metres the real parcel may sit from this row's coordinate.

    Takes the **row**, not its accuracy label, and takes it as a required
    argument. An optional record falling back to the label would return the
    locality slack for any caller that forgot to pass one -- safe for the data
    and wrong for the report, which `SeaDistanceService.measure`'s own
    docstring already refuses in the same words: a required argument cannot be
    forgotten quietly.
    """
    return slack_for_tier(coordinate_tier(record))


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


def record_portal_coordinate(
    enrichment: Optional[dict], *, source: str, lat: Any, lon: Any
) -> dict:
    """Write the pin `portal_coordinate` reads back, and return the block.

    The writer lives beside the reader for the reason `read_verdict` and
    `state_expression` live beside each other in
    `services/listing_verification.py`: two places that have to agree about a
    shape will eventually disagree about it. Two callers already exist -- the
    importer, at insert, and the backfill that establishes the pin for rows
    imported before this field did.

    `lat`/`lon` are stored as strings. `Property.enrichment` is a JSON column,
    and a float round-trips through it with whatever precision the encoder
    feels like; the coordinate columns are `Numeric(10, 7)`, so the decimal
    text is what actually matches them. None for either is a block of None
    rather than a `(0, 0)` pin, which is a real place in the Gulf of Guinea.
    """
    block = dict(enrichment or {})
    imported = dict(block.get("import") or {})
    if lat is None or lon is None:
        imported["coordinate"] = None
    else:
        imported["coordinate"] = {
            "source": source,
            "lat": str(lat),
            "lon": str(lon),
        }
    block["import"] = imported
    return block


@dataclass(frozen=True)
class HandSetLocation:
    """A location a person established, and what they were looking at."""

    lat: float
    lon: float
    accuracy: str
    note: str
    source: str
    set_at: Optional[str] = None


def manual_coordinate(record: Any) -> Optional[HandSetLocation]:
    """The location a person established for this listing, if any.

    Read from `enrichment["location"]`, whose one writer is
    `record_manual_coordinate`. It exists because the geocoder is not the only
    thing that can locate a listing and is frequently the worst of them:
    `_build_geocoding_queries` reads the text after "in", which for a plot is a
    village or a district, so a re-geocode answers with a centroid however many
    hours somebody spent in the cadastre establishing the parcel.

    Measured on production, 2026-08-20. Three rows carry a location a person
    established, in three different shapes, and **nothing in this repository
    read any of them**: 161 (`coordinate_provenance.method =
    cadastre_by_address`, recording the coordinate 3.45 km away that it
    replaced), 792 (`cadastre_barrio_verified`, which raised the accuracy to
    `precise` over a verified barrio without moving the point) and 774 (a
    `cadastre` block from the Catastro WFS). Two of those three carry a
    `precise` their own `enrichment["geocoding"]` record contradicts, which is
    the fingerprint of a write made outside the geocoder.

    Returns None for a block that does not parse, for the reason
    `portal_coordinate` does: this is provenance, and a row with a malformed
    one should still be geocodable rather than raising in the middle of an
    enrichment run.
    """
    enrichment = getattr(record, "enrichment", None)
    if not isinstance(enrichment, dict):
        return None
    block = enrichment.get(MANUAL_LOCATION_KEY)
    if not isinstance(block, dict):
        return None
    try:
        lat = float(block["lat"])
        lon = float(block["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    accuracy = normalize_accuracy(block.get("accuracy"))
    if accuracy not in KNOWN_ACCURACIES:
        return None
    note = str(block.get("note") or "").strip()
    if not note:
        # A hand-set location with no reason is provenance that says nothing.
        # `record_manual_coordinate` refuses to write one, so a block without a
        # note did not come from that writer and is not read as one.
        return None
    set_at = block.get("set_at")
    return HandSetLocation(
        lat=lat,
        lon=lon,
        accuracy=accuracy,
        note=note,
        source=str(block.get("source") or SOURCE_MANUAL),
        set_at=str(set_at) if set_at else None,
    )


def is_hand_set(record: Any) -> bool:
    """Did a person establish this row's location?

    The question `services/property_location_service.ensure_coordinates` asks
    before it geocodes, exactly as `services/advertiser.enrich` asks
    `is_hand_set` before it fetches a page.
    """
    return manual_coordinate(record) is not None


def validate_hand_set(
    *, lat: Any, lon: Any, accuracy: Any, note: Any
) -> Tuple[float, float, str, str]:
    """Check a hand-set location, and raise rather than store half of one.

    Separate from `record_manual_coordinate` so a caller can run it **before it
    locks the row**. That is `services/enrichment_write.py`'s own rule applied
    one argument further out: it validates the caller ahead of the measurement
    so an impossible write costs a raise instead of a billed round of lookups,
    and an argument that cannot be stored should likewise cost a raise instead
    of a row lock and a rollback.

    Returns the parsed `(lat, lon, accuracy, note)`, so the writer parses once.
    """
    text = str(note or "").strip()
    if not text:
        raise ValueError("a hand-set location needs a note saying what was checked")

    # An empty accuracy is a missing argument, not a person saying `unknown`.
    # `normalize_accuracy` maps silence to `unknown` because that is the honest
    # *reading* of a column nobody filled; a writer being told what a finding
    # supports has to have it spelled, and `unknown` is a word one can type.
    if accuracy is None or not str(accuracy).strip():
        raise ValueError("a hand-set location needs an accuracy, `unknown` included")
    label = normalize_accuracy(accuracy)
    if label not in KNOWN_ACCURACIES:
        raise ValueError(f"not an accuracy this column carries: {accuracy!r}")

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        raise ValueError(f"not a coordinate: {lat!r}, {lon!r}")
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        raise ValueError(f"coordinate out of range: {lat_f}, {lon_f}")

    return lat_f, lon_f, label, text


def record_manual_coordinate(
    enrichment: Optional[dict],
    *,
    lat: Any,
    lon: Any,
    accuracy: Any,
    note: str,
    source: str = SOURCE_MANUAL,
    displaced: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Write the block `manual_coordinate` reads back, and return it.

    The writer lives beside the reader for the reason
    `record_portal_coordinate` does, and validates for a reason that one does
    not have to: this block **stops a re-geocode**, so a malformed one would
    silently pin a row to a coordinate nothing can correct.

    `note` is required and must say something. The advertiser's hand-set
    verdict needs no note because `owner` and `agency` describe themselves; a
    coordinate is two numbers, and "why here" is the entire content of the
    record. The three blocks already on production are all notes -- a parcel
    reference, a barrio, a sight line -- written because whoever wrote them
    knew that.

    `displaced` records what the row said before, so clearing this block is a
    decision somebody can act on rather than a value that is simply gone. It
    is a *record* and not an automatic undo: see `set_location_by_hand`.

    `lat`/`lon` are stored as strings, per `record_portal_coordinate` -- the
    coordinate columns are `Numeric(10, 7)` and a float round-trips through a
    JSON column with whatever precision the encoder feels like.
    """
    _lat, _lon, label, text = validate_hand_set(
        lat=lat, lon=lon, accuracy=accuracy, note=note
    )
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    block = dict(enrichment or {})
    entry = {
        "source": str(source or SOURCE_MANUAL),
        "lat": str(lat),
        "lon": str(lon),
        "accuracy": label,
        "note": text,
        "set_at": stamp,
    }
    if displaced:
        entry["displaced"] = displaced
    block[MANUAL_LOCATION_KEY] = entry
    return block


def clear_manual_coordinate(enrichment: Optional[dict]) -> dict:
    """Take the hand-set block off, putting the row back on the computed path."""
    block = dict(enrichment or {})
    block.pop(MANUAL_LOCATION_KEY, None)
    return block


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


def shared_coordinate_peers(prop: Any, limit: int = 25) -> Tuple[List[int], Population]:
    """Ids of other listings stored at exactly this coordinate, and how many
    there really are.

    Computed rather than stored: it is a fact about the *set* of rows, so it
    changes whenever any other row is geocoded, and a column would need a
    writer on every geocode to stay true. 652 rows answer this in milliseconds.

    It is evidence, not a verdict, and nothing scores on it. Two flats in one
    building legitimately share a point, and the coordinate alone cannot tell
    that apart from four plots sharing a village centroid -- so this is shown
    to the reader, who can see the addresses, rather than used to refuse a
    measurement. What refuses a measurement is `location_accuracy`, and 39 of
    the 229 rows sharing a point are labelled `precise`.

    The population is the **whole table** and stays that way (decision #410):
    exact-coordinate multiplicity is a fact about every row, so filtering
    hidden or retired subscriptions out of it would present an incomplete
    measurement as a complete one, and hidden is not confidentiality.

    What it did not do until UNIVERSE-001 is say how many it was showing. The
    list is capped at `limit`, and a capped list that does not name its own
    cap reads as the whole cluster -- `utils/report_coordinate_quality.py`
    wrote that rule down for the same clusters. The largest group on
    production is 21 against a cap of 25 (2026-08-19), so nothing is hidden
    today; the count is taken anyway, because "nothing is hidden today" is a
    measurement and not a design. It costs one `COUNT(*)` on an indexed
    equality, and only for a row that has a coordinate at all.
    """
    lat = getattr(prop, "location_lat", None)
    lon = getattr(prop, "location_lon", None)
    prop_id = getattr(prop, "id", None)
    empty = Population(
        label="shared_coordinate_class",
        total=None,
        returned=None,
        cap=limit,
        basis="exact equality on the stored coordinate",
        notes=("every subscription, hidden and retired included",),
    )
    if lat is None or lon is None or prop_id is None:
        # No coordinate is not an empty cluster: nobody looked, and a `total`
        # of 0 here would be the #98 defect in a disclosure.
        return [], empty

    from models import Property

    same_point = Property.query.filter(
        Property.location_lat == lat,
        Property.location_lon == lon,
        Property.id != prop_id,
    )
    total = same_point.count()
    rows = (
        same_point.with_entities(Property.id).order_by(Property.id).limit(limit).all()
    )
    ids = [row[0] for row in rows]
    return ids, Population(
        label="shared_coordinate_class",
        total=total,
        returned=len(ids),
        cap=limit,
        basis="exact equality on the stored coordinate",
        notes=("every subscription, hidden and retired included",),
    )
