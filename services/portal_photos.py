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

# Tokens that make a parameter NAME a credential, matched as substrings so that
# `apikey`, `x-amz-signature`, `sessionid` and `accessToken` are all caught by
# one entry. Refused rather than stripped: a URL that needs a secret to work
# does not work without it, and storing a broken URL is worse than nothing.
#
# This is a heuristic and cannot be complete -- a secret in the PATH
# (`/img/<token>/x.jpg`) is indistinguishable from a path segment, and a
# parameter named something nobody guessed is not caught. It is written down
# rather than hidden because the mitigation is a fact about the source, not
# about this list: these URLs come from a portal's PUBLIC listing payload and
# point at a public CDN, so a credential in one is an anomaly rather than the
# normal case. The list is the cheap catch for the anomaly, not a proof.
#
# Safe against the real data by construction: fotocasa's only parameter is
# `rule=original`, milanuncios' and yaencontre's photo URLs carry none, and
# `tests/test_portal_photos_captured.py` asserts that on the committed
# fixtures -- a tightening here that started refusing real photographs would
# go red rather than quietly capture nothing.
_SECRET_NAME_TOKENS = (
    "key",
    "token",
    "auth",
    "signature",
    "sig",
    "password",
    "passwd",
    "pwd",
    "secret",
    "session",
    "credential",
    "hmac",
    "jwt",
    "bearer",
)

# fotocasa serves a listing's photographs and its agency's logo from ONE host,
# and the path segment is the only thing that separates them.
_FOTOCASA_LISTING_PATH = "/images/ads/"

# yaencontre's alert email is mostly chrome. Only this host carries the
# listing's own photographs.
_YAENCONTRE_PHOTO_HOST = "media.yaencontre.com"

_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _names_a_secret(blob: str) -> bool:
    if not blob:
        return False
    for key, _value in parse_qsl(blob, keep_blank_values=True):
        lowered = key.strip().lower()
        if any(token in lowered for token in _SECRET_NAME_TOKENS):
            return True
    return False


def _has_credential(url: str) -> bool:
    """Whether this URL carries a secret anywhere it can be seen.

    Three places, and the first is the one that also matters for a reason that
    is not confidentiality at all. `https://media.yaencontre.com@evil.test/x.jpg`
    has a HOST of `evil.test` -- the trusted name is userinfo -- so a URL with
    an `@` in its authority is refused outright rather than parsed further: a
    photo from a portal has no business carrying credentials, and the same
    syntax is how a hostile string reads as a trusted one. The query is the
    obvious place. The FRAGMENT is the third: it never reaches the origin, but
    it is stored here and rendered into an href, so it travels wherever the
    page travels and lands in browser history.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    if "@" in parsed.netloc:
        return True
    return _names_a_secret(parsed.query) or _names_a_secret(parsed.fragment)


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


def _capture(entries: List[Optional[Dict[str, Any]]], published: int) -> Dict[str, Any]:
    """What was captured, and how many the payload NAMED.

    The second number is the whole point and it was missing: a payload naming
    eight photographs of which the guard refuses all eight leaves an empty
    list, and an empty list stored on its own is indistinguishable from a
    portal that published none -- so a refusal was about to be reported as a
    measurement, which is the #98 defect this module exists to avoid, one
    layer inside it. Found by the independent review of the first version and
    reproduced before it was believed.
    """
    seen = set()
    items: List[Dict[str, Any]] = []
    for entry in entries:
        if entry is None or entry["url"] in seen:
            continue
        seen.add(entry["url"])
        items.append(entry)
        if len(items) >= MAX_PHOTOS:
            break
    return {"items": items, "published": published}


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
            entries.append(None)
            continue
        url = normalise_photo_url(item.get("src") or item.get("url"))
        if url is None or _FOTOCASA_LISTING_PATH not in urlparse(url).path:
            # The agency logo lives at /images/client/ on the same host. On
            # the one fixture measured it sits OUTSIDE this block entirely
            # (`publisher.logo`, `agency.logo`, `promotionLogo`), so this
            # guard has never been observed to fire on real data — which is
            # why it is tested directly rather than through the fixture, where
            # its absence changed nothing and read as coverage.
            entries.append(None)
            continue
        entries.append(_entry(url, item.get("type")))
    return _capture(entries, len(items))


def from_milanuncios_ad(ad: Any) -> List[Dict[str, Any]]:
    """The ad's photographs from a milanuncios `__INITIAL_PROPS__` payload.

    `ad.images` is a flat list of strings -- the ad's own media and nothing
    else -- so there is no chrome to filter, only the shared URL guard.
    """
    images = ad.get("images") if isinstance(ad, dict) else None
    if not isinstance(images, list):
        return _capture([], 0)
    return _capture([_entry(normalise_photo_url(item)) for item in images], len(images))


def from_yaencontre_card(markup: Any) -> List[Dict[str, Any]]:
    """The listing's photographs from one yaencontre alert-email card.

    The email is 24 `<img>` tags of which 13 are template chrome and one is a
    tracking pixel carrying an apikey; the host is what separates them.
    """
    if not isinstance(markup, str) or not markup:
        return _capture([], 0)
    entries: List[Optional[Dict[str, Any]]] = []
    published = 0
    for src in _IMG_SRC.findall(markup):
        cleaned = src.replace("&amp;", "&")
        try:
            host = urlparse(cleaned).netloc.lower()
        except ValueError:
            host = ""
        # Only an image on the photo host was ever a candidate here; the mail
        # chrome and the tracking pixel are not photographs the portal
        # published, so they are not counted as refused ones either.
        if host != _YAENCONTRE_PHOTO_HOST:
            continue
        published += 1
        url = normalise_photo_url(cleaned)
        entries.append(_entry(url) if url is not None else None)
    return _capture(entries, published)


def read_photos(prop: Any) -> Dict[str, Any]:
    """What this row knows about its photographs. Pure -- no session, no query.

    Total and fail-closed, the `read_verdict` shape: a block nobody can read
    reads as a block nobody has written, and every stored URL is re-checked
    against the same guard the writer used, so a value edited straight into the
    database through `docker exec psql` -- a supported workflow here -- cannot
    reach an `href` on the strength of having once been written.

    Three states, and the distinction between the last two is the module's
    reason for existing: `none_published` is a MEASUREMENT (a payload was read
    and named no photographs) while `not_captured` is an absence (nobody
    looked, or what was there could not be stored). An empty list is only the
    first when the capture also says the payload named none -- otherwise
    somebody's eight refused URLs would be reported as a portal with no
    pictures.
    """
    absent = {"state": STATE_NOT_CAPTURED, "photos": [], "count": 0}
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return absent
    block = enrichment.get("import")
    if not isinstance(block, dict) or ENRICHMENT_KEY not in block:
        return absent
    stored = block.get(ENRICHMENT_KEY)
    if isinstance(stored, dict):
        raw = stored.get("items")
        published = stored.get("published")
    elif isinstance(stored, list):
        # A bare list: a block written by hand. Nothing here can say what the
        # payload named, so its own length is the only honest reading.
        raw = stored
        published = len(stored)
    else:
        return absent
    if not isinstance(raw, list):
        return absent
    if not isinstance(published, int) or isinstance(published, bool) or published < 0:
        return absent
    capture = _capture(
        [
            _entry(normalise_photo_url(item.get("url")), item.get("type"))
            if isinstance(item, dict)
            else None
            for item in raw
        ],
        published,
    )
    photos = capture["items"]
    if photos:
        return {"state": STATE_CAPTURED, "photos": photos, "count": len(photos)}
    # Nothing to show. Which of the two facts that is depends entirely on
    # whether the payload named anything in the first place.
    state = STATE_NONE_PUBLISHED if published == 0 else STATE_NOT_CAPTURED
    return {"state": state, "photos": [], "count": 0}
