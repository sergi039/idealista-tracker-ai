"""Turning a list of fotocasa links into rows, in two halves that cannot be
confused: read the pages, then -- only on a second, explicit press -- write.

The split is not ceremony. **This application cannot delete a property.** A
tree-wide search for a delete route or a `db.session.delete` on `Property`
finds none, so a row inserted from a misread page stays in the table, in the
medians, and in the comparable pool of every listing in its subscription. A
preview is the only undo there is, so the fetch half writes nothing at all and
the confirm half makes no network call.

The halves are also split by *time*. Ninety links at the courtesy pace in
`services/fotocasa_source.py` is four and a half minutes, and this app serves
on one gunicorn worker with four threads and no `--timeout` flag, so the
default thirty seconds applies. Holding a thread for minutes is how a
four-thread pool is exhausted by one person pressing a button twice -- the
hazard `routes/api_routes.py` already documents for the outbound endpoints.
So reading runs as a background job and confirming, which touches nothing but
the database, runs in the request.

One consequence worth stating because it reads as luck rather than design: a
deploy that kills the app container mid-fetch (#283) costs nothing here. The
job holds its results in memory until it finishes, and nothing is written
until the owner confirms, so an interrupted import loses a few minutes of
somebody else's bandwidth and no data.

`listing_status_source` is left NULL on purpose, and this is the one line in
the file that is a bug fix rather than a feature. The out-of-band script that
imported 324 of the rows in this table set it to `manual`, reasoning that the
row was entered by hand -- but that column answers "who established the listing
is live", and `services/listing_verification.py` reads it as such, so 324
listings nobody had ever checked were reported as verified (STATUS-002 in issue
#265; those rows were repaired on production on 2026-08-17). The row was not
ingested either, so `ingest` would be the same mistake with a different word.
Nobody has checked it, and NULL is how this schema says that.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from sqlalchemy import null
from sqlalchemy.exc import IntegrityError

from services import portal_photos
from services.coordinate_quality import record_portal_coordinate

from services import advertiser
from services.fotocasa_source import (
    SOURCE_NAME,
    fetch_listing,
    listing_id_from_url,
    normalize_url,
    url_says_plot,
)

logger = logging.getLogger(__name__)

# What the preview says about each pasted link.
STATUS_NEW = "new"
STATUS_DUPLICATE = "duplicate"
STATUS_REFUSED = "refused"
STATUS_REJECTED = "rejected"


# Every portal-sourced row goes through the writers below, so the per-portal
# knowledge they need lives in one table: how a listing id appears in that
# portal's stored URL. Ids are only unique *within* a portal -- fotocasa and
# milanuncios can both have a listing 190280914 -- which is why the pattern
# is per source and the dash/`.htm`/`/d` anchors are part of it.
PORTAL_URL_PATTERNS = {
    "fotocasa": "%/{listing_id}/d%",
    "milanuncios": "%-{listing_id}.htm",
    "yaencontre": "%-{listing_id}",
}


# `source_email_id` is the only NOT NULL + UNIQUE column on `Property`, so it
# is both the bookkeeping fact "where this row came from" and, for free, the
# constraint that a listing cannot be imported twice. The prefix is the source
# rather than the word `manual`: see the module docstring.
def source_email_id_for(listing_id: int, source: str = SOURCE_NAME) -> str:
    return f"{source}:{listing_id}"


def existing_by_listing_id(listing_id: int, source: str = SOURCE_NAME):
    """The row already holding this listing, whichever way it got here.

    Two lookups because two importers exist. `source_email_id` catches
    anything this module wrote; the URL pattern catches the 56 rows the
    out-of-band script wrote, whose ids sit in a `manual:<batch>:<id>`
    string this module would never construct. Measured 2026-08-17: all 56
    stored fotocasa URLs end in `/<id>/d`, so the pattern really does reach
    every one of them. The other portals have no out-of-band rows, but the
    URL half stays for them too: it is what keeps a hand-inserted row from
    being re-ingested the day one exists.
    """
    from models import Property

    row = Property.query.filter_by(
        source_email_id=source_email_id_for(listing_id, source)
    ).first()
    if row is not None:
        return row
    pattern = PORTAL_URL_PATTERNS[source].format(listing_id=listing_id)
    return Property.query.filter(Property.url.ilike(pattern)).first()


def _parse_published(value: Optional[str]) -> Optional[datetime]:
    """Fotocasa's `creationDate`, or None.

    Never `now()` on a parse failure: a fabricated publication date makes a
    listing look fresh in a table sorted by date, which is the one thing that
    column is used for.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    logger.warning("Unparseable fotocasa creationDate %r", value)
    return None


def rejected_row(url: str, reason: str) -> Dict[str, Any]:
    """A link that was never fetched, and why."""
    return {"url": url, "status": STATUS_REJECTED, "reason": reason}


def preview_row(listing) -> Dict[str, Any]:
    """One line of the preview table, as plain JSON-able data.

    Plain data because it becomes a background job's result, which is
    persisted and read back by a different request.
    """
    row: Dict[str, Any] = {
        "url": normalize_url(listing.url) or listing.url,
        "listing_id": listing.listing_id,
        "status": STATUS_NEW,
        "reason": None,
        "title": listing.title,
        "price": listing.price,
        "area": listing.area,
        "area_type": listing.area_type,
        "plot_area": listing.plot_area,
        "deal_type": listing.deal_type,
        "municipality": listing.municipality,
        "province": listing.province,
        "postal_code": listing.postal_code,
        "district": listing.district,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "description": listing.description,
        "building_type": listing.building_type,
        "agency": listing.agency,
        "publisher_type": listing.publisher_type,
        "client_type_id": listing.client_type_id,
        "published_at": listing.published_at,
        "attributes": dict(listing.attributes or {}),
        "portal_accuracy": dict(listing.portal_accuracy or {}),
        # A field added to the dataclass and not here is dropped again at this
        # layer -- this dict, not the dataclass, is what a background job
        # persists and a later request reads back.
        "photos": dict(listing.photos or {}),
    }
    if not listing.ok:
        row["status"] = STATUS_REFUSED
        row["reason"] = listing.refusal
    return row


def read_urls(urls: List[str], *, session=None) -> List[Dict[str, Any]]:
    """Fetch and read every link. Writes nothing.

    Runs as a background job. Every outcome is reported per link, including
    the ones that failed: a link that could not be read is a line in the
    table saying why, never a line quietly missing from it.

    One `requests.Session` for the whole batch, so ninety links cost one
    connection rather than ninety handshakes to a host we are already asking
    to be patient with us.

    Each link is read inside its own `try`. `fetch_listing` turns the
    *expected* failures into refusals already, so this catches the
    unexpected -- and the reason it must is arithmetic: without it, one bad
    link at position forty loses the thirty-nine pages that were read before
    it, and those cost real time on somebody else's server.
    """
    from models import Property  # noqa: F401  (registers the mapper)

    http = session or requests.Session()

    rows: List[Dict[str, Any]] = []
    for raw in urls:
        listing_id = listing_id_from_url(raw)
        if listing_id is None:
            rows.append(rejected_row(raw, "not a fotocasa listing link"))
            continue

        existing = existing_by_listing_id(listing_id)
        if existing is not None:
            rows.append(
                {
                    "url": normalize_url(raw) or raw,
                    "listing_id": listing_id,
                    "status": STATUS_DUPLICATE,
                    "reason": None,
                    "existing_id": existing.id,
                    "title": existing.title,
                }
            )
            continue

        try:
            rows.append(preview_row(fetch_listing(raw, session=http)))
        except Exception:
            logger.exception("Reading %s failed unexpectedly", raw)
            rows.append(rejected_row(raw, "could not be read"))

    return rows


def classify_row(row: Dict[str, Any], profile_id: Optional[int]):
    """Category and subtype, from the classifier every other row goes through.

    Fotocasa states its own `buildingType`, and it would be easy to map it
    here -- which would be a second vocabulary, drifting from the one the
    profile's own rules are written in. It is passed to the shared classifier
    as one of the texts instead, so a per-subscription rule reaches these rows
    exactly as it reaches an ingested one.

    The type word fotocasa puts in its own canonical path rides in the same
    text, ahead of `buildingType`, and only when that word is a land one. It
    has to be *some* text rather than a verdict returned here, or a
    subscription's own rules would stop reaching these rows -- and it has to
    be ahead of the description, because the rules are read in text order and
    a plot advertised as somewhere to build "la casa de tus sueños" otherwise
    matches `housing/house` on the strength of the marketing copy. That is
    exactly how property 1333, a 16,782 m² field, became a house.
    """
    from app import db
    from models import SearchProfile
    from services.property_classification_service import PropertyClassificationService
    from services.search_profile_service import SearchProfileService

    profile = db.session.get(SearchProfile, profile_id) if profile_id else None
    # Takes the profile object and falls back to the global rules itself, so
    # a subscription with no rules of its own is not a special case here.
    rules = SearchProfileService.get_classification_rules(profile)

    portal_type = "terreno" if url_says_plot(row.get("url")) else None
    stated_type = " ".join(
        part for part in (portal_type, row.get("building_type")) if part
    )

    return PropertyClassificationService.classify_sources(
        row.get("title"),
        stated_type or None,
        row.get("description"),
        rules or [],
    )


def build_property(
    row: Dict[str, Any],
    *,
    profile_id: Optional[int],
    classification: Optional[tuple] = None,
    method: str = "portal_payload",
    email_date: Optional[datetime] = None,
    email_subject: Optional[str] = None,
    email_sender: Optional[str] = None,
    source: str = SOURCE_NAME,
    record_advertiser: bool = True,
):
    """One Property from one read portal listing, not yet in any session.

    Every door into the table for a portal-sourced row goes through here --
    the fotocasa paste-links import, and the alert-email ingestion for
    fotocasa, milanuncios and yaencontre (`services/property_imap_service.py`)
    -- because parallel builders would be one incident away from disagreeing
    about the dedup key, the NULL `listing_status_source` or the portal pin,
    and a listing that arrives through two doors must land as one row either
    way: the `<source>:<id>` `source_email_id` is that guarantee, so every
    writer must construct it identically. The module keeps its historical
    fotocasa name because the paste-links UI and its tests live on it; the
    writers themselves are portal-generic.

    `method` records which door it was; `email_date` overrides the portal's
    `creationDate` for a row that arrived by mail, because for an ingested row
    that column means "when the email arrived" everywhere else in this table.
    `record_advertiser=False` is for a source that never read who is selling
    (yaencontre rows come off the email card alone): an advertiser block with
    all-None evidence would store "the source did not say" for a source
    nobody asked, where an absent key honestly reads "not established".
    """
    from models import Property

    listing_id = int(row["listing_id"])
    if classification is None:
        classification = classify_row(row, profile_id)
    category, subtype = classification

    prop = Property()
    prop.source_email_id = source_email_id_for(listing_id, source)
    prop.url = row.get("url")
    prop.title = row.get("title")
    prop.description = row.get("description")
    prop.price = row.get("price")
    prop.area = row.get("area")
    prop.area_type = row.get("area_type") or "unknown"
    prop.plot_area = row.get("plot_area")
    prop.deal_type = row.get("deal_type") or "sale"
    prop.municipality = row.get("municipality")
    # One hop through a route (migration 025): the paste door lets a
    # caller name any profile id, and on SQLite — the test suite — no
    # trigger canonicalizes it. PostgreSQL remains the guarantee; this is
    # the readable boundary at the one builder every portal door shares.
    from app import db
    from models import SearchProfile
    from services.property_classification_service import PropertyClassificationService
    from services.search_profile_service import SearchProfileService

    profile_obj = db.session.get(SearchProfile, profile_id) if profile_id else None
    canonical = SearchProfileService.canonical_profile(profile_obj)
    prop.search_profile_id = canonical.id if canonical else profile_id
    prop.property_category = category
    prop.property_subtype = subtype
    # The one place the classifier's verdict and the portal's own area meet.
    # Without this a row classified `land` keeps whatever `area_type` the
    # payload implied, and a parcel is counted as floor space.
    PropertyClassificationService.reconcile_area_type(prop)
    prop.attributes = row.get("attributes") or {}
    prop.email_date = email_date or _parse_published(row.get("published_at"))
    prop.email_subject = email_subject
    prop.email_sender = email_sender

    # The portal's own coordinate, recorded as approximate whatever it
    # says. `services/coordinate_quality.py` grants `precise` zero slack,
    # which unlocks a paid travel run; the only fotocasa page measured so
    # far declares `isExact: false`, so nothing here has ever seen the
    # evidence that would justify the stronger label.
    if row.get("latitude") is not None and row.get("longitude") is not None:
        prop.location_lat = row["latitude"]
        prop.location_lon = row["longitude"]
        prop.location_accuracy = "approximate"

    # Not `manual`, and not `ingest`. See the module docstring.
    #
    # `null()` and not `None`: the column carries a Python-side default of
    # `"ingest"` (models.py), and SQLAlchemy applies a Python default to
    # any attribute that is None at flush -- so the plain assignment reads
    # like the intent and stores the opposite. A SQL expression is a value,
    # so the default does not fire. Measured by the test that asserts this
    # column, which failed with `'ingest' is not None` before this line.
    prop.listing_status_source = null()

    prop.enrichment = {
        "import": {
            "source": source,
            "method": method,
            "listing_id": listing_id,
            "imported_at": datetime.utcnow().isoformat(),
            "agency": row.get("agency"),
            "publisher_type": row.get("publisher_type"),
            "client_type_id": row.get("client_type_id"),
            "province": row.get("province"),
            "postal_code": row.get("postal_code"),
            "district": row.get("district"),
            "locality": row.get("locality"),
            "building_type": row.get("building_type"),
            # Verbatim, so the day somebody measures a page that claims an
            # exact coordinate, the evidence for revisiting the label above
            # is already in the row rather than needing a re-fetch.
            "portal_accuracy": row.get("portal_accuracy") or {},
        },
    }
    # The portal's own photographs, beside its own accuracy claim and for the
    # same reason. The key is written ONLY when the row really carries a list,
    # because a missing key means "nobody captured any" and an empty list means
    # "the payload was read and named none" -- two different facts, and the
    # page says which (services/portal_photos.py).
    photos = row.get("photos")
    if isinstance(photos, dict) and isinstance(photos.get("items"), list):
        prop.enrichment["import"][portal_photos.ENRICHMENT_KEY] = {
            "items": [
                dict(photo) for photo in photos["items"] if isinstance(photo, dict)
            ],
            "published": photos.get("published"),
        }
    if record_advertiser:
        # Who is selling, recorded on the way past. The page has been
        # fetched already, so this costs nothing and spares the row the one
        # thing this deployment cannot do later on demand for every site --
        # go back and read the advert. `services/advertiser.py` owns what
        # the portal's word is taken to mean.
        prop.enrichment[advertiser.ENRICHMENT_KEY] = advertiser.portal_verdict(
            portal_type=row.get("publisher_type"),
            client_type_id=row.get("client_type_id"),
            client_name=row.get("agency"),
            site=source,
        )

    # The portal's own pin, through the writer that lives beside the reader
    # in `services/coordinate_quality.py`, so a re-geocode cannot silently
    # replace it with something derived from the title (#393).
    prop.enrichment = record_portal_coordinate(
        prop.enrichment,
        source=source,
        lat=row.get("latitude"),
        lon=row.get("longitude"),
    )

    return prop


def insert_rows(
    rows: List[Dict[str, Any]], *, profile_id: Optional[int]
) -> Dict[str, Any]:
    """Create a Property for every previewed row that is still new.

    Re-checks each row against the table rather than trusting the preview:
    minutes may have passed, and the alternative is an IntegrityError from the
    unique constraint that would abort the whole batch over one link somebody
    imported in another tab.
    """
    from app import db

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows:
        if row.get("status") != STATUS_NEW:
            continue
        listing_id = row.get("listing_id")
        if not listing_id:
            continue

        existing = existing_by_listing_id(int(listing_id))
        if existing is not None:
            skipped.append({"url": row.get("url"), "existing_id": existing.id})
            continue

        prop = build_property(row, profile_id=profile_id)

        # Each row lands inside its own SAVEPOINT, so a collision costs that
        # row and not the batch.
        #
        # The `existing_by_listing_id` check above is a plain SELECT and
        # cannot see a row another transaction has inserted but not committed,
        # so two confirms overlapping on one listing both pass it -- a double
        # click on the Add button is enough, and two tabs certainly are. The
        # second one's flush then raises IntegrityError from the unique
        # constraint on `source_email_id`, and without this it propagated out
        # of the loop before the single `commit()` below was ever reached: the
        # caller rolled back, and every other row in that batch, all of them
        # valid, was discarded while the page said "Import failed". The
        # docstring promised exactly this could not happen; it was true only
        # of the sequential case the test exercised.
        try:
            with db.session.begin_nested():
                db.session.add(prop)
                db.session.flush()
        except IntegrityError:
            # The other transaction has committed it by now, so this is the
            # ordinary duplicate outcome arriving a moment late.
            existing = existing_by_listing_id(int(listing_id))
            skipped.append(
                {
                    "url": row.get("url"),
                    "existing_id": existing.id if existing is not None else None,
                }
            )
            continue

        created.append({"id": prop.id, "url": prop.url, "title": prop.title})

    db.session.commit()

    scored = _score(created)

    return {"created": created, "skipped": skipped, "scored": scored}


def _score(created: List[Dict[str, Any]]) -> int:
    """Score the new rows, as ingestion does.

    Free -- the scorer reads stored columns and calls nothing outbound -- and
    it respects `AUTO_PROPERTY_SCORING` for the same reason ingestion does:
    a deployment that has turned scoring off did so deliberately. A row that
    cannot be scored keeps a NULL score, which sorts last and reads as
    "not scored", rather than a zero that reads as "worthless".
    """
    if not created:
        return 0

    from config import Config

    if not getattr(Config, "AUTO_PROPERTY_SCORING", True):
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
            logger.warning("Scoring failed for property %s", item["id"], exc_info=True)
    return done
