"""What the portal published as photographs, captured on the way past (#498).

The owner writes review comments about photographs this application has never
shown them. Measured on production 2026-09-04: of 1893 rows, exactly one row's
`enrichment` and four rows' `attributes` so much as mention an image, while
`templates/property_detail.html` asserted *"No photos"* on every listing page
unconditionally -- an absence rendered as a measurement (#98), on the one datum
the owner was being asked to judge listings by.

The URLs are already in memory. `fotocasa_source.parse_listing` and
`milanuncios_source.parse_listing` `json.loads` a payload that carries them and
name neither key; `yaencontre_source.cards_in_email` holds each card's markup
and discards the `<img>` in it. So capture costs **no request, no money and no
migration** -- `enrichment` is a JSON column and the block already carries
`portal_accuracy` for exactly this reason: keep the portal's own datum verbatim
so nobody has to re-fetch to get it.

What this module does NOT do, stated because a guard presented as more than it
is would be worse than none. It does not put a photograph in front of the taste
model: measured against this repository's own code and confirmed by three
independent refuters (0 of 3 could break it), `services/subscription_transport.py`
sends six text fields and neither CLI argument list in `tools/ai_bridge.py` has
any image or attachment argument, so nothing here can be read by a model today.
It does not fetch, hotlink or validate any image -- whether these CDNs serve
these URLs to this application, and under what terms, is unmeasured. And it does
not cover idealista, which is 60% of the table and answers DataDome to this
machine.

Three rules are load bearing.

**A URL that is not a listing photograph must not be captured.** The
discriminator differs per portal and each one is measured, not assumed:
fotocasa serves listing photos from `/images/ads/` and the *agency logo* from
`/images/client/` on the same host; a yaencontre alert email carries 24 `<img>`
tags of which 13 are template chrome on `static-mail.yaencontre.com` and one is
a tracking pixel on `apicondor.yaencontre.com`, against the listing photos on
`media.yaencontre.com`; milanuncios' `ad.images` is the ad's own list and needs
no filter.

**A captured URL must never carry a credential.** That yaencontre tracking
pixel's query is `apikey=...` -- the fixture is committed token-redacted for
that reason. A URL with a secret in it would be persisted, rendered in an
`href`, and travel wherever the page travels, so `normalise_photo_url` refuses
one outright rather than trying to strip it.

**"The portal published none" and "nobody captured any" are different facts.**
The key is written whenever a payload was read, so an empty list is a
measurement and a missing key is an absence -- the distinction the badge on the
property page could not make, because it had no data at all to make it from.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlparse

# One home for "may this string reach an href": the scheme, host and length
# guard `services/dossier.py` already owns. A second copy is a second thing to
# get wrong, and this value ends up in exactly the same place.
from services.dossier import normalise_url

logger = logging.getLogger(__name__)

ENRICHMENT_KEY = "photos"

# A listing carries a dozen or so; the cap is a bound on a value read from an
# external document, not a product decision.
MAX_PHOTOS = 40

# Read states for the page. `not_captured` is the honest answer for every row
# ingested before this shipped, and for every idealista row.
STATE_CAPTURED = "captured"
STATE_NONE_PUBLISHED = "none_published"
STATE_NOT_CAPTURED = "not_captured"

# Query parameter names that make a URL a credential. Refused rather than
# stripped: a URL that needs a secret to work does not work without it, and
# storing a broken URL is worse than storing nothing.
_SECRET_QUERY_KEYS = (
    "apikey",
    "api_key",
    "token",
    "access_token",
    "auth",
    "signature",
    "sig",
    "password",
    "secret",
)

# fotocasa serves a listing's photographs and its agency's logo from ONE host,
# and the path segment is the only thing that separates them.
_FOTOCASA_LISTING_PATH = "/images/ads/"

# yaencontre's alert email is mostly chrome. Only this host carries the
# listing's own photographs.
_YAENCONTRE_PHOTO_HOST = "media.yaencontre.com"

_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _has_credential(url: str) -> bool:
    try:
        query = urlparse(url).query
    except ValueError:
        return True
    if not query:
        return False
    for key, _value in parse_qsl(query, keep_blank_values=True):
        if key.strip().lower() in _SECRET_QUERY_KEYS:
            return True
    return False


def normalise_photo_url(value: Any) -> Optional[str]:
    """The URL if it may be stored and linked to, else ``None``."""
    url = normalise_url(value)
    if url is None:
        return None
    if _has_credential(url):
        return None
    return url


def _entry(url: Optional[str], kind: Any = None) -> Optional[Dict[str, Any]]:
    if url is None:
        return None
    entry: Dict[str, Any] = {"url": url}
    # `type` verbatim where the portal states one. fotocasa's committed fixture
    # holds nine entries and every one says "image", which is a sample of one
    # listing -- a payload carrying a video or a floor plan has not been
    # observed here, so the word is recorded rather than assumed away.
    if isinstance(kind, str) and kind.strip():
        entry["type"] = kind.strip()[:40]
    return entry


def _collect(entries: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """De-duplicated by URL, in the portal's own order, bounded."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if entry is None or entry["url"] in seen:
            continue
        seen.add(entry["url"])
        out.append(entry)
        if len(out) >= MAX_PHOTOS:
            break
    return out


def from_fotocasa_payload(estate: Any, detail: Any) -> List[Dict[str, Any]]:
    """The listing's photographs from a fotocasa `__initial_props__` payload.

    Both blocks carry the same nine URLs in the same order in the committed
    fixture, differing only in the key name (`src` against `url`) and an extra
    `position`. `realEstate.multimedia` leads because it is the block
    `parse_listing` already binds; the detail block is the fallback for a
    payload shaped the other way.
    """
    items: List[Any] = []
    for block, key in ((estate, "multimedia"), (detail, "multimedias")):
        if isinstance(block, dict) and isinstance(block.get(key), list):
            items = block[key]
            break
    entries: List[Optional[Dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = normalise_photo_url(item.get("src") or item.get("url"))
        if url is None or _FOTOCASA_LISTING_PATH not in urlparse(url).path:
            # The agency logo lives at /images/client/ on the same host.
            continue
        entries.append(_entry(url, item.get("type")))
    return _collect(entries)


def from_milanuncios_ad(ad: Any) -> List[Dict[str, Any]]:
    """The ad's photographs from a milanuncios `__INITIAL_PROPS__` payload.

    `ad.images` is a flat list of strings -- the ad's own media and nothing
    else -- so there is no chrome to filter, only the shared URL guard.
    """
    images = ad.get("images") if isinstance(ad, dict) else None
    if not isinstance(images, list):
        return []
    return _collect([_entry(normalise_photo_url(item)) for item in images])


def from_yaencontre_card(markup: Any) -> List[Dict[str, Any]]:
    """The listing's photographs from one yaencontre alert-email card.

    The email is 24 `<img>` tags of which 13 are template chrome and one is a
    tracking pixel carrying an apikey; the host is what separates them.
    """
    if not isinstance(markup, str) or not markup:
        return []
    entries: List[Optional[Dict[str, Any]]] = []
    for src in _IMG_SRC.findall(markup):
        url = normalise_photo_url(src.replace("&amp;", "&"))
        if url is None:
            continue
        if urlparse(url).netloc.lower() != _YAENCONTRE_PHOTO_HOST:
            continue
        entries.append(_entry(url))
    return _collect(entries)


def read_photos(prop: Any) -> Dict[str, Any]:
    """What this row knows about its photographs. Pure -- no session, no query.

    Total and fail-closed, the `read_verdict` shape: a block nobody can read
    reads as a block nobody has written, and every stored URL is re-checked
    against the same guard the writer used, so a value edited straight into the
    database through `docker exec psql` -- a supported workflow here -- cannot
    reach an `href` on the strength of having once been written.
    """
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return {"state": STATE_NOT_CAPTURED, "photos": [], "count": 0}
    block = enrichment.get("import")
    if not isinstance(block, dict) or ENRICHMENT_KEY not in block:
        return {"state": STATE_NOT_CAPTURED, "photos": [], "count": 0}
    stored = block.get(ENRICHMENT_KEY)
    if not isinstance(stored, list):
        # Present but unreadable: nobody can say what the portal published.
        return {"state": STATE_NOT_CAPTURED, "photos": [], "count": 0}
    photos = _collect(
        [
            _entry(normalise_photo_url(item.get("url")), item.get("type"))
            if isinstance(item, dict)
            else None
            for item in stored
        ]
    )
    if not photos:
        # An empty list is a measurement: the payload was read and named none.
        # A list whose every entry the guard refuses is NOT -- somebody wrote
        # something here and it cannot be shown, which is not the portal
        # saying it has no photographs.
        state = STATE_NONE_PUBLISHED if not stored else STATE_NOT_CAPTURED
        return {"state": state, "photos": [], "count": 0}
    return {"state": STATE_CAPTURED, "photos": photos, "count": len(photos)}
