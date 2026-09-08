"""Record the location a person established for one listing, and defend it.

The geocoder is not the only thing that can locate a listing, and for a plot it
is frequently the worst of them: `_build_geocoding_queries` reads the text after
"in", which for a plot is a village or a district, so a re-geocode answers with a
centroid however many hours somebody spent in the cadastre establishing the
parcel. `services/property_location_service.ensure_coordinates` now refuses to
geocode a row whose location a person set -- and this is the way to set one.

**It exists because the alternative is what actually happened.** Measured on
production 2026-08-20, the only writers of `Property.location_accuracy` in this
tree are the geocoder, the fotocasa import, the `Land` migration and the restore
half of `utils/refresh_property_accuracy.py`. Every other coordinate a person
established was written by an ad-hoc script through `docker exec`, and three
rows carry the result in three different shapes -- 161 and 792 under
`enrichment["coordinate_provenance"]` with `method` values that do not match and
timestamps under two different names, 774 under `enrichment["cadastre"]`. Two of
the three carry a `precise` their own `enrichment["geocoding"]` record
contradicts, which is the fingerprint of a write made outside the geocoder.
Nothing in the repository read any of them, and nothing defended them.

This tool does not touch those three rows and neither does anything else in this
change. Nothing in the database distinguishes a `precise` a person curated from
a `precise` Google returned -- 130 of the 132 `precise` rows on production carry
no portal pin (measured 15:02Z; that count moves with every ingest, so re-measure
rather than quoting it) -- so converting them by inference is the STATUS-002
mistake in a new column.

**Two of those three rows have since been "defended" the wrong way, and that is
the clearest argument for this tool.** Between the morning measurement and
15:02Z, hand-run scripts wrote their cadastre conclusions into
`enrichment["import"]["coordinate"]` -- 161 as `source: cadastre_manual`, 792 as
`cadastre_parcel` -- which is the field `services/coordinate_quality.py`
documents as "the coordinate the source portal published for this listing". A
cadastral parcel centroid is not a portal's pin. It works, because
`_apply_geocode_outcome` defends that field, and it is exactly the STATUS-002
mistake the paragraph above refuses: an inference stored under a name that means
something else, where the next reader has no way to tell the two apart. Those
rows wanted moving to `enrichment["location"]` with `--source cadastre`, by the
person who established them, with the note their own block already held -- and
that is how they went (#536): 161 on 2026-08-25 and 792 on 2026-09-03, one run
of this tool each, after which the misfiled entries were taken off both rows
under a snapshot (`data/issue_536_pre_fix.json` on the mini). This tool does
not remove such an entry; it names it (`MISFILED`, in `_describe`), and it does
so on a row that already carries a hand-set block as much as on one that does
not, because on 161 the block hid the entry from this window for nine days and
only a SQL query could see it.

    python -m utils.set_property_location --id 792 \\
        --lat 43.537720 --lon -5.552047 --accuracy precise --source cadastre \\
        --note "cadastre_by_area_match: RC 52076A00400209 (Pol. 4 Parc. 209
                EL MILAN, Villaviciosa), 1344 m2 against 1345 m2 listed;
                the point is the parcel"

That is the point 792 carries. An earlier version of this example quoted the
barrio-centre pin the row's own provenance had already superseded, 420 m from
the parcel -- an example is a command somebody will paste, so it quotes the
current point or none.

`--id` on its own prints the row -- what it is located at, what the geocode
last said, whether a person set it, and whether that block still agrees with
the columns -- and exits. That is the only window onto a hand-set location that
has drifted from its own provenance, so it costs no arguments to look. A
*partial* set of arguments is still an error naming what is missing: a
forgotten `--note` must not quietly become "looked and did nothing".

With the four arguments and no `--apply` it prints what it would do and exits.
`--apply` writes. `--clear` takes the block
off and puts the row back on the computed path, leaving the coordinate columns
alone -- see `clear_location_by_hand` for why it does not restore what the block
displaced.
"""

import argparse
import logging

logger = logging.getLogger(__name__)


# The portals whose own pin ends up in `enrichment["import"]["coordinate"]`:
# `services/fotocasa_import.py` writes the site's name, `portal_coordinate`
# answers "portal" for an entry with no source, and the hand-run map-pin
# scripts wrote the portal's name with a suffix. The field is free text and
# has drifted across nine spellings on production (measured 2026-09-07:
# `fotocasa` 145, `idealista_map` 44, `milanuncios` 26, `fotocasa_pin` 8,
# `idealista map pin` 3, `idealista_pin` 3, and one each of `pisos_pin`,
# `fotocasa payload` and `idealista`), every one of them naming the portal
# first. So a pin is the portal's own when its source *starts with* a portal's
# name. An exact set of spellings was tried first and held four of the nine,
# which called the other 83 real pins MISFILED -- a false alarm in the one line
# meant to catch the real one, `cadastre_parcel` (#536).
PORTAL_NAMES = (
    "fotocasa",
    "idealista",
    "milanuncios",
    "yaencontre",
    "pisos",
    "portal",
)


def is_portal_pin_source(source) -> bool:
    """Whether a pin's `source` names a portal, whatever its spelling."""
    token = str(source or "").strip().lower()
    return any(token.startswith(name) for name in PORTAL_NAMES)


def _describe(prop) -> str:
    from services.coordinate_quality import (
        improves_on,
        manual_coordinate,
        normalize_accuracy,
        portal_coordinate,
    )

    hand = manual_coordinate(prop)
    pin = portal_coordinate(prop)
    lines = [
        f"  id            {prop.id}",
        f"  title         {(prop.title or '')[:70]}",
        f"  coordinate    {prop.location_lat}, {prop.location_lon}",
        f"  accuracy      {prop.location_accuracy or 'unknown'}",
    ]
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    record = enrichment.get("geocoding")
    if isinstance(record, dict):
        lines.append(
            f"  geocode said  {record.get('accuracy')!r} for {record.get('query')!r}"
        )
    if hand is None:
        # "No hand-set block" is not the same fact as "a refresh will overwrite
        # this row", and printing the second on the strength of the first is a
        # claim about a guard nothing here consulted. Measured on production
        # 2026-08-20, it was false for exactly the two rows this tool exists
        # for: 161 and 792 carried a portal pin (they no longer do, #536), and
        # `_apply_geocode_outcome` refuses to trade a `precise` for anything a
        # geocode can answer, so a refresh left them alone. So say which guard
        # applies, or that none does.
        if pin is None:
            lines.append("  hand-set      no -- nothing defends this row")
            lines.append(
                "  EXPOSED       a refresh would write whatever the geocoder "
                "answers over this coordinate and its accuracy"
            )
        else:
            _, _, pin_source = pin
            defended = not improves_on("precise", prop.location_accuracy)
            lines.append(
                f"  hand-set      no -- but a {pin_source!r} pin is on the row"
            )
            if defended:
                lines.append(
                    "  defended by   enrichment.import.coordinate (#393): no "
                    "geocode improves on this accuracy, so a refresh keeps it"
                )
            else:
                lines.append(
                    "  EXPOSED       the pin is kept only against a geocode that "
                    "is no better; a `precise` answer would replace it"
                )
    else:
        lines.append(
            f"  hand-set      {hand.source} at {hand.set_at}: {hand.note[:60]}"
        )
        # The block is provenance and the columns are the value, and the design
        # permits them to disagree: nothing inside the app can move the columns
        # of a hand-set row any more, but an out-of-band script still can, and
        # that boundary is the one `services/ingest_policy.py` records as
        # uncloseable. The score reads the *columns*, so a disagreement means
        # the row is being measured from a point its own provenance does not
        # describe. Say so here, because this is the only window onto it.
        moved = (
            prop.location_lat is None
            or prop.location_lon is None
            or abs(float(prop.location_lat) - hand.lat) > 1e-7
            or abs(float(prop.location_lon) - hand.lon) > 1e-7
        )
        if moved:
            lines.append(
                f"  DISAGREES     the block says {hand.lat}, {hand.lon} -- "
                "something moved the columns since. The score reads the columns."
            )
        if normalize_accuracy(prop.location_accuracy) != hand.accuracy:
            lines.append(
                f"  DISAGREES     the block says accuracy {hand.accuracy!r}, "
                f"the column says {normalize_accuracy(prop.location_accuracy)!r}"
            )
    if pin is not None and not is_portal_pin_source(pin[2]):
        # The field means "the coordinate the source portal published". A value
        # from anywhere else works -- which is exactly why it is worth saying
        # out loud rather than leaving to be discovered by whoever next reads
        # the row as a portal's own claim. Said whether or not a hand-set block
        # is on the row: this check used to live under `hand is None`, and on
        # production the entry on row 161 disappeared from this window the day
        # its block landed and stayed visible to nothing but a SQL query for
        # nine days (#536).
        if hand is None:
            remedy = (
                "Move it here with "
                f"--lat {prop.location_lat} --lon {prop.location_lon} "
                f"--accuracy {normalize_accuracy(prop.location_accuracy)} "
                "--source <where it came from> --note '<what was checked>'"
            )
        else:
            remedy = (
                "The hand-set block above already carries this row's "
                "provenance, so the entry adds nothing and keeps a claim the "
                "portal never made; this tool leaves it -- null it under a "
                "snapshot, the way #536 did."
            )
        lines.append(
            f"  MISFILED      {pin[2]!r} is not a portal. That field means what "
            "the source site published; a conclusion stored there is "
            f"indistinguishable from one. {remedy}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="Property id")
    parser.add_argument("--lat", help="Latitude a person established")
    parser.add_argument("--lon", help="Longitude a person established")
    parser.add_argument(
        "--accuracy",
        help="precise | approximate | unknown -- what the finding really supports",
    )
    parser.add_argument(
        "--note", help="What was checked. Required: see the module docstring"
    )
    parser.add_argument(
        "--source",
        default="manual",
        help="Who established it (default: manual). Name a tool if one did.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove the hand-set block; the coordinate columns are left alone",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Without it, report only."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    setting = [
        name for name in ("lat", "lon", "accuracy", "note") if getattr(args, name)
    ]
    # Three intents, told apart by how much was given rather than by a flag.
    #
    # **Nothing** means "show me this row" -- the mode this tool shipped without
    # and was described as having. `_describe` is the only window onto a hand-set
    # block whose columns have drifted from it, and requiring the caller to
    # invent the coordinate they are not setting in order to look at the one
    # that is there made that window unreachable.
    #
    # **Some but not all** stays an error naming what is missing. Folding it
    # into the looking mode would turn a forgotten `--note` into "looked and did
    # nothing" -- a refusal wearing the clothes of a completed action, which is
    # the defect this repository files under #98.
    inspect_only = not args.clear and not setting
    if inspect_only and args.apply:
        parser.error(
            "--apply needs something to apply: give --lat/--lon/--accuracy/--note, "
            "or --clear. Without them this prints the row and exits."
        )
    if not args.clear and setting:
        missing = [
            name
            for name in ("lat", "lon", "accuracy", "note")
            if not getattr(args, name)
        ]
        if missing:
            parser.error(
                "--" + ", --".join(missing) + " required unless --clear is given"
            )

    from app import create_app
    from models import Property, db
    from services.property_location_service import (
        clear_location_by_hand,
        set_location_by_hand,
    )

    app = create_app()
    with app.app_context():
        prop = db.session.get(Property, args.id)
        if prop is None:
            logger.error("No such property: %s", args.id)
            return 1

        if inspect_only:
            logger.info("%s", _describe(prop))
            return 0

        logger.info("Before:\n%s", _describe(prop))

        if not args.apply:
            if args.clear:
                logger.info("\nWould clear the hand-set block. Re-run with --apply.")
            else:
                logger.info(
                    "\nWould set %s, %s as %s (%s).\nRe-run with --apply.",
                    args.lat,
                    args.lon,
                    args.accuracy,
                    args.source,
                )
            return 0

        if args.clear:
            outcome = clear_location_by_hand(prop, commit=True)
            if not outcome["cleared"]:
                logger.info("\nNothing to clear: this row carries no hand-set block.")
                return 0
            logger.info("\nCleared. It held: %s", outcome["previous"])
        else:
            outcome = set_location_by_hand(
                prop,
                lat=args.lat,
                lon=args.lon,
                accuracy=args.accuracy,
                note=args.note,
                source=args.source,
                commit=True,
            )
            if outcome["displaced"]:
                logger.info("\nDisplaced: %s", outcome["displaced"])

        db.session.refresh(prop)
        logger.info("After:\n%s", _describe(prop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
