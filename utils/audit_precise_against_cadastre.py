"""What is a Google ROOFTOP worth on this coast? Ask the cadastre (#559).

`utils/audit_precise_accuracy.py` withdrew the `precise` labels the stored
record could refute. What it could not ask is what the *kept* labels are worth:
`services/coordinate_quality.py` grants `precise` zero slack, and the only
ground truth anybody had was one row (360) that was 2868 m out. This asks the
Spanish cadastre, which is free and keyless, for a second coordinate.

    python -m utils.audit_precise_against_cadastre                 # every kept row
    python -m utils.audit_precise_against_cadastre --limit 20      # a sample
    python -m utils.audit_precise_against_cadastre --ids 1537,1734
    python -m utils.audit_precise_against_cadastre --json out.json

**It writes nothing, and that is deliberate.** #559's own list of what must not
happen has two entries: do not widen `precise`'s slack globally on the strength
of one measurement, and do not relabel a row from a displacement alone. A tool
that could write would be a tool that could do the second by accident. The
output is a sample, and what a sample is for is a person deciding whether a
band belongs in `_tier_slack_table` -- with the sample written beside it, the
way `LISTING_PIN_SLACK_M` carries its seven rows.

**The cohort is the rows whose ROOFTOP agreed with the query.** That is
`services/address_agreement.py`'s own reading, applied here to the same two
stored strings, so this tool and the withdrawal tool cannot disagree about who
is in scope. A row whose label a person set is out of scope and is named: its
coordinate is not the geocoder's claim and measuring it would answer a
different question.

**One request per row**, on the shared `CATASTRO_GATE` at one second, with no
retries -- Catastro publishes no rate limit and does document an approximately
ten-day IP ban for abuse. A hundred and thirty rows is about two and a half
minutes of a single connection, which is why the loop carries a `utils.inflight`
marker: a deploy that kills it should say so (#283).

**Two passes, because one of them only reaches 25 m.** The first asks what
parcel lies under Google's point: `on_asked_parcel` bounds the error by the
size of one parcel, `displaced` is a metre measurement, and
`beyond_neighbours` or `number_matched_other_street` mean the row is not
placed -- the neighbour list reaches about 25 m, so such a row may be 30 m out
or 3 km. The second pass places those from the other direction, by looking the
asked address up in the cadastre's own street index and measuring to the
parcel's reference point. Measured on production it is what found the large
errors: row 25, which carries `precise` and 0 m of slack today, is 337 m from
the "calle Tarancon, 6" its query asked for.

A row neither pass can place stays counted as unplaced and never as correct,
and the summary keeps the three columns apart, because a band computed over a
column that mixes measurements with the rows nothing could measure would be a
band that fits the sample rather than the world.
"""

import argparse
import json
import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from services.address_agreement import (
    AGREED,
    answered_house_number,
    house_number_agreement,
    query_house_numbers,
)
from services.cadastre_locate import (
    BEYOND_NEIGHBOURS,
    PlaceIndex,
    match_street,
    parcel_for_address,
    streets,
    DISPLACED,
    NO_NUMBER_ASKED,
    NO_PARCEL,
    NUMBER_MATCHED_OTHER_STREET,
    ON_ASKED_PARCEL,
    compare_to_asked_number,
    parcels_at,
)
from services.cadastre_service import OK, fetch_parcel
from services.coordinate_quality import PRECISE, manual_coordinate
from services.sea_view_service import haversine_m

logger = logging.getLogger(__name__)

JOB_NAME = "audit_precise_against_cadastre"

# The findings that carry a number a band could be built from, and the one that
# carries only a floor. Kept as two names rather than one predicate so the
# summary and the caller read the same split.
BOUNDED_FINDINGS = (ON_ASKED_PARCEL, DISPLACED)

# The first-pass findings that leave the row unplaced, and so are worth the
# three extra requests of the second pass. `displaced` is already a
# measurement and `on_asked_parcel` is already an answer; neither is re-asked.
SECOND_PASS_FINDINGS = (BEYOND_NEIGHBOURS, NUMBER_MATCHED_OTHER_STREET)


def _geocoding(prop) -> Dict[str, Any]:
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return {}
    record = enrichment.get("geocoding")
    return record if isinstance(record, dict) else {}


def in_cohort(prop) -> Optional[str]:
    """`None` if the row belongs in the sample, else why it does not.

    A reason rather than a boolean because every exclusion is printed: a
    cohort nobody can see the edges of is a cohort whose summary means nothing.
    """
    if manual_coordinate(prop) is not None:
        return "a person set this location"
    if prop.location_lat is None or prop.location_lon is None:
        return "no coordinate"
    record = _geocoding(prop)
    if not record:
        return "no geocoding record"
    query = record.get("query")
    if not query_house_numbers(query):
        return "the query named no house number"
    state = house_number_agreement(query, answered_house_number(record))
    if state != AGREED:
        return f"address_check is {state}, not {AGREED}"
    return None


def asked_street(query: Any) -> Optional[str]:
    """The street a geocoding query named: its first comma-separated component.

    Passed to `compare_to_asked_number` only to raise a doubt, never to settle
    one, so the street-type word ("calle", "Lugar") is left on: keeping it can
    only add a word two names might share, and sharing a word is what makes the
    check stay quiet. The conservative direction is the one that keeps quiet.
    """
    head = str(query or "").split(",")[0].strip()
    return head or None


def measure(prop) -> Dict[str, Any]:
    """One row: what the cadastre says lies under the coordinate Google gave.

    The asked number comes from the query, not from the answer. They are equal
    for every row in the cohort -- that is what `agreed` means -- and taking it
    from the query keeps this reading anchored to what was asked for even if
    the cohort is ever widened.
    """
    record = _geocoding(prop)
    asked = sorted(query_house_numbers(record.get("query")))
    lat, lon = float(prop.location_lat), float(prop.location_lon)

    answer = parcels_at(lat, lon)
    row: Dict[str, Any] = {
        "id": prop.id,
        "municipality": prop.municipality,
        "asked": asked[0] if asked else None,
        "query": record.get("query"),
        "formatted_address": record.get("formatted_address"),
        "lat": lat,
        "lon": lon,
        "cadastre_status": answer.get("status"),
    }
    if answer.get("status") != OK:
        row["finding"] = None
        row["detail"] = answer.get("detail")
        return row

    parcels = answer["parcels"]
    row["parcels_returned"] = len(parcels)
    row["neighbour_reach_m"] = parcels[-1]["distance_m"] if parcels else None
    row["place_codes"] = [parcels[0]["province_code"], parcels[0]["municipality_code"]]
    row.update(
        compare_to_asked_number(
            parcels, row["asked"], asked_street(record.get("query"))
        )
    )
    return row


# What the second pass calls the outcome when it could not place the asked
# address at all. Each is an absence of measurement, never a distance.
RESOLVED = "resolved"


def resolve_asked_address(row: Dict[str, Any], index: PlaceIndex) -> Dict[str, Any]:
    """Where the cadastre puts the address that was asked, and how far that is.

    The first pass answers "what is under Google's point", which bounds the
    error only when the asked number is inside the neighbour list's reach --
    about 25 m. This is the other direction, for the rows it could not reach:
    the municipality's street index, then the parcel at that number, then that
    parcel's own reference point. Three requests, and the last of them is
    `services/cadastre_service.fetch_parcel`, reused rather than re-implemented
    so the reference point comes from the module that already checks the CRS.

    Every way of failing keeps its own name. `street_not_matched` in particular
    is not evidence about the listing: it says this municipality's index has no
    street spelled the way the query spelled it, which happens for a road code
    ("N 632") and for a lugar Google wrote as a street.
    """
    place = row.get("place")
    street = asked_street(row.get("query"))
    number = row.get("asked")
    if not place or not street or not number:
        return {"resolution": "no place or street to look up"}

    index_answer = streets(place["province"], place["municipality"])
    if index_answer.get("status") != OK:
        return {"resolution": f"street index: {index_answer.get('status')}"}

    matched = match_street(index_answer["streets"], street)
    if matched.get("status") != OK:
        return {"resolution": matched["status"], "detail": matched.get("detail")}

    found = matched["street"]
    parcel = parcel_for_address(
        place["province"], place["municipality"], found["sigla"], found["name"], number
    )
    if parcel.get("status") != OK:
        return {
            "resolution": f"parcel: {parcel.get('status')}",
            "detail": parcel.get("detail"),
        }

    block = fetch_parcel(parcel["reference"])
    point = block.get("reference_point")
    if not point:
        return {
            "resolution": "parcel has no reference point",
            "asked_reference": parcel["reference"],
        }

    return {
        "resolution": RESOLVED,
        "asked_reference": parcel["reference"],
        "asked_address": parcel.get("address"),
        "asked_lat": point["lat"],
        "asked_lon": point["lon"],
        "resolved_distance_m": haversine_m(
            row["lat"], row["lon"], point["lat"], point["lon"]
        ),
    }


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The distribution, with what was measured kept apart from what was not.

    Three columns and never one: the rows whose coordinate stands on the parcel
    that carries the number asked; the rows whose displacement from it was
    measured, by either pass; and the rows nothing here could place. Averaging
    across the three would answer with the shape of the sample rather than the
    shape of the error.
    """
    findings = Counter(
        row.get("finding") or f"cadastre:{row['cadastre_status']}" for row in rows
    )
    near = [row["distance_m"] for row in rows if row.get("finding") == DISPLACED]
    resolved = [
        row["resolved_distance_m"]
        for row in rows
        if row.get("resolved_distance_m") is not None
    ]
    on_parcel = findings.get(ON_ASKED_PARCEL, 0)
    measured = sorted(near + resolved)
    unresolved = len(rows) - on_parcel - len(measured)
    return {
        "rows": len(rows),
        "findings": dict(findings),
        "resolutions": dict(
            Counter(row["resolution"] for row in rows if row.get("resolution"))
        ),
        "on_asked_parcel": on_parcel,
        "measured_displacements_m": measured,
        "measured_max_m": max(measured) if measured else None,
        "measured_over_25m": sum(1 for value in measured if value > 25),
        "unresolved_rows": unresolved,
    }


def _report(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    for row in rows:
        finding = row.get("finding")
        if finding == ON_ASKED_PARCEL:
            detail = f"on the parcel numbered {row['asked']} ({row.get('reference')})"
        elif finding == DISPLACED:
            detail = (
                f"{row['distance_m']:.2f} m from the parcel numbered "
                f"{row['asked']}; landed on {row.get('landed_on')!r}"
            )
        elif finding == NUMBER_MATCHED_OTHER_STREET:
            detail = (
                f"number {row['asked']} found, but on {row.get('cadastral_street')!r} "
                f"and the query asked {row.get('asked_street')!r} -- cannot tell"
            )
        elif finding == BEYOND_NEIGHBOURS:
            detail = (
                f"at least {row['at_least_m']:.2f} m away -- the parcel numbered "
                f"{row['asked']} is not among the {row.get('parcels_returned')} "
                f"returned; landed on {row.get('landed_on')!r}"
            )
        elif finding == NO_PARCEL or row.get("cadastre_status") != OK:
            detail = f"{row['cadastre_status']}: {row.get('detail') or 'no parcel'}"
        elif finding == NO_NUMBER_ASKED:
            detail = "the query named no house number"
        else:
            detail = str(finding)
        logger.info(
            "  %-6s %-24s %s", row["id"], (row["municipality"] or "")[:24], detail
        )

    logger.info("")
    logger.info("%d row(s) in the cohort", summary["rows"])
    for finding, count in sorted(summary["findings"].items(), key=lambda kv: -kv[1]):
        logger.info("  under the point: %-30s %d", finding, count)
    for state, count in sorted(summary["resolutions"].items(), key=lambda kv: -kv[1]):
        logger.info("  asked address:   %-30s %d", state, count)

    measured = summary["measured_displacements_m"]
    logger.info("")
    logger.info(
        "  %d on the parcel that carries the number asked", summary["on_asked_parcel"]
    )
    logger.info(
        "  %d displacement(s) measured, max %.1f m, %d of them over 25 m",
        len(measured),
        summary["measured_max_m"] or 0.0,
        summary["measured_over_25m"],
    )
    logger.info("  %d row(s) neither confirmed nor placed", summary["unresolved_rows"])
    logger.info("")
    logger.info(
        "A row nothing could place is not a row that is right. This is a sample, "
        "not a band: `_tier_slack_table` changes only when a person decides it does."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure a kept `precise` against the cadastre (#559). Reports only."
    )
    parser.add_argument(
        "--ids", help="Comma-separated property ids instead of the whole cohort."
    )
    parser.add_argument(
        "--limit", type=int, help="Stop after this many rows of the cohort."
    )
    parser.add_argument("--json", help="Write the rows and the summary here.")
    args = parser.parse_args(argv)

    from flask import current_app, has_app_context

    if has_app_context() and current_app.extensions.get("sqlalchemy") is _db():
        return _run(args)

    from app import create_app

    with create_app().app_context():
        return _run(args)


def _db():
    from app import db

    return db


def _run(args) -> int:
    from app import db
    from models import Property
    from utils.inflight import inflight
    from utils.refresh_property_accuracy import _parse_ids

    query = db.session.query(Property).filter(Property.location_accuracy == PRECISE)
    if args.ids:
        query = query.filter(Property.id.in_(_parse_ids(args.ids)))
    candidates = query.order_by(Property.id).all()

    cohort, skipped = [], []
    for prop in candidates:
        reason = in_cohort(prop)
        if reason:
            skipped.append((prop.id, reason))
        else:
            cohort.append(prop)

    if args.limit is not None:
        cohort = cohort[: args.limit]

    logger.info(
        "%d row(s) carry `precise`; %d in the cohort, %d skipped",
        len(candidates),
        len(cohort),
        len(skipped),
    )
    for pid, reason in skipped:
        logger.info("  skipped %-6s %s", pid, reason)
    if not cohort:
        return 0

    rows: List[Dict[str, Any]] = []
    index = PlaceIndex()
    # One request per row for the first pass, up to five more for a row the
    # second pass has to place by name, all on the one-second gate. A marker so
    # a deploy that kills the run says so. Not resumable -- it writes nothing,
    # so a re-run is the resume (#283).
    with inflight(JOB_NAME, resumable=False):
        for prop in cohort:
            row = measure(prop)
            codes = row.get("place_codes") or [None, None]
            row["place"] = (
                index.place_of(
                    {"province_code": codes[0], "municipality_code": codes[1]}
                )
                if codes[0]
                else None
            )
            if row.get("finding") in SECOND_PASS_FINDINGS:
                row.update(resolve_asked_address(row, index))
            rows.append(row)

    summary = summarise(rows)
    _report(rows, summary)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {"summary": summary, "rows": rows}, handle, indent=1, ensure_ascii=False
            )
        logger.info("Wrote %s", args.json)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
