"""What a fotocasa.es listing page hands us, in one place.

Every listing in this database used to arrive as an Idealista alert email.
Fotocasa listings are here too -- 56 of 730 rows on 2026-08-17 -- but nothing
in the application could produce one: `utils/idealista_extractors.py` only
matches `idealista.com` URLs, so those rows were written by a hand-run import
script outside this repository. This module is the missing half: given a
fotocasa listing URL, what the page actually says about the listing.

Three properties of the target were measured on 2026-08-17 from this machine,
not assumed, because each one decides a design question:

* **It is not Idealista.** `services/listing_status_service.py` documents that
  idealista.com answers DataDome to every request from here, browser headers
  included. Fotocasa answers `200` to the bare product token in
  `utils.http.HTTP_USER_AGENT` and `403` to `python-requests/2.31.0`,
  `curl/8.7.1` and a bare `Mozilla/5.0`. There is no captcha, no JS challenge
  and no DataDome anywhere in either body: the filter is on the client name,
  so identifying ourselves honestly is *sufficient*, and nothing here spoofs a
  browser. If that ever stops being true the answer is to stop fetching, not
  to dress up as one -- the same rule the Idealista scraper lives under.
* **robots.txt allows the listing page** for `*` and disallows `/buscar/`.
  So one page on the owner's request is within it and walking search results
  is not, which is why nothing here takes a search URL.
* **The data is already structured.** The page carries
  `<script type="application/json" id="__initial_props__">`, ~40 KB, holding
  price, surface, coordinates, municipality and the advert text. No LLM is
  involved, no HTML parser is needed, and `trafilatura` -- declared in
  `pyproject.toml` and imported nowhere -- stays unimported. The `ld+json` on
  the page is a BreadcrumbList and carries none of it.

Three things in that payload are traps, and all three are why this reads it
field by field rather than handing the blob to something generic:

* **`realEstate.address` and `realEstateAdDetailEntityV2.address` disagree
  about `municipality`.** On the measured page the first says `Avilés` (with
  `cityId: 33004`, which is Avilés's INE code) and the second says `Llaranes`,
  which is the *district*. `utils/municipality_grouping.py` groups four
  listing surfaces on this string and `/municipalities` joins it to INE, so
  the wrong one would invent a municipality that no join can resolve. This
  module reads `realEstate.address` and never the other one.
* **`0` means "not set", not zero.** The measured plot carries
  `rooms: 0, bathrooms: 0, heating: 0` -- fotocasa's own filler for fields
  that do not apply. Reading a `0` surface as an area of zero square metres
  would be #98 with a number in the place of a blank, so a zero is absent.
* **The coordinate is declared imprecise by the portal itself**
  (`coordinates.accuracy: 0`, and `address.isExact: false` in the detail
  block). See `location_accuracy` below.

`location_accuracy` is always `approximate`, never `precise`. `precise` is the
strongest claim in this codebase -- `services/coordinate_quality.py` grants it
zero slack, which unlocks a ~$0.36 travel run and an unbounded sea distance --
and the only fotocasa page measured so far says `isExact: false`. Promoting a
row on the strength of an `isExact: true` nobody has ever seen would ship an
untested branch straight into the one place where being wrong costs money and
credibility. The portal's own two flags are kept verbatim in the import
provenance, so the day someone measures a page that claims exactness, the
evidence for changing this is already stored.
"""

from __future__ import annotations

import html as html_entities
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from utils.http import HTTP_USER_AGENT, RateGate, request_with_retries

logger = logging.getLogger(__name__)

SOURCE_NAME = "fotocasa"

# Courtesy pacing for a host that publishes no rate limit. It is not a
# measured ceiling and must not be presented as one: what was measured is that
# the probe run of 2026-08-17, spaced at three seconds, was served in full
# without a single refusal. The gate matters because the import takes a list --
# ninety links at this interval is four and a half minutes of somebody else's
# server, which is why the import runs as a background job rather than inside a
# request.
FOTOCASA_MIN_INTERVAL_S = 3.0
FOTOCASA_GATE = RateGate(FOTOCASA_MIN_INTERVAL_S, name="fotocasa")

FETCH_TIMEOUT_S = 20

# The listing detail page, in any of fotocasa's five language spellings:
# `/en/buy/land/aviles/llaranes/190280914/d`, `/es/comprar/terreno/...`.
# The id and the trailing `/d` are the invariant; the words in front are not.
_LISTING_PATH = re.compile(r"/(\d{4,})/d/?$")

_INITIAL_PROPS = re.compile(
    r'<script[^>]*id="__initial_props__"[^>]*>(.*?)</script>', re.S
)

# What the 403 page says. Kept so a refusal is reported as a refusal rather
# than as a listing that could not be parsed -- the distinction the whole #98
# family turns on.
_REFUSAL_MARKERS = ("sentimos la interrupci", "access denied", "forbidden")

_HOSTS = ("fotocasa.es",)

# Reasons a page yields nothing. Deliberately the vocabulary
# `services/listing_status_service.py` already uses for the same situations,
# so the two are read the same way when both appear in one log.
REFUSAL_BLOCKED = "blocked"
REFUSAL_HTTP_ERROR = "http_error"
REFUSAL_TIMEOUT = "timeout"
REFUSAL_NOT_A_LISTING = "not_the_listing_page"
REFUSAL_NO_PAYLOAD = "no_payload"
REFUSAL_UNREADABLE = "unreadable_payload"
REFUSAL_NOT_FOTOCASA = "not_a_fotocasa_url"


def is_fotocasa_url(url: Optional[str]) -> bool:
    """True for a URL on fotocasa.es, whatever the subdomain or scheme."""
    host = _host_of(url)
    return any(host == h or host.endswith("." + h) for h in _HOSTS)


def _host_of(url: Optional[str]) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""


def listing_id_from_url(url: Optional[str]) -> Optional[int]:
    """The listing id a fotocasa detail URL names, or None.

    None for a search page, an agency page or a bare host: those carry no id,
    and refusing them here is what keeps the import from walking a results
    page robots.txt puts out of bounds.
    """
    if not is_fotocasa_url(url):
        return None
    try:
        path = urlsplit((url or "").strip()).path
    except ValueError:
        return None
    found = _LISTING_PATH.search(path)
    return int(found.group(1)) if found else None


# A URL inside an email body. Hrefs end at the closing quote and text-part
# links end at whitespace; both terminators are excluded from the match.
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def listing_urls_in_text(text: Optional[str]) -> List[str]:
    """Every fotocasa listing URL in a block of text, one per listing id.

    Built for alert email bodies: the mail links each listing and also the
    search page, the alert settings and the unsubscribe endpoint, and only a
    URL naming exactly one listing (the ``/<id>/d`` shape) comes back --
    the same gate `listing_id_from_url` already is for pasted links, so a
    search page robots.txt puts out of bounds can never be fetched from here
    either. A listing linked twice (photo and title) is one entry; order is
    preserved so the email's own ordering survives into the ingest loop.

    HTML entities are unescaped first because hrefs arrive ``&amp;``-encoded;
    the listing id lives in the path, so this only matters for not carrying
    a mangled query tail into the fetch.
    """
    found: List[str] = []
    seen: set = set()
    for match in _URL_IN_TEXT.finditer(text or ""):
        candidate = html_entities.unescape(match.group(0))
        listing_id = listing_id_from_url(candidate)
        if listing_id is None or listing_id in seen:
            continue
        seen.add(listing_id)
        found.append(candidate)
    return found


def normalize_url(url: Optional[str]) -> Optional[str]:
    """The listing URL with its query string and fragment dropped.

    Fotocasa links arrive carrying campaign parameters. They identify the
    delivery, not the listing, and `utils/listing_search.py` already drops the
    same class of tail when matching a pasted link against a stored one.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "//" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return f"{parts.scheme or 'https'}://{parts.hostname}{parts.path}".rstrip("/")


@dataclass
class FotocasaListing:
    """One listing page, read.

    `refusal` is set when the page yielded nothing; every field is then None.
    A field that is None on a successful read means the page did not say --
    never zero, never a guess.
    """

    url: str
    listing_id: Optional[int] = None
    refusal: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = None
    area: Optional[float] = None
    area_type: str = "unknown"
    # The parcel, where the payload states one (surfaceLand / groundSurface,
    # 0-as-blank). Carried separately from `area` because a house's `area`
    # is its BUILT surface and the criteria verdict needs both (#498).
    plot_area: Optional[float] = None
    deal_type: Optional[str] = None
    municipality: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    district: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    description: Optional[str] = None
    building_type: Optional[str] = None
    agency: Optional[str] = None
    publisher_type: Optional[str] = None
    client_type_id: Optional[int] = None
    published_at: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    portal_accuracy: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.refusal is None


def _positive_number(value: Any) -> Optional[float]:
    """A number the portal actually set, or None.

    Zero is not a value here. The measured plot carries `rooms: 0`,
    `bathrooms: 0` and `heating: 0` for fields that do not apply to it, so
    fotocasa uses zero as its blank. A surface of zero square metres is not a
    measurement of anything, and storing one would let the scorer treat an
    absence as a fact.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _coordinate(value: Any) -> Optional[float]:
    """A latitude or longitude the portal set.

    Unlike a surface, zero is a real coordinate -- so this accepts it and only
    refuses what is not a number at all. Nowhere near Spain, but the guard
    belongs to whoever validates a location, not to the reader of a JSON field.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_listing(html: str, url: str) -> FotocasaListing:
    """Read a fetched fotocasa listing page.

    Pure: no network, no clock, no database. That is what lets the test read
    the real 40 KB payload of a real listing out of `tests/data/`.
    """
    listing = FotocasaListing(url=url, listing_id=listing_id_from_url(url))

    body = html or ""
    found = _INITIAL_PROPS.search(body)
    if not found:
        lowered = body[:20000].lower()
        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            listing.refusal = REFUSAL_BLOCKED
        else:
            listing.refusal = REFUSAL_NO_PAYLOAD
        return listing

    try:
        payload = json.loads(found.group(1))
    except (ValueError, TypeError):
        listing.refusal = REFUSAL_UNREADABLE
        return listing

    estate = payload.get("realEstate") if isinstance(payload, dict) else None
    if not isinstance(estate, dict):
        listing.refusal = REFUSAL_UNREADABLE
        return listing

    detail = payload.get("realEstateAdDetailEntityV2")
    detail = detail if isinstance(detail, dict) else {}

    # The id from the payload wins over the one in the path: the path is what
    # was pasted and the payload is what was served, and a redirect between
    # them is the case where they differ.
    payload_id = _positive_number(estate.get("id"))
    if payload_id is not None:
        listing.listing_id = int(payload_id)

    # `seoTitle` rather than `propertyTitle`: the latter is the bare
    # "Land for sale", while the former is "Land for sale in Llaranes,
    # Avilés". `PropertyLocationService._build_geocoding_queries` reads the
    # text after "in", so the generic one would hand the geocoder nothing.
    listing.title = _text(payload.get("seoTitle")) or _text(
        payload.get("propertyTitle")
    )

    # A hidden price is absent, not free. `showPrice` is the portal saying so.
    if estate.get("showPrice") is not False:
        listing.price = _positive_number(estate.get("price"))
        if listing.price is None:
            price_block = detail.get("price")
            if isinstance(price_block, dict):
                listing.price = _positive_number(price_block.get("amount"))

    address = estate.get("address")
    address = address if isinstance(address, dict) else {}
    # `realEstate.address`, never `realEstateAdDetailEntityV2.address`: see the
    # module docstring. The second one names the district as the municipality.
    listing.municipality = _text(address.get("municipality")) or _text(
        address.get("city")
    )
    listing.province = _text(address.get("province"))
    listing.postal_code = _text(address.get("zipCode"))
    listing.district = _text(address.get("district"))

    coordinates = estate.get("coordinates")
    coordinates = coordinates if isinstance(coordinates, dict) else {}
    listing.latitude = _coordinate(coordinates.get("latitude"))
    listing.longitude = _coordinate(coordinates.get("longitude"))

    detail_address = detail.get("address")
    detail_address = detail_address if isinstance(detail_address, dict) else {}
    listing.portal_accuracy = {
        "coordinates_accuracy": coordinates.get("accuracy"),
        "record_accuracy": estate.get("accuracy"),
        "is_exact": detail_address.get("isExact"),
    }

    listing.building_type = _text(estate.get("buildingSubtype")) or _text(
        estate.get("buildingType")
    )

    # `transactionTypeId` is 1 for a sale on the measured page. Anything else
    # is read as a rental rather than guessed to be a sale: this database is
    # venta-first, `PropertyIMAPService` drops rentals at ingest when
    # `SALE_ONLY` is set, and a rental priced per month stored as a sale price
    # would be a four-figure plot in every ranking on the site.
    transaction = estate.get("transactionTypeId")
    if transaction is not None:
        listing.deal_type = "sale" if transaction == 1 else "rent"

    features = estate.get("features")
    features = features if isinstance(features, dict) else {}
    built = _positive_number(features.get("surface"))
    land = _positive_number(features.get("surfaceLand")) or _positive_number(
        detail.get("groundSurface")
    )
    listing.plot_area = land
    if (listing.building_type or "").lower() in ("land", "terreno", "terreny"):
        listing.area = land or built
        listing.area_type = "plot" if listing.area is not None else "unknown"
    elif built is not None:
        listing.area = built
        listing.area_type = "built"
    elif land is not None:
        listing.area = land
        listing.area_type = "plot"

    # The full advert text. Both blocks carry the same 825 characters on the
    # measured page; the detail one is a plain string while the other is keyed
    # by locale, so it is read first and the locale map is the fallback.
    listing.description = _text(detail.get("description"))
    if listing.description is None:
        descriptions = estate.get("descriptions")
        if isinstance(descriptions, dict):
            for value in descriptions.values():
                listing.description = _text(value)
                if listing.description:
                    break

    listing.agency = _text(estate.get("clientName")) or _text(estate.get("clientAlias"))
    listing.published_at = _text(detail.get("creationDate"))

    # Who is advertising, in the portal's own vocabulary. Two blocks carry the
    # same enum and `publisher` is read first because it describes exactly this
    # -- `agency` is the same value seen from the agency's side, and on a
    # private advert there may be no agency block to see it from at all.
    # `clientTypeId` is the numeric twin (`3` beside `professional` on every
    # page measured on 2026-08-17); it is recorded and never decides, because
    # nobody here has seen what the other numbers mean.
    # `services/advertiser.py` owns what these values are taken to mean.
    publisher = detail.get("publisher")
    publisher = publisher if isinstance(publisher, dict) else {}
    agency_block = detail.get("agency")
    agency_block = agency_block if isinstance(agency_block, dict) else {}
    listing.publisher_type = _text(publisher.get("type")) or _text(
        agency_block.get("type")
    )
    client_type_id = _positive_number(estate.get("clientTypeId"))
    listing.client_type_id = int(client_type_id) if client_type_id is not None else None

    rooms = _positive_number(features.get("rooms"))
    bathrooms = _positive_number(features.get("bathrooms"))
    if rooms is not None:
        listing.attributes["bedrooms"] = int(rooms)
    if bathrooms is not None:
        listing.attributes["bathrooms"] = int(bathrooms)

    return listing


def fetch_listing(
    url: str, session: Optional[requests.Session] = None
) -> FotocasaListing:
    """Fetch one listing page and read it.

    Paced by `FOTOCASA_GATE`, which is handed to `request_with_retries` rather
    than taken around it: the transport takes the gate before *every* attempt,
    and a caller that paces only its first request leaves the retries unpaced,
    which is the traffic a struggling endpoint sees most of.
    """
    if listing_id_from_url(url) is None:
        return FotocasaListing(url=url, refusal=REFUSAL_NOT_FOTOCASA)

    http = session or requests.Session()
    headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }

    try:
        response = request_with_retries(
            http.get,
            url,
            headers=headers,
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
            logger=logger,
            gate=FOTOCASA_GATE,
        )
    except requests.Timeout:
        return FotocasaListing(url=url, refusal=REFUSAL_TIMEOUT)
    except requests.RequestException:
        return FotocasaListing(url=url, refusal=REFUSAL_HTTP_ERROR)

    if response.status_code == 403:
        return FotocasaListing(url=url, refusal=REFUSAL_BLOCKED)
    if response.status_code != 200:
        return FotocasaListing(url=url, refusal=REFUSAL_HTTP_ERROR)

    # The final URL after redirects is where the server says what it served.
    # A listing that has been taken down redirects to a search page, which
    # parses to nothing anyway -- but naming it here reports the right reason.
    served_id = listing_id_from_url(getattr(response, "url", "") or url)
    asked_id = listing_id_from_url(url)
    if served_id is None or (asked_id is not None and served_id != asked_id):
        return FotocasaListing(
            url=url, listing_id=asked_id, refusal=REFUSAL_NOT_A_LISTING
        )

    return parse_listing(response.text or "", url)


def split_urls(raw: Optional[str]) -> List[str]:
    """The links in a pasted block, in order, without repeats.

    One per line is the shape the box asks for, but a block copied out of a
    document arrives with the links run together by spaces, so both separate.

    Two spellings of one listing are one link. The id is the identity, not the
    path: `/en/buy/land/aviles/llaranes/190280914/d` and
    `/es/comprar/terreno/aviles/llaranes/190280914/d` are the same advert in
    two languages, and keying on the normalized path would fetch it twice --
    paying the gate twice for a row the database would refuse anyway.
    """
    seen = set()
    out: List[str] = []
    for token in re.split(r"[\s,;]+", (raw or "").strip()):
        candidate = token.strip()
        if not candidate:
            continue
        listing_id = listing_id_from_url(candidate)
        key = (
            f"id:{listing_id}"
            if listing_id is not None
            else (normalize_url(candidate) or candidate)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out
