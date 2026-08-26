"""Create listings from the owner's own research spreadsheet.

    python -m utils.import_research_sheet sheet.csv --profile "Manus AI"
    python -m utils.import_research_sheet sheet.csv --profile "Manus AI" --apply

**Why this is not the link importer.** `services/fotocasa_import.py` reads a
listing page and stores what the portal published. That works for exactly one
site: idealista answers DataDome to every request from this machine, and the
rest of the portals in this sheet -- yaencontre, milanuncios, pisos.com, an
agency's own site -- nothing here can read. Measured on the sheet the owner
brought on 2026-08-17: 50 listings across five sites, **none of them fotocasa**.

So the data comes from the sheet, which the owner curated by hand, and the row
says so. The provenance records `source: "research_sheet"`, not a portal, and
carries no `coordinate` block -- there is no portal pin behind these numbers,
and inventing one would be the #393 defect with the evidence fabricated instead
of merely missing.

**What the sheet cannot give, this does not invent.** No coordinate, so the row
is created unlocated and geocodes on the first Enrich like any other; the
queries these titles produce ("San Martín de Podes, Gozón") resolve to a
locality, so the coordinate will read `approximate` and travel will refuse
itself. That is the honest outcome, not a shortfall to paper over.

**The research notes are labelled as such.** `description` normally holds the
advert's own text, and the AI analysis reads it. These are the owner's
observations about a listing -- planning status, utilities, risks -- so they go
in prefixed with what they are, rather than passing as words the seller wrote.

Deduplication asks `utils/listing_search.py`, the module that already answers
"does a row for this link exist", for the whole reading rather than half of it
-- `listing_id_clause`, which matches the Idealista listing id against
`idealista_property_id` **and** against the `/inmueble/<id>/` form in the URL.
Measured against production the same day, of the 50 rows 15 were already in the
table -- every one of them an Idealista listing that had arrived by alert
email, and only 2 of which an exact URL comparison would have found, because
the stored links carry `?utm_...` and the sheet's do not.

**These rows are created with `idealista_property_id` NULL, and that is not
what closes the hole.** Writing the id here was considered and refused on
2026-08-25, for two reasons worth stating rather than rediscovering. It fixes
nothing that needs fixing: 48 rows written before that date already carry NULL
(every NULL-id idealista row on production is one of ours), so `_existing` has
to read the URL regardless, and a dedup that only recognised rows written after
its own fix would be the defect it was written to remove. And it is not a dedup
change at all -- `services/property_imap_service._find_properties` matches that
column, per subscription and, for price-change and "no longer listed" emails,
across all of them. Populating it would make an alert email for the same
listing *update* a research row instead of creating its own (`if existing:
continue`), so the advert's title, description and attributes would never be
recorded for that listing in that subscription and the owner's research notes
would stand in for them. That may well be the better behaviour; it is a
decision about ingestion, with its own tests, and not a side effect of a
lookup.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import null
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

SOURCE_NAME = "research_sheet"

# The columns this reads. A sheet that does not carry them is refused rather
# than half-read: a silently empty price is a listing with no price, and this
# tool cannot tell that from a column somebody renamed.
REQUIRED_COLUMNS = ("Location", "Municipality", "Price €", "Plot m²", "Direct URL")

# Everything else the sheet knows, kept verbatim in the provenance block. These
# have no column in `properties` and inventing one for a fifty-row import would
# be a schema change nobody asked for.
RESEARCH_COLUMNS = (
    "Rank",
    "Priority",
    "Type",
    "Planning Status",
    "Buildable m²",
    "Utilities",
    "Key Positives",
    "Key Risks / Notes",
    "Source",
    "Ref / ID",
    "Updated",
    "Seller Type",
)

STATUS_NEW = "new"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"


def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _number(value: Any) -> Optional[float]:
    """A figure the sheet actually carries, or None -- never a guess.

    Deliberately narrow. "45.000" means forty-five thousand to a Spanish
    spreadsheet and forty-five to a decimal parser, and nothing in the string
    says which; a tool that picks one is choosing a price for the owner. So
    only unambiguous forms are read -- plain digits, or digits with one or two
    decimal places -- and anything else is None, which `read_rows` turns into a
    refusal naming the value rather than a row with a quietly wrong price.

    Measured on the sheet this was written for: every one of the 50 prices and
    plot sizes is plain digits, so the narrowness costs nothing there and only
    bites on a sheet formatted differently -- which is exactly when somebody
    should look.

    Zero is None for the reason `services/fotocasa_source.py` gives: a zero
    here is a blank somebody typed, not a plot of no size.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"[\s\u00a0€$]", "", raw)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        return None
    number = float(cleaned)
    return number if number > 0 else None


def _unreadable_number(value: Any) -> bool:
    """The sheet said something here, and it could not be read."""
    return bool(str(value or "").strip()) and _number(value) is None


def _area_type(type_text: Optional[str]) -> str:
    lowered = (type_text or "").lower()
    if "land" in lowered or "plot" in lowered or "terreno" in lowered:
        return "plot"
    if lowered:
        return "built"
    return "unknown"


def _research(row: Dict[str, str]) -> Dict[str, Any]:
    return {
        column: _text(row.get(column))
        for column in RESEARCH_COLUMNS
        if _text(row.get(column))
    }


def _description(research: Dict[str, Any], sheet: str) -> Optional[str]:
    """The owner's notes, saying that is what they are.

    Prefixed because `description` is where the advert's own words go and the
    AI analysis reads it as such. A note reading "Confirm suelo class via PGOU"
    is the owner talking to himself, not the seller making a claim, and a
    valuation that cannot tell them apart is worse than one with no text.
    """
    parts = []
    for label, key in (
        ("Planning", "Planning Status"),
        ("Buildable", "Buildable m²"),
        ("Utilities", "Utilities"),
        ("Positives", "Key Positives"),
        ("Risks", "Key Risks / Notes"),
        ("Seller", "Seller Type"),
    ):
        value = research.get(key)
        if value:
            parts.append(f"{label}: {value}")
    if not parts:
        return None
    return f"Research notes from {sheet} — not the advert text. " + " · ".join(parts)


def _existing(row: Dict[str, Any]):
    """The row already holding this listing, or None.

    Two identities, and deliberately not a third. `source_email_id` recognises
    a row this importer wrote before *from the same sheet line*, so re-running
    one sheet unchanged is a no-op. An Idealista listing id recognises the
    listing however it got here -- measured against production, 14 of the
    sheet's 50 were already here by alert email, and only 2 of them would have
    been found by comparing URLs exactly, because the stored links carry a
    `?utm_...` tail the sheet's copy does not.

    The id is asked of `utils.listing_search.listing_id_clause`, which knows
    the two places a row can carry it. Asking only the column was this
    importer's own blind spot: it never writes `idealista_property_id`, so its
    rows carry the id in the URL alone and it could not see them. Reproduced
    on production 2026-08-24 -- property 925 held
    `.../inmueble/81187720/` in subscription 22, and a dry run over a sheet
    containing that very link reported 20 new rows. Nothing here can delete a
    property, so each of those would have been permanent: on `/properties`, in
    the `/municipalities` medians and in the subscription's comparable pool.

    A changed line is a new row, and that is the honest answer rather than a
    shortcoming. `_stable_key` mixes the price and the plot size into the key,
    so a sheet re-exported with a corrected price no longer recognises itself
    by `source_email_id` -- but for an Idealista link the id branch catches it,
    and for the rest the sheet is the only thing that knows whether two lines
    are one listing re-priced or two plots the owner listed separately.

    What is *not* used is a bare URL match. A category link is not an identity:
    four different Ribadesella plots share one in this sheet, and matching on
    it would fold them into a single listing. A `/inmueble/<id>/` URL is safe
    for exactly the reason a category one is not -- the id in it names one
    listing.
    """
    from models import Property
    from utils.listing_search import extract_listing_id, listing_id_clause

    found = Property.query.filter_by(source_email_id=source_email_id_for(row)).first()
    if found is not None:
        return found

    listing_id = extract_listing_id(row.get("url"))
    if listing_id is not None:
        return Property.query.filter(listing_id_clause(Property, listing_id)).first()
    return None


def read_rows(path: str, sheet: str) -> List[Dict[str, Any]]:
    """Read the sheet and say, per line, what would happen. Writes nothing."""
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise SystemExit(f"Sheet is missing required column(s): {missing}")
        raw_rows = list(reader)

    out: List[Dict[str, Any]] = []
    for raw in raw_rows:
        url = _text(raw.get("Direct URL"))
        research = _research(raw)
        row: Dict[str, Any] = {
            "url": url,
            "title": _text(raw.get("Location")),
            "municipality": _text(raw.get("Municipality")),
            "price": _number(raw.get("Price €")),
            "area": _number(raw.get("Plot m²")),
            "area_type": _area_type(_text(raw.get("Type"))),
            "research": research,
            "description": _description(research, sheet),
            "status": STATUS_NEW,
            "reason": None,
        }
        if not url:
            row["status"] = STATUS_REJECTED
            row["reason"] = "no link in the sheet"
        elif not row["title"]:
            row["status"] = STATUS_REJECTED
            row["reason"] = "no location in the sheet"
        else:
            for column in ("Price €", "Plot m²"):
                if _unreadable_number(raw.get(column)):
                    row["status"] = STATUS_REJECTED
                    row["reason"] = (
                        f"{column} is {raw.get(column)!r}, which is ambiguous "
                        "-- a thousands separator and a decimal point look the "
                        "same here"
                    )
                    break
        out.append(row)
    return out


def mark_duplicates(rows: List[Dict[str, Any]]) -> None:
    """Ask the database about each link, in place."""
    for row in rows:
        if row["status"] != STATUS_NEW:
            continue
        found = _existing(row)
        if found is not None:
            row["status"] = STATUS_DUPLICATE
            row["existing_id"] = found.id
            row["existing_title"] = found.title


def _profile(name: str, create: bool):
    from app import db
    from models import SearchProfile

    profile = SearchProfile.query.filter_by(name=name).first()
    if profile is not None or not create:
        return profile
    profile = SearchProfile(name=name[:120], is_active=True, is_default=False)
    db.session.add(profile)
    db.session.commit()
    logger.info("Created subscription %r (id %s)", profile.name, profile.id)
    return profile


def _stable_key(row: Dict[str, Any]) -> str:
    """What identifies one listing in this sheet.

    Not the URL. Six of the 50 rows in the sheet this was written for share two
    links, because no direct listing URL existed for them and the owner
    recorded the category page instead -- four different plots in Ribadesella
    behind `.../terrenos/ribadesella/e-baratos`, two in Folgueras behind a
    `/geo/` page. Keying on the URL made the second insert violate the unique
    constraint and, worse, would have meant discarding four real listings as
    duplicates of the first.

    So the key is the link plus what tells those rows apart: the location, the
    price and the plot size. Deterministic, so importing the same sheet twice
    recognises its own rows instead of creating them again.
    """
    from utils.listing_search import url_fragment

    parts = [
        url_fragment(row.get("url") or "") or (row.get("url") or ""),
        (row.get("title") or "").casefold(),
        "" if row.get("price") is None else f"{row['price']:.2f}",
        "" if row.get("area") is None else f"{row['area']:.2f}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def source_email_id_for(row: Dict[str, Any]) -> str:
    """Unique per listing, and legible about where the row came from.

    Not the word `manual`: that is the mistake STATUS-002 (#265) is about, and
    this row was neither ingested nor checked.
    """
    return f"{SOURCE_NAME}:{_stable_key(row)}"


def insert_rows(rows: List[Dict[str, Any]], *, profile_id: int, sheet: str) -> Dict:
    from app import db
    from models import Property, SearchProfile
    from services.property_classification_service import PropertyClassificationService
    from services.search_profile_service import SearchProfileService

    profile = db.session.get(SearchProfile, profile_id)
    rules = SearchProfileService.get_classification_rules(profile)

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    # Two rows the sheet records identically are one listing, and this batch
    # is the only thing that knows it: `mark_duplicates` asked the database
    # before any of these existed. Asking the database again per row would
    # work and is the wrong shape -- a batch should not need a round trip to
    # learn about itself, and the savepoint below is for the *other* writer.
    seen: set = set()

    for row in rows:
        if row["status"] != STATUS_NEW:
            continue

        key = source_email_id_for(row)
        if key in seen:
            skipped.append({"url": row.get("url"), "title": row.get("title")})
            continue
        seen.add(key)

        category, subtype = PropertyClassificationService.classify_sources(
            row.get("title"),
            row["research"].get("Type"),
            row.get("description"),
            rules or [],
        )

        prop = Property()
        prop.source_email_id = key
        prop.url = row["url"]
        prop.title = row["title"]
        prop.municipality = row["municipality"]
        prop.price = row["price"]
        prop.area = row["area"]
        prop.area_type = row["area_type"]
        prop.deal_type = "sale"
        prop.description = row["description"]
        prop.property_category = category
        prop.property_subtype = subtype
        prop.search_profile_id = profile_id
        # Nobody checked whether this listing is live, and the app did not
        # ingest it either. NULL is how this schema says that, and `null()`
        # rather than `None` because the column's Python-side default is
        # "ingest" and SQLAlchemy applies it to any attribute that is None at
        # flush (#391).
        prop.listing_status_source = null()
        # No `coordinate` key: there is no portal pin behind these rows, and
        # `services/coordinate_quality.portal_coordinate` reading None is the
        # correct answer -- these geocode like any other unlocated listing.
        prop.enrichment = {
            "import": {
                "source": SOURCE_NAME,
                "method": "csv",
                "sheet": sheet,
                "imported_at": datetime.utcnow().isoformat(),
                "research": row["research"],
            }
        }

        # Each row in its own SAVEPOINT, the lesson `services/fotocasa_import.py`
        # already carries: without it a single collision aborts the whole batch
        # at the one commit below, and every other row -- all of them valid --
        # is discarded. That is exactly how this import failed the first time
        # it was run for real.
        try:
            with db.session.begin_nested():
                db.session.add(prop)
                db.session.flush()
        except IntegrityError:
            db.session.expunge(prop)
            skipped.append({"url": row.get("url"), "title": row.get("title")})
            logger.warning("Refused by the database, skipped: %s", row.get("title"))
            continue

        created.append({"id": prop.id, "url": prop.url, "title": prop.title})

    db.session.commit()
    return {"created": created, "skipped": skipped}


def score(created: List[Dict[str, Any]]) -> int:
    """Free -- the scorer reads stored columns and calls nothing outbound."""
    if not created:
        return 0
    from app import db
    from models import Property
    from services.property_scoring_service import PropertyScoringService

    service = PropertyScoringService()
    done = 0
    for item in created:
        prop = db.session.get(Property, item["id"])
        if prop is None:
            continue
        try:
            service.calculate_for_property(prop, commit=True)
            done += 1
        except Exception:
            db.session.rollback()
            logger.warning("Scoring failed for %s", item["id"], exc_info=True)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--profile", required=True, help="Subscription name.")
    parser.add_argument("--sheet", default="", help="Label recorded on each row.")
    parser.add_argument(
        "--apply", action="store_true", help="Write; otherwise only report."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sheet = args.sheet or args.csv_path

    from app import create_app

    app = create_app()
    with app.app_context():
        rows = read_rows(args.csv_path, sheet)
        mark_duplicates(rows)

        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        logger.info("Sheet: %s row(s) -- %s", len(rows), counts)

        for row in rows:
            if row["status"] == STATUS_DUPLICATE:
                logger.info("  already here as #%s: %s", row["existing_id"], row["url"])
            elif row["status"] == STATUS_REJECTED:
                logger.info("  skipped (%s): %s", row["reason"], row.get("title"))

        if not args.apply:
            logger.info("Report only. Re-run with --apply to write.")
            return

        profile = _profile(args.profile, create=True)
        outcome = insert_rows(rows, profile_id=profile.id, sheet=sheet)
        scored = score(outcome["created"])
        logger.info(
            "Created %s listing(s) in %r, scored %s.",
            len(outcome["created"]),
            profile.name,
            scored,
        )


if __name__ == "__main__":
    main()
