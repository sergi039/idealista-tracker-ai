"""What a yaencontre alert email says about its listings, in one place.

Yaencontre is the one portal in this pipeline whose listing pages cannot be
read at all: measured 2026-08-30 from both the laptop and the Mac mini,
www.yaencontre.com answers 403 with a DataDome challenge to every request --
even `robots.txt` comes back as the captcha page -- exactly the machinery
that blocks idealista.com from these machines. Defeating that is not on the
table (the `listing_status_service` rule), so **the alert email is the whole
source**: every field a row gets comes off the email card, and nothing here
ever makes a network request.

What a card carries was measured on the real alerts of 2026-08-30
(`tests/data/yaencontre_alert_boiro.html`, tokens redacted): three anchors
per listing sharing one URL (photo, title, CTA), the title text ("Casa
adosada en venta en avenida Compostela, Outes"), a price paragraph
("180.000 €") and a facts line ("7 hab. | 2 baños | 294 m²"). No
coordinates, no seller information, no description -- so a yaencontre row
stores no advertiser verdict and no portal pin, and the geocoder fills the
coordinate from the title at ingest (`AUTO_GEOCODING`, the standing owner
decision in config.py).

The listing identity is the SECOND number in `/venta/casa/inmueble-<a>-<b>`:
in the measured mail `<a>` repeats across three different listings of one
seller (79977) while `<b>` is unique per advert, monotone with freshness.
The municipality is read from the title's last comma segment ("..., Outes");
a title with no comma keeps None rather than guessing -- the alert's own
subject names the *search polygon* ("... en Boiro"), which spans other
municipalities (the Boiro mail's first card is in Outes), so the subject is
deliberately never used for it.
"""

from __future__ import annotations

import html as html_entities
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from utils.idealista_extractors import extract_area_m2, extract_price

logger = logging.getLogger(__name__)

SOURCE_NAME = "yaencontre"

_HOSTS = ("yaencontre.com",)

# `/venta/casa/inmueble-75866-112395195`: the second number is the listing.
_LISTING_PATH = re.compile(r"/venta/[^/]+/inmueble-(\d+)-(\d+)/?$")

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

_ANCHOR = re.compile(
    r'<a\b[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<inner>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_BEDROOMS = re.compile(r"(\d+)\s*hab\b", re.IGNORECASE)
_BATHROOMS = re.compile(r"(\d+)\s*baño", re.IGNORECASE)

# The URL's own word for what is being sold: `/venta/<type>/inmueble-...`.
# A plot's surface is the plot; anything habitable measures its built area.
_PLOT_TYPES = ("terreno", "terrenos", "parcela", "solar", "finca")

# What yaencontre puts between a district and the municipality it belongs to:
# "Teis en Vigo", "Bocines - Nembro - Cardo en Gozón". Matched with its spaces
# and in lower case on purpose -- a bare "en" would cut inside a name, and no
# municipality in the five watched provinces carries " en " (checked against
# all 391 names in `data/ine_municipal.json`).
_DISTRICT_SEPARATOR = " en "


def is_yaencontre_url(url: Optional[str]) -> bool:
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
    """The listing id a yaencontre detail URL names, or None.

    None for the search page (`/venta/casas/custom/...`), the alert pages and
    the bare host: none of them carry the `inmueble-<a>-<b>` pair.
    """
    if not is_yaencontre_url(url):
        return None
    try:
        path = urlsplit((url or "").strip()).path
    except ValueError:
        return None
    found = _LISTING_PATH.search(path)
    return int(found.group(2)) if found else None


def normalize_url(url: Optional[str]) -> Optional[str]:
    """The listing URL with its query string and fragment dropped.

    The alert links carry `utm_*` and a per-send `utm_term` token: they
    identify the delivery, not the listing, the same class of tail
    `services/fotocasa_source.py` already drops.
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
class YaencontreCard:
    """One listing as its alert-email card states it.

    A field that is None means the card did not say -- never zero, never a
    guess. There is deliberately no `refusal`: nothing here fetches, so the
    only failure is an email this parser cannot read, and that surfaces as an
    empty list from `cards_in_email`, which the ingester logs and consumes.
    """

    url: str
    listing_id: int
    title: Optional[str] = None
    price: Optional[float] = None
    area: Optional[float] = None
    area_type: str = "unknown"
    deal_type: str = "sale"
    municipality: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)


def _municipality_from_title(title: Optional[str]) -> Optional[str]:
    """The municipality the card title names, or None.

    Yaencontre writes the place two ways, and both end in the municipality:
    "... avenida Compostela, Outes" puts it after the last comma, and
    "... calle Rosa, Teis en Vigo" puts a *district* there with the
    municipality behind an " en ". Titles with no street at all skip the comma
    entirely -- "Casa en venta en Boiro", "Casa adosada en venta en Esteiro en
    Ferrol" -- and the whole line is then the same shape.

    So: take the last comma segment when there is one, then whatever follows
    the last " en ". Measured over the 227 rows this parser has written to
    production, that moves 119 rows naming a real municipality to 227, leaves
    none unnamed (was 63) and leaves no district string at all (was 45).

    A title with neither a comma nor an " en " still returns None rather than
    a guess: `utils/municipality_grouping.py` groups four surfaces on this
    string, and one wrong pick invents a municipality that outlives it.
    """
    if not title:
        return None
    had_comma = "," in title
    head = title.rsplit(",", 1)[1].strip() if had_comma else title.strip()
    if _DISTRICT_SEPARATOR in head:
        head = head.rsplit(_DISTRICT_SEPARATOR, 1)[1].strip()
    elif not had_comma:
        return None
    return head or None


def _area_type_for(url: str, area: Optional[float]) -> str:
    if area is None:
        return "unknown"
    try:
        segment = urlsplit(url).path.split("/")[2].lower()
    except (ValueError, IndexError):
        return "unknown"
    return "plot" if segment in _PLOT_TYPES else "built"


def _text_of(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_entities.unescape(text)).strip()


def cards_in_email(body: Optional[str]) -> List[YaencontreCard]:
    """Every listing card in an alert email body, one per listing id.

    Pure -- no network, no clock, no database -- which is what lets the test
    read the real (token-redacted) alert of 2026-08-30 out of `tests/data/`.

    The parse anchors on structure, not on inline styles: all anchors for one
    listing share its URL, the title is the first of them with visible text,
    and the card's price and facts line sit between that anchor and the next
    listing's first anchor. Prices and areas go through the same Spanish
    number grammars every idealista email already goes through
    (`utils/idealista_extractors.py`).
    """
    text = body or ""
    anchors: List[Dict[str, Any]] = []
    for match in _ANCHOR.finditer(text):
        href = html_entities.unescape(match.group("href"))
        listing_id = listing_id_from_url(href)
        if listing_id is None:
            continue
        anchors.append(
            {
                "id": listing_id,
                "href": href,
                "inner": match.group("inner"),
                "start": match.start(),
                "end": match.end(),
            }
        )
    if not anchors:
        return []

    # First anchor per listing, in email order.
    order: List[int] = []
    first: Dict[int, int] = {}
    for i, a in enumerate(anchors):
        if a["id"] not in first:
            first[a["id"]] = i
            order.append(a["id"])

    cards: List[YaencontreCard] = []
    for pos, listing_id in enumerate(order):
        start_i = first[listing_id]
        next_start = (
            anchors[first[order[pos + 1]]]["start"]
            if pos + 1 < len(order)
            else len(text)
        )
        segment = text[anchors[start_i]["start"] : next_start]

        title = None
        for a in anchors[start_i:]:
            if a["id"] != listing_id:
                break
            candidate = _text_of(a["inner"])
            if candidate:
                title = candidate
                break

        flat = _text_of(segment)
        price = extract_price(flat)
        area = extract_area_m2(flat)
        url = normalize_url(anchors[start_i]["href"]) or anchors[start_i]["href"]

        card = YaencontreCard(
            url=url,
            listing_id=listing_id,
            title=title,
            price=price,
            area=area,
            area_type=_area_type_for(url, area),
            municipality=_municipality_from_title(title),
        )
        bedrooms = _BEDROOMS.search(flat)
        bathrooms = _BATHROOMS.search(flat)
        if bedrooms:
            card.attributes["bedrooms"] = int(bedrooms.group(1))
        if bathrooms:
            card.attributes["bathrooms"] = int(bathrooms.group(1))
        cards.append(card)
    return cards


def listing_urls_in_text(text: Optional[str]) -> List[str]:
    """Every yaencontre listing URL in a block of text, one per listing id.

    The cheap presence probe the email loop uses before paying for a full
    card parse; same contract as the fotocasa one.
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
