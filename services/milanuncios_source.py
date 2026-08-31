"""What a milanuncios listing page hands us, and how its alert mail names it.

Milanuncios is Adevinta, like fotocasa, and behaves like fotocasa where it
counts -- measured 2026-08-30 from both the laptop and the Mac mini: the
listing page answers **200 to the bare product token** in
`utils.http.HTTP_USER_AGENT`, no DataDome, no captcha, and carries the whole
listing as `window.__INITIAL_PROPS__ = JSON.parse("...")` -- id, title,
price, surface, **coordinates**, seller type (`private`/`professional`),
description. So the reading model is fotocasa's: the email supplies which
listings, the page supplies every field. If the portal ever starts refusing,
the answer is to stop fetching, not to dress up as a browser.

One thing fotocasa does not have: **the alert email carries no direct
listing links at all.** Every anchor is a SparkPost click tracker
(`sgt.milanuncios.com/ls/click?upn=u001.<opaque>`), and the target URL is
encrypted inside the token -- unrecoverable offline. So identity costs one
extra request per card: a GET of the tracker with redirects OFF, reading the
`Location` header, which the measured tracker answers as a 302 to
`https://www.milanuncios.com/ads/<slug>-<id>.htm`. Only the *card* trackers
are ever resolved -- an anchor whose inner `<img>` is served from the
portal's ad-image hosts -- because the same template wraps `Eliminar`,
`Desactívala` and `Dar de baja esta alerta` in identical trackers, and a
loop that resolved every tracker would be knocking on alert-management
doors it has no business near. Reading a card tracker's redirect never
loads the target page, so nothing here can trip an unsubscribe.

Two payload facts are traps, both measured on the real page committed under
`tests/data/milanuncios_listing_612329827.html`:

* **`sellType` distinguishes supply from demand.** Milanuncios carries "se
  busca" adverts; a `demand` ad is somebody looking to buy, not a property
  for sale, and storing one would put a phantom listing in the table this
  application cannot delete from. Anything but `supply` is refused.
* **`location.city.name` is the locality, with the municipality in
  parentheses.** "Los Quintanales (Mieres)" and the email's own "Piniella
  (Siero)" both name a village first; `utils/municipality_grouping.py` and
  the INE join want Mieres and Siero. The parenthesised name wins when
  present; a city with none ("Oviedo") is its own municipality.

The coordinate is stored `approximate`: nothing on the page declares parcel
precision, the one measured ad sits on a village, and `precise` is the label
that unlocks paid work (`services/coordinate_quality.py`).
"""

from __future__ import annotations

import html as html_entities
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

from utils.http import HTTP_USER_AGENT, RateGate, request_with_retries

logger = logging.getLogger(__name__)

SOURCE_NAME = "milanuncios"

# Courtesy pacing, fotocasa's number: nothing about milanuncios's tolerance
# has been measured, the digests carry one to three cards each, and a resolve
# plus a page read per card is tiny traffic at any polite interval.
MILANUNCIOS_MIN_INTERVAL_S = 3.0
MILANUNCIOS_GATE = RateGate(MILANUNCIOS_MIN_INTERVAL_S, name="milanuncios")

FETCH_TIMEOUT_S = 20

_HOSTS = ("milanuncios.com",)

# `/ads/<slug>-612329827.htm` from the tracker, or the canonical
# `/venta-de-chalets-en-.../<slug>-612329827.htm` it 301s to.
_LISTING_PATH = re.compile(r"-(\d{5,})\.htm$")

# The SparkPost click tracker. The path segment before `ls/click` varies by
# template -- a real alert of 2026-08-30 used `/uni/ls/click` and was refused
# whole by a pattern anchored on `/ls/click`, so the four ads it carried were
# never read. The host is milanuncios' own tracker and nothing else is served
# from it, so the prefix is what is loosened, not the host.
_TRACKER = re.compile(
    r"https?://sgt\.milanuncios\.com/(?:[A-Za-z0-9_-]+/)*ls/click\?[^\s\"'<>]+"
)

_ANCHOR = re.compile(
    r'<a\b[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<inner>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# The ad-photo hosts seen in the real digests; braze template art lives on
# cdn.braze.eu and marks a management button, never a card.
_AD_IMAGE_HOSTS = ("images.milanuncios.com", "images-re.milanuncios.com")

# The template's own word for "this anchor leads to the ad", carried on the
# card's opening tag. It is read beside the photo host because an ad with no
# photograph renders a braze placeholder instead of an `images*.milanuncios`
# one, and the whole email was then refused as cardless -- losing every
# photo-less ad, which is exactly the shape a cheap private-seller plot
# arrives in. Measured against both committed digests, this attribute selects
# the identical anchor set the photo host does (3 in `..._solares.html`, 2 in
# `..._chalets.html`), so it widens recognition without moving any card that
# is found today.
_CARD_TITLE = re.compile(
    r'title="ver el resultado de la b(?:ú|&uacute;|u)squeda"', re.IGNORECASE
)

_INITIAL_PROPS = re.compile(
    r'window\.__INITIAL_PROPS__\s*=\s*JSON\.parse\("(?P<literal>(?:[^"\\]|\\.)*)"\)',
    re.DOTALL,
)

# Same vocabulary as services/fotocasa_source.py, read the same way.
REFUSAL_BLOCKED = "blocked"
REFUSAL_HTTP_ERROR = "http_error"
REFUSAL_TIMEOUT = "timeout"
REFUSAL_NOT_A_LISTING = "not_the_listing_page"
REFUSAL_NO_PAYLOAD = "no_payload"
REFUSAL_UNREADABLE = "unreadable_payload"
REFUSAL_NOT_SUPPLY = "not_a_supply_ad"

_PLOT_SLUG_TOKENS = ("terreno", "solar", "parcela", "finca")


def is_milanuncios_url(url: Optional[str]) -> bool:
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
    """The listing id a milanuncios ad URL names, or None."""
    if not is_milanuncios_url(url):
        return None
    try:
        path = urlsplit((url or "").strip()).path
    except ValueError:
        return None
    found = _LISTING_PATH.search(path)
    return int(found.group(1)) if found else None


def normalize_url(url: Optional[str]) -> Optional[str]:
    """The ad URL with its query string and fragment dropped."""
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


def card_tracker_urls(body: Optional[str]) -> List[str]:
    """The click-tracker URLs of the listing cards, in email order.

    A card is an anchor that either wraps an ad photo
    (`images*.milanuncios.com`) or carries the template's own
    "ver el resultado de la búsqueda" title; every other tracker in the
    template -- the alert-management buttons, the survey, the footer -- is
    left strictly alone. The "Ver más fotos" twin under each photo resolves
    to the same ad and carries neither mark, so each card still costs one
    resolve; identical hrefs are collapsed so a template that ever marked
    both cannot double the traffic.
    """
    urls: List[str] = []
    seen: set = set()
    for match in _ANCHOR.finditer(body or ""):
        href = html_entities.unescape(match.group("href"))
        if not _TRACKER.match(href):
            continue
        whole = match.group(0)
        opening_tag = whole[: whole.find(">") + 1]
        inner = match.group("inner")
        is_card = any(host in inner for host in _AD_IMAGE_HOSTS) or bool(
            _CARD_TITLE.search(opening_tag)
        )
        if is_card and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def resolve_tracker(
    url: str, session: Optional[requests.Session] = None
) -> Optional[str]:
    """The listing URL a card tracker redirects to, or None.

    Redirects are OFF: the `Location` header is the answer, and following it
    would load a page this function has no mandate to load. None means the
    tracker did not answer with a milanuncios ad URL -- expired token, an
    unexpected target, a non-redirect -- and the caller treats that as a
    transient refusal (the email is re-read next run).
    """
    http = session or requests.Session()
    try:
        response = request_with_retries(
            http.get,
            url,
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=False,
            logger=logger,
            gate=MILANUNCIOS_GATE,
        )
    except requests.RequestException:
        return None
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    target = response.headers.get("Location") or ""
    target = urljoin(url, target)
    if listing_id_from_url(target) is None:
        return None
    return target


def _positive_number(value: Any) -> Optional[float]:
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


def _municipality_of(city_name: Optional[str]) -> Optional[str]:
    """Mieres out of "Los Quintanales (Mieres)"; the name itself otherwise."""
    if not city_name:
        return None
    found = re.search(r"\(([^()]+)\)\s*$", city_name)
    if found:
        inner = found.group(1).strip()
        if inner:
            return inner
    return city_name.strip() or None


def _refusal_row(url: str, reason: str, listing_id: Optional[int]) -> Dict[str, Any]:
    return {
        "url": url,
        "listing_id": listing_id,
        "status": "refused",
        "reason": reason,
    }


def parse_listing(body: str, url: str) -> Dict[str, Any]:
    """Read a fetched milanuncios ad page into a portal row dict.

    Pure -- no network, no clock, no database -- and the dict speaks the same
    keys `services/fotocasa_import.preview_row` produces, because
    `fotocasa_import.build_property` is the one writer both portals share.
    """
    listing_id = listing_id_from_url(url)
    text = body or ""

    found = _INITIAL_PROPS.search(text)
    if not found:
        lowered = text[:20000].lower()
        if "captcha" in lowered or "datadome" in lowered:
            return _refusal_row(url, REFUSAL_BLOCKED, listing_id)
        return _refusal_row(url, REFUSAL_NO_PAYLOAD, listing_id)

    try:
        # Undo the JS string escaping, then read the JSON it protects.
        payload = json.loads(json.loads(f'"{found.group("literal")}"'))
    except (ValueError, TypeError):
        return _refusal_row(url, REFUSAL_UNREADABLE, listing_id)

    ad = payload.get("ad") if isinstance(payload, dict) else None
    if not isinstance(ad, dict):
        return _refusal_row(url, REFUSAL_UNREADABLE, listing_id)

    sell_type = _text(ad.get("sellType"))
    if sell_type != "supply":
        # A "demand" ad is somebody searching, not a property for sale.
        return _refusal_row(url, REFUSAL_NOT_SUPPLY, listing_id)

    payload_id = _positive_number(ad.get("id"))
    if payload_id is not None:
        listing_id = int(payload_id)

    price_block = ad.get("price")
    price_block = price_block if isinstance(price_block, dict) else {}
    cash = price_block.get("cashPrice")
    cash = cash if isinstance(cash, dict) else {}

    location = ad.get("location")
    location = location if isinstance(location, dict) else {}
    city = location.get("city")
    city = city if isinstance(city, dict) else {}
    province = location.get("province")
    province = province if isinstance(province, dict) else {}
    geo = location.get("geolocation")
    geo = geo if isinstance(geo, dict) else {}

    attributes_in = ad.get("attributes")
    attributes_in = attributes_in if isinstance(attributes_in, list) else []
    by_type: Dict[str, Any] = {}
    for item in attributes_in:
        if isinstance(item, dict) and item.get("type"):
            by_type[str(item["type"])] = item.get("value")

    slugs = " ".join(
        str(c.get("slug") or "")
        for c in (ad.get("categories") or [])
        if isinstance(c, dict)
    ).lower()
    area = _positive_number(by_type.get("squareMeters"))
    if area is None:
        area_type = "unknown"
    elif any(token in slugs for token in _PLOT_SLUG_TOKENS):
        area_type = "plot"
    else:
        area_type = "built"

    seller = ad.get("sellerType")
    seller = seller if isinstance(seller, dict) else {}
    author = ad.get("author")
    author = author if isinstance(author, dict) else {}

    row: Dict[str, Any] = {
        "url": normalize_url(url) or url,
        "listing_id": listing_id,
        "status": "new",
        "reason": None,
        "title": _text(ad.get("title")),
        "price": _positive_number(cash.get("value")),
        "area": area,
        "area_type": area_type,
        "deal_type": "rent" if "alquiler" in slugs else "sale",
        "municipality": _municipality_of(_text(city.get("name"))),
        "province": _text(province.get("name")),
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
        "description": _text(ad.get("description")),
        "building_type": _text((ad.get("category") or {}).get("name"))
        if isinstance(ad.get("category"), dict)
        else None,
        "agency": _text(author.get("userName")),
        "publisher_type": _text(seller.get("value")),
        "client_type_id": None,
        "published_at": _text(ad.get("publicationDate")),
        "attributes": {},
        "portal_accuracy": {},
        # The locality as the portal spelled it, kept beside the municipality
        # the parenthesis rule extracted, so the rule's basis is on the row.
        "locality": _text(city.get("name")),
    }
    bedrooms = _positive_number(by_type.get("bedrooms"))
    bathrooms = _positive_number(by_type.get("bathrooms"))
    if bedrooms is not None:
        row["attributes"]["bedrooms"] = int(bedrooms)
    if bathrooms is not None:
        row["attributes"]["bathrooms"] = int(bathrooms)
    return row


def fetch_listing(
    url: str, session: Optional[requests.Session] = None
) -> Dict[str, Any]:
    """Fetch one ad page and read it, redirects followed to the canonical URL.

    The gate is handed to the transport, never taken around it, so retries
    are paced too. A page whose final URL names a different ad -- or none --
    is `not_the_listing_page`: the advert is gone and the server said so.
    """
    asked_id = listing_id_from_url(url)
    if asked_id is None:
        return _refusal_row(url, REFUSAL_NOT_A_LISTING, None)

    http = session or requests.Session()
    try:
        response = request_with_retries(
            http.get,
            url,
            headers={
                "User-Agent": HTTP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
            logger=logger,
            gate=MILANUNCIOS_GATE,
        )
    except requests.Timeout:
        return _refusal_row(url, REFUSAL_TIMEOUT, asked_id)
    except requests.RequestException:
        return _refusal_row(url, REFUSAL_HTTP_ERROR, asked_id)

    if response.status_code in (403, 429):
        return _refusal_row(url, REFUSAL_BLOCKED, asked_id)
    if response.status_code in (404, 410):
        # The server answered: no such advert. Tomorrow's answer is the same.
        return _refusal_row(url, REFUSAL_NOT_A_LISTING, asked_id)
    if response.status_code != 200:
        return _refusal_row(url, REFUSAL_HTTP_ERROR, asked_id)

    served_id = listing_id_from_url(getattr(response, "url", "") or url)
    if served_id != asked_id:
        return _refusal_row(url, REFUSAL_NOT_A_LISTING, asked_id)

    return parse_listing(response.text or "", getattr(response, "url", "") or url)
