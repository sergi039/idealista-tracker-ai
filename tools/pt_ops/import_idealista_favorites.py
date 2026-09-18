#!/usr/bin/env python3
"""Import Idealista.pt favorites into Property rows (is_favorite=True).

Primary path: parse a saved HTML or captured XHR JSON dump from a logged-in
browser. Secondary: HTTP GET the favorites page with an owner-supplied Cookie
header file (never printed). Dry-run by default; pass ``--apply`` to write.

Does not defeat DataDome. Does not spend Google money. Does not send Telegram.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

# docker exec without PYTHONPATH: tools/pt_ops/ → repo root (/app).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("import_idealista_favorites")

SOURCE_PREFIX = "idealista:favorite:"
DEFAULT_COOKIES_PATH = (
    "/Users/baf/Backups/idealista-tracker/private/idealista-favorites.cookies"
)
DEFAULT_FAVORITES_URL = "https://www.idealista.pt/en/utilizador/favoritos/"
CANONICAL_HOST = "www.idealista.pt"

_ARTICLE_RE = re.compile(
    r"<article\b[^>]*\bclass=[\"'][^\"']*\bitem\b[^\"']*[\"'][^>]*>(.*?)</article>",
    re.IGNORECASE | re.DOTALL,
)
_ITEM_LINK_RE = re.compile(
    r"<a\b[^>]*\bclass=[\"'][^\"']*\bitem-link\b[^\"']*[\"'][^>]*"
    r"href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>"
    r"|"
    r"<a\b[^>]*href=[\"'](?P<href2>[^\"']+)[\"'][^>]*"
    r"\bclass=[\"'][^\"']*\bitem-link\b[^\"']*[\"'][^>]*>(?P<title2>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_ITEM_PRICE_RE = re.compile(
    r"<(?:span|div)\b[^>]*\bclass=[\"'][^\"']*\bitem-price\b[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?)</(?:span|div)>",
    re.IGNORECASE | re.DOTALL,
)
_PRICE_ROW_RE = re.compile(
    r"<(?:div|span)\b[^>]*\bclass=[\"'][^\"']*\bprice-row\b[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?)</(?:div|span)>",
    re.IGNORECASE | re.DOTALL,
)
_DETAIL_CHAR_RE = re.compile(
    r"<div\b[^>]*\bclass=[\"'][^\"']*\bitem-detail(?:-char)?\b[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_RENTAL_RE = re.compile(
    r"\b(alquiler|en\s+alquiler|for\s+rent|to\s+rent|rental|"
    r"arrendar|arrendamento|alugar|aluguer|€\s*/\s*(?:mês|mes|month))\b",
    re.IGNORECASE,
)
_BLOCKED_MARKERS = (
    "datadome",
    "geo.captcha",
    "captcha-delivery",
    "please enable js",
    "access denied",
    "request unsuccessful",
)
_LAZY_IMG = re.compile(
    r"<(?:img|source)\b[^>]*?\b(?:data-ondemand-img|data-src|data-lazy(?:-src)?)"
    r"\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_PHOTO_HOST = re.compile(r"(^|\.)img\d*\.idealista\.(?:pt|com)$", re.IGNORECASE)
_PT_LISTING_RE = re.compile(
    r"https?://(?:www\.)?idealista\.pt/(?:[a-z]{2}/)?imovel/(\d+)",
    re.IGNORECASE,
)
_PT_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?imovel/(\d+)/?", re.IGNORECASE)


@dataclass(frozen=True)
class FavoriteListing:
    listing_id: int
    url: str
    title: Optional[str] = None
    price: Optional[float] = None
    previous_price: Optional[float] = None
    area: Optional[float] = None
    typology: Optional[str] = None
    bedrooms: Optional[int] = None
    is_rental: bool = False
    raw_details: Optional[str] = None
    photos: dict = field(default_factory=lambda: {"items": [], "published": 0})

    def source_email_id(self) -> str:
        return f"{SOURCE_PREFIX}{self.listing_id}"


class DumpBlockedError(RuntimeError):
    """Idealista refused the fetch (DataDome / captcha / HTTP block)."""


class DumpParseError(RuntimeError):
    """Dump could not be interpreted as favorites HTML or JSON."""


def source_email_id_for(listing_id: int) -> str:
    return f"{SOURCE_PREFIX}{int(listing_id)}"


def canonical_listing_url(listing_id: int) -> str:
    return f"https://{CANONICAL_HOST}/imovel/{int(listing_id)}/"


def _strip_tags(text: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = re.sub(r"\s+", " ", plain)
    return plain.strip()


def _looks_blocked(status_code: Optional[int], body: str) -> Optional[str]:
    if status_code in (401, 403, 429):
        return f"http_{status_code}"
    lowered = (body or "")[:8000].lower()
    for marker in _BLOCKED_MARKERS:
        if marker in lowered:
            return f"body:{marker}"
    return None


def resolve_cookies_path(explicit: Optional[str] = None) -> Path:
    raw = (
        explicit
        or os.environ.get("IDEALISTA_FAVORITES_COOKIES")
        or DEFAULT_COOKIES_PATH
    )
    return Path(raw).expanduser()


def load_cookie_header(path: Path) -> str:
    """Load a Cookie request header from a private file. Never log the value.

    Accepts either a raw ``Cookie:`` / bare cookie header line, or a Netscape
    cookie jar (tab-separated). Authorization headers are refused.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"cookies file not found: {path} "
            "(set IDEALISTA_FAVORITES_COOKIES or pass --cookies)"
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "cookies file mode is %03o (prefer 0600); continuing without printing contents",
            mode,
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    # Refuse dumping secrets into argv-style mistakes.
    if re.search(r"(?im)^(authorization|proxy-authorization)\s*:", text):
        raise ValueError(
            "cookies file must not contain Authorization headers; "
            "use a Cookie header or Netscape cookie jar only"
        )
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("cookies file is empty")

    netscape_rows: List[str] = []
    for line in lines:
        if line.startswith("#") and "HttpOnly_" not in line:
            continue
        # Netscape: domain \t flag \t path \t secure \t expiry \t name \t value
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                name, value = parts[5], parts[6]
                if name:
                    netscape_rows.append(f"{name}={value}")
                continue
        if line.lower().startswith("cookie:"):
            return line.split(":", 1)[1].strip()
        # Bare cookie header (possibly multi-line joined).
        if "=" in line and "\t" not in line:
            return "; ".join(
                ln.split(":", 1)[1].strip() if ln.lower().startswith("cookie:") else ln
                for ln in lines
                if not ln.startswith("#")
            )
    if netscape_rows:
        return "; ".join(netscape_rows)
    raise ValueError("could not parse cookies file as Cookie header or Netscape jar")


def _coerce_listing_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _pt_listing_id(text)


def _coerce_price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if number >= 0 else None
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return None
    from utils.idealista_extractors import extract_price

    return extract_price(text)


def _coerce_area(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if number > 0 else None
    from utils.idealista_extractors import extract_area_m2

    return extract_area_m2(str(value))


def _empty_photos() -> dict:
    return {"items": [], "published": 0}


def _pt_listing_id(url: str) -> Optional[int]:
    text = url or ""
    match = _PT_LISTING_RE.search(text) or _PT_PATH_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _imovel_urls(html: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for match in re.finditer(
        r"""(?:href|src)\s*=\s*["']([^"']*imovel/\d+[^"']*)["']""",
        html or "",
        re.IGNORECASE,
    ):
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        found.append(raw)
    return found


def _is_listing_photo(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        return False
    if not _PHOTO_HOST.search(host):
        return False
    if "pixel" in host or "logo" in path or "static" in path:
        return False
    if path.endswith(".gif") and "open" in path:
        return False
    return True


def _photos_from_urls(urls: Iterable[Any]) -> dict:
    """Keep public Idealista CDN photographs; drop credentials and chrome."""
    import html as html_lib

    from services.portal_photos import normalise_photo_url

    items: List[Dict[str, str]] = []
    seen = set()
    published = 0
    for raw in urls:
        if not isinstance(raw, str) or not raw.strip():
            continue
        published += 1
        cleaned = html_lib.unescape(raw.strip()).replace("&amp;", "&")
        url = normalise_photo_url(cleaned)
        if url is None or not _is_listing_photo(url) or url in seen:
            continue
        seen.add(url)
        items.append({"url": url})
    return {"items": items, "published": published}


def _merge_photos(*blocks: Any) -> dict:
    items: List[Dict[str, str]] = []
    seen = set()
    published = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        try:
            published += int(block.get("published") or 0)
        except (TypeError, ValueError):
            pass
        for item in block.get("items") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url or url in seen:
                continue
            seen.add(url)
            items.append({"url": url})
    return {"items": items, "published": published}


def _photos_from_card_html(card_html: str) -> dict:
    import html as html_lib

    srcs = _IMG_SRC.findall(card_html or "")
    lazy = [
        html_lib.unescape(src).replace("&amp;", "&")
        for src in _LAZY_IMG.findall(card_html or "")
    ]
    return _photos_from_urls([*srcs, *lazy])


def _looks_like_photo_url(value: str) -> bool:
    lowered = (value or "").lower()
    if "idealista." not in lowered:
        return False
    return (
        "/blur/" in lowered
        or "id.pro." in lowered
        or "image.master" in lowered
        or lowered.endswith((".jpg", ".jpeg", ".webp", ".png"))
    )


def _collect_json_photo_urls(obj: Any) -> List[str]:
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if _looks_like_photo_url(node):
                found.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(obj)
    return found


def _has_photo_items(prop: Any) -> bool:
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return False
    block = enrichment.get("import")
    if not isinstance(block, dict):
        return False
    photos = block.get("photos")
    if isinstance(photos, dict):
        items = photos.get("items") or []
        return bool(items)
    if isinstance(photos, list):
        return bool(photos)
    return False


def _fill_photos_if_missing(prop: Any, listing: FavoriteListing) -> bool:
    """Backfill listing photos when the row still has none.

    Does not overwrite mail-captured photographs. Credential-bearing URLs
    are dropped by the same guard the mail parser uses.
    """
    incoming = _photos_from_urls(
        item.get("url")
        for item in (listing.photos or {}).get("items") or []
        if isinstance(item, dict)
    )
    items = incoming.get("items") or []
    if not items:
        return False
    if _has_photo_items(prop):
        return False
    enrichment = dict(getattr(prop, "enrichment", None) or {})
    import_block = dict(enrichment.get("import") or {})
    import_block["photos"] = {
        "items": list(items),
        "published": incoming.get("published") or len(items),
    }
    enrichment["import"] = import_block
    prop.enrichment = enrichment
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(prop, "enrichment")
    except Exception:
        pass
    return True


def _absolute_pt_url(href: str) -> Optional[str]:
    raw = (href or "").strip()
    if not raw or raw.startswith(("javascript:", "data:", "#")):
        return None
    absolute = urljoin(f"https://{CANONICAL_HOST}/", raw)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").lower()
    if host not in {"idealista.pt", "www.idealista.pt"}:
        return None
    listing_id = _pt_listing_id(absolute)
    if listing_id is None:
        return None
    return canonical_listing_url(listing_id)


def _listing_from_card_html(card_html: str) -> Optional[FavoriteListing]:
    from utils.idealista_extractors import (
        extract_area_m2,
        extract_price,
        extract_property_attributes,
        extract_listing_title,
    )

    href = None
    title = None
    link = _ITEM_LINK_RE.search(card_html)
    if link:
        href = link.group("href") or link.group("href2")
        title = _strip_tags(link.group("title") or link.group("title2") or "")
    if not href:
        urls = _imovel_urls(card_html)
        href = urls[0] if urls else None
    if not href:
        return None
    absolute = _absolute_pt_url(href)
    if absolute is None:
        return None
    listing_id = _pt_listing_id(absolute)
    if listing_id is None:
        return None
    if not title:
        title = extract_listing_title(card_html, idealista_property_id=listing_id)

    price_body = None
    price_match = _ITEM_PRICE_RE.search(card_html) or _PRICE_ROW_RE.search(card_html)
    if price_match:
        price_body = _strip_tags(price_match.group("body"))
    price = extract_price(price_body or card_html)

    details_parts: List[str] = []
    for match in _DETAIL_CHAR_RE.finditer(card_html):
        details_parts.append(_strip_tags(match.group("body")))
    details = " · ".join(p for p in details_parts if p) or None
    attrs_text = " ".join(p for p in (details, card_html) if p)
    attrs = extract_property_attributes(attrs_text) or {}
    tipo_match = re.search(r"\bT(\d{1,2})\b", attrs_text or "", re.IGNORECASE)
    if tipo_match and not attrs.get("typology"):
        attrs["typology"] = f"T{int(tipo_match.group(1))}"
        if attrs.get("bedrooms") is None:
            attrs["bedrooms"] = int(tipo_match.group(1))
    area = extract_area_m2(details or "") or extract_area_m2(card_html)
    rental_text = f"{title or ''}\n{details or ''}\n{price_body or ''}"
    return FavoriteListing(
        listing_id=listing_id,
        url=absolute,
        title=title or None,
        price=price,
        area=area,
        typology=attrs.get("typology"),
        bedrooms=attrs.get("bedrooms"),
        is_rental=bool(_RENTAL_RE.search(rental_text)),
        raw_details=details,
        photos=_photos_from_card_html(card_html),
    )


def parse_favorites_html(html: str) -> List[FavoriteListing]:
    """Parse Idealista favorites / listing-grid HTML into FavoriteListing rows."""
    if not html or not html.strip():
        return []
    blocked = _looks_blocked(None, html)
    if blocked:
        raise DumpBlockedError(
            f"dump looks blocked ({blocked}); save HTML/JSON from a logged-in "
            "browser instead of retrying against DataDome"
        )

    cards = _ARTICLE_RE.findall(html)
    found: Dict[int, FavoriteListing] = {}
    for card in cards:
        listing = _listing_from_card_html(card)
        if listing is None:
            continue
        found.setdefault(listing.listing_id, listing)

    if found:
        return [found[k] for k in sorted(found)]

    # Fallback: any idealista.pt /imovel/<id>/ anchors on the page.
    from utils.idealista_extractors import (
        extract_listing_title,
        extract_price,
        extract_area_m2,
        extract_property_attributes,
    )

    page_urls = _imovel_urls(html)
    for url in page_urls:
        absolute = _absolute_pt_url(url)
        if absolute is None:
            continue
        listing_id = _pt_listing_id(absolute)
        if listing_id is None or listing_id in found:
            continue
        title = extract_listing_title(html, idealista_property_id=listing_id)
        # Price/area from whole page is ambiguous for multi-card pages; leave
        # None unless a single listing is present.
        price = extract_price(html) if len(page_urls) == 1 else None
        area = extract_area_m2(html) if len(page_urls) == 1 else None
        attrs = extract_property_attributes(title or "") if title else {}
        found[listing_id] = FavoriteListing(
            listing_id=listing_id,
            url=absolute,
            title=title,
            price=price,
            area=area,
            typology=attrs.get("typology"),
            bedrooms=attrs.get("bedrooms"),
            is_rental=bool(_RENTAL_RE.search(title or "")),
            photos=_photos_from_card_html(html)
            if len(page_urls) == 1
            else _empty_photos(),
        )
    return [found[k] for k in sorted(found)]


def _walk_json_ads(payload: Any) -> List[Dict[str, Any]]:
    """Collect ad-like dicts from common Idealista XHR envelopes."""
    out: List[Dict[str, Any]] = []

    def consider(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                consider(item)
            return
        if not isinstance(obj, dict):
            return
        keys = {str(k).lower() for k in obj}
        id_like = keys & {
            "adid",
            "ad_id",
            "id",
            "propertycode",
            "property_code",
            "listing_id",
            "listingid",
        }
        url_like = any(
            isinstance(obj.get(k), str)
            and ("/imovel/" in obj.get(k) or "/inmueble/" in obj.get(k))
            for k in obj
            if isinstance(k, str)
        )
        if id_like or url_like:
            out.append(obj)
            return
        for key in (
            "ads",
            "items",
            "listings",
            "favorites",
            "favourites",
            "list",
            "results",
            "data",
            "jsonResponse",
            "json_response",
        ):
            if key in obj:
                consider(obj[key])
        # Generic walk one level for envelopes we did not name.
        if not any(
            k in obj for k in ("ads", "items", "listings", "data", "jsonResponse")
        ):
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    consider(value)

    consider(payload)
    return out


def _listing_from_json_obj(obj: Dict[str, Any]) -> Optional[FavoriteListing]:
    listing_id = None
    for key in (
        "adId",
        "ad_id",
        "propertyCode",
        "property_code",
        "listing_id",
        "listingId",
        "id",
    ):
        listing_id = _coerce_listing_id(obj.get(key))
        if listing_id is not None:
            break
    url_raw = None
    for key in ("url", "detailUrl", "detail_url", "link", "href"):
        if obj.get(key):
            url_raw = str(obj.get(key))
            break
    if listing_id is None and url_raw:
        listing_id = _coerce_listing_id(url_raw)
    if listing_id is None:
        return None
    url = _absolute_pt_url(url_raw) if url_raw else canonical_listing_url(listing_id)
    if url is None:
        url = canonical_listing_url(listing_id)

    title = None
    for key in ("title", "address", "headline", "name", "subject"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    price = None
    for key in (
        "price",
        "priceValue",
        "price_value",
        "askingPrice",
        "priceAmount",
        "price_hint",
    ):
        price = _coerce_price(obj.get(key))
        if price is not None:
            break
    if price is None:
        for key in ("priceText", "price_text", "formattedPrice"):
            price = _coerce_price(obj.get(key))
            if price is not None:
                break

    previous_price = None
    for key in (
        "previous_price",
        "previousPrice",
        "price_old",
        "oldPrice",
        "old_price",
    ):
        previous_price = _coerce_price(obj.get(key))
        if previous_price is not None:
            break

    area = None
    detail = obj.get("detail") if isinstance(obj.get("detail"), dict) else {}
    features = obj.get("features") if isinstance(obj.get("features"), dict) else {}
    for source in (obj, detail, features):
        for key in (
            "size",
            "area",
            "constructedArea",
            "constructed_area",
            "floorArea",
            "surface",
        ):
            area = _coerce_area(source.get(key))
            if area is not None:
                break
        if area is not None:
            break

    bedrooms = None
    typology = None
    for source in (obj, detail, features):
        for key in ("roomNumber", "rooms", "bedrooms", "bedroomNumber"):
            raw = source.get(key)
            if raw is None or raw == "":
                continue
            try:
                bedrooms = int(raw)
                break
            except (TypeError, ValueError):
                continue
        if bedrooms is not None:
            break
    for source in (obj, detail, features):
        raw = source.get("typology") or source.get("typologyName")
        if isinstance(raw, str) and re.search(r"\bT\d", raw, re.IGNORECASE):
            typology = re.search(r"\bT\d{1,2}(?:\+\d+)?\b", raw, re.IGNORECASE)
            typology = typology.group().upper() if typology else raw.strip()
            break
    if typology is None and bedrooms is not None:
        typology = f"T{bedrooms}"

    rental_bits = " ".join(
        str(obj.get(k) or "")
        for k in ("operation", "operationType", "dealType", "title", "priceText")
    )
    is_rental = bool(_RENTAL_RE.search(rental_bits)) or str(
        obj.get("operation") or obj.get("operationType") or ""
    ).lower() in {"rent", "rental", "2", "arrendar", "aluguel"}

    return FavoriteListing(
        listing_id=listing_id,
        url=url,
        title=title,
        price=price,
        previous_price=previous_price,
        area=area,
        typology=typology,
        bedrooms=bedrooms,
        is_rental=is_rental,
        photos=_photos_from_urls(_collect_json_photo_urls(obj)),
    )


def parse_favorites_json(payload: Any) -> List[FavoriteListing]:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        blocked = _looks_blocked(None, text)
        if blocked:
            raise DumpBlockedError(
                f"dump looks blocked ({blocked}); capture XHR JSON from a "
                "logged-in browser instead of retrying against DataDome"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DumpParseError(f"invalid JSON dump: {exc}") from exc

    found: Dict[int, FavoriteListing] = {}
    for obj in _walk_json_ads(payload):
        listing = _listing_from_json_obj(obj)
        if listing is None:
            continue
        found.setdefault(listing.listing_id, listing)
    return [found[k] for k in sorted(found)]


def sniff_and_parse(raw: str, *, hint: Optional[str] = None) -> List[FavoriteListing]:
    """Parse a dump. ``hint`` is ``html`` / ``json`` / None (sniff)."""
    text = raw or ""
    kind = (hint or "").lower().strip() or None
    stripped = text.lstrip()
    if kind == "json" or (kind is None and stripped[:1] in "{["):
        return parse_favorites_json(text)
    if kind == "html" or kind is None:
        listings = parse_favorites_html(text)
        if listings or kind == "html":
            return listings
    # Last resort: try JSON even when sniff said HTML.
    if stripped[:1] in "{[":
        return parse_favorites_json(text)
    return listings


def load_dump_file(path: Path) -> List[FavoriteListing]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    hint = (
        "json" if suffix == ".json" else "html" if suffix in {".html", ".htm"} else None
    )
    listings = sniff_and_parse(text, hint=hint)
    if not listings:
        raise DumpParseError(f"no Idealista.pt /imovel/<id>/ favorites found in {path}")
    return listings


def fetch_favorites_html(
    *,
    url: str = DEFAULT_FAVORITES_URL,
    cookies_path: Optional[Path] = None,
    timeout: float = 30.0,
) -> str:
    """Secondary path: GET favorites with owner Cookie header. Never logs secrets."""
    import requests

    from utils.http import HTTP_USER_AGENT

    path = cookies_path or resolve_cookies_path()
    cookie_header = load_cookie_header(path)
    logger.info(
        "fetching favorites page (cookies from %s, %s bytes on disk)",
        path,
        path.stat().st_size,
    )
    response = requests.get(
        url,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-PT,en;q=0.9,pt;q=0.8",
            "Cookie": cookie_header,
        },
        timeout=timeout,
        allow_redirects=True,
    )
    body = response.text or ""
    blocked = _looks_blocked(response.status_code, body)
    if blocked:
        raise DumpBlockedError(
            f"Idealista refused the fetch ({blocked}). Do not retry into "
            "DataDome — Save As HTML or capture the favorites XHR JSON from a "
            "logged-in browser and pass --dump."
        )
    if response.status_code >= 400:
        raise DumpBlockedError(
            f"favorites fetch HTTP {response.status_code}; prefer a saved dump"
        )
    return body


def _default_profile_id() -> Optional[int]:
    from models import SearchProfile

    active = (
        SearchProfile.query.filter_by(is_active=True, is_hidden=False)
        .order_by(SearchProfile.is_default.asc(), SearchProfile.id.asc())
        .first()
    )
    return active.id if active else None


def plan_actions(
    listings: Sequence[FavoriteListing],
    *,
    existing_by_listing_id: Dict[int, Any],
) -> List[Dict[str, Any]]:
    """Pure plan: create / mark_favorite / already_favorite per listing."""
    plans: List[Dict[str, Any]] = []
    for listing in listings:
        existing = existing_by_listing_id.get(listing.listing_id)
        base = {
            "listing_id": listing.listing_id,
            "url": listing.url,
            "title": listing.title,
            "price": listing.price,
            "area": listing.area,
            "typology": listing.typology,
            "bedrooms": listing.bedrooms,
            "is_rental": listing.is_rental,
            "source_email_id": listing.source_email_id(),
            "photos": len((listing.photos or {}).get("items") or []),
        }
        if existing is None:
            plans.append({**base, "action": "create"})
            continue
        prop_id = getattr(existing, "id", None)
        already = bool(getattr(existing, "is_favorite", False))
        if already:
            plans.append(
                {
                    **base,
                    "action": "already_favorite",
                    "property_id": prop_id,
                }
            )
        else:
            plans.append(
                {
                    **base,
                    "action": "mark_favorite",
                    "property_id": prop_id,
                }
            )
    return plans


def _fill_price_if_missing(prop, listing: FavoriteListing) -> None:
    """Backfill asking/previous price on an existing row when still empty."""
    if prop.price is None and listing.price is not None:
        prop.price = listing.price
    if prop.previous_price is None and listing.previous_price is not None:
        prop.previous_price = listing.previous_price
        if prop.price is not None:
            prop.price_change_amount = float(prop.price) - float(listing.previous_price)
    if prop.area is None and listing.area is not None:
        prop.area = listing.area
        prop.area_type = "built"


def _create_property_from_listing(
    listing: FavoriteListing, *, profile_id: Optional[int]
):
    from sqlalchemy import null

    from app import db
    from models import Property, SearchProfile
    from services.property_classification_service import PropertyClassificationService
    from services.search_profile_service import SearchProfileService
    from utils.idealista_extractors import extract_municipality_from_title

    profile = db.session.get(SearchProfile, profile_id) if profile_id else None
    canonical = SearchProfileService.canonical_profile(profile)
    rules = SearchProfileService.get_classification_rules(canonical or profile)
    category, subtype = PropertyClassificationService.classify_sources(
        listing.title, listing.title or "", listing.raw_details or "", rules or []
    )

    prop = Property()
    prop.source_email_id = listing.source_email_id()
    prop.idealista_property_id = listing.listing_id
    prop.title = listing.title
    prop.url = listing.url
    prop.deal_type = "rent" if listing.is_rental else "sale"
    prop.price = listing.price
    if listing.previous_price is not None:
        prop.previous_price = listing.previous_price
        if listing.price is not None:
            prop.price_change_amount = float(listing.price) - float(
                listing.previous_price
            )
    prop.area = listing.area
    prop.area_type = "built" if listing.area is not None else "unknown"
    prop.municipality = extract_municipality_from_title(listing.title)
    prop.search_profile_id = canonical.id if canonical else profile_id
    prop.property_category = category
    prop.property_subtype = subtype
    PropertyClassificationService.reconcile_area_type(prop)
    attrs: Dict[str, Any] = {}
    if listing.typology is not None:
        attrs["typology"] = listing.typology
    if listing.bedrooms is not None:
        attrs["bedrooms"] = listing.bedrooms
    prop.attributes = attrs or None
    prop.is_favorite = True
    prop.listing_status_source = null()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prop.email_date = now
    if listing.price is not None:
        prop.price_changed_date = now
    import_block: Dict[str, Any] = {
        "source": "idealista",
        "method": "favorites_dump",
        "listing_id": listing.listing_id,
        "imported_at": now.isoformat(),
        "portal": "idealista.pt",
        "previous_price": listing.previous_price,
    }
    photos = _photos_from_urls(
        item.get("url")
        for item in (listing.photos or {}).get("items") or []
        if isinstance(item, dict)
    )
    if photos.get("items"):
        import_block["photos"] = photos
    prop.enrichment = {"import": import_block}
    db.session.add(prop)
    db.session.flush()
    return prop


def apply_plans(
    listings: Sequence[FavoriteListing],
    plans: Sequence[Dict[str, Any]],
    *,
    profile_id: Optional[int],
) -> List[Dict[str, Any]]:
    from app import db
    from models import Property

    try:
        from services.pt_mail_service import import_lock
    except ImportError:
        from contextlib import nullcontext as import_lock

    by_id = {listing.listing_id: listing for listing in listings}
    results: List[Dict[str, Any]] = []
    with import_lock():
        for plan in plans:
            action = plan.get("action")
            listing_id = int(plan["listing_id"])
            if action == "create":
                exists = Property.query.filter_by(
                    idealista_property_id=listing_id
                ).first()
                if exists is not None:
                    listing = by_id[listing_id]
                    _fill_price_if_missing(exists, listing)
                    _fill_photos_if_missing(exists, listing)
                    if not exists.is_favorite:
                        exists.is_favorite = True
                        results.append(
                            {
                                "listing_id": listing_id,
                                "action": "mark_favorite",
                                "property_id": exists.id,
                            }
                        )
                    else:
                        results.append(
                            {
                                "listing_id": listing_id,
                                "action": "already_favorite",
                                "property_id": exists.id,
                            }
                        )
                    continue
                collision = Property.query.filter_by(
                    source_email_id=source_email_id_for(listing_id)
                ).first()
                if collision is not None:
                    listing = by_id[listing_id]
                    _fill_price_if_missing(collision, listing)
                    _fill_photos_if_missing(collision, listing)
                    was_favorite = bool(collision.is_favorite)
                    collision.is_favorite = True
                    results.append(
                        {
                            "listing_id": listing_id,
                            "action": (
                                "already_favorite" if was_favorite else "mark_favorite"
                            ),
                            "property_id": collision.id,
                            "note": "source_email_id_existed",
                        }
                    )
                    continue
                listing = by_id[listing_id]
                prop = _create_property_from_listing(listing, profile_id=profile_id)
                results.append(
                    {
                        "listing_id": listing_id,
                        "action": "create",
                        "property_id": prop.id,
                        "source_email_id": prop.source_email_id,
                        "is_favorite": True,
                    }
                )
            elif action == "mark_favorite":
                prop = db.session.get(Property, plan.get("property_id"))
                if prop is None:
                    prop = Property.query.filter_by(
                        idealista_property_id=listing_id
                    ).first()
                if prop is None:
                    results.append(
                        {
                            "listing_id": listing_id,
                            "action": "missing",
                            "reason": "property_gone",
                        }
                    )
                    continue
                prop.is_favorite = True
                listing = by_id.get(listing_id)
                if listing is not None:
                    _fill_price_if_missing(prop, listing)
                    _fill_photos_if_missing(prop, listing)
                results.append(
                    {
                        "listing_id": listing_id,
                        "action": "mark_favorite",
                        "property_id": prop.id,
                    }
                )
            else:
                listing = by_id.get(listing_id)
                prop = None
                if plan.get("property_id") is not None:
                    prop = db.session.get(Property, plan.get("property_id"))
                if prop is None:
                    prop = Property.query.filter_by(
                        idealista_property_id=listing_id
                    ).first()
                if listing is not None and prop is not None:
                    _fill_price_if_missing(prop, listing)
                    _fill_photos_if_missing(prop, listing)
                results.append(
                    {
                        "listing_id": listing_id,
                        "action": action,
                        "property_id": (
                            prop.id if prop is not None else plan.get("property_id")
                        ),
                    }
                )
        db.session.commit()
    return results


def _load_existing_map(listing_ids: Iterable[int]) -> Dict[int, Any]:
    from models import Property

    ids = sorted({int(x) for x in listing_ids})
    if not ids:
        return {}
    rows = Property.query.filter(Property.idealista_property_id.in_(ids)).all()
    return {
        int(row.idealista_property_id): row
        for row in rows
        if row.idealista_property_id is not None
    }


USAGE_EPILOG = """
Effective use (logged-in browser dump; never fight DataDome):

  1. Open https://www.idealista.pt/en/utilizador/favoritos/ while logged in.
     Paginate: /pagina-2, /pagina-3, … and merge, or save each page.
  2. Capture photos from article.item[data-element-id] (the whole card,
     including the gallery). closest('.item') / [class*="item"] is the
     title block and will yield photos=0.
  3. Preferred dumps (private path, not git):
       Save As → HTML, or XHR JSON, or
       /Users/baf/Backups/idealista-tracker/private/capture-favorites-console.js
       (needs listings[].photo_urls on img*.idealista.pt).
  4. Dry-run, then --apply. Re-run --apply to backfill photos; existing
     mail photographs are not overwritten. --fetch hits DataDome — skip it.

  docker cp dump.json idealista-pt-app:/tmp/favorites.json
  docker exec -u root idealista-pt-app chmod 644 /tmp/favorites.json
  docker exec -w /app -e PYTHONPATH=/app idealista-pt-app \\
    python tools/pt_ops/import_idealista_favorites.py --dump /tmp/favorites.json
  docker exec -w /app -e PYTHONPATH=/app idealista-pt-app \\
    python tools/pt_ops/import_idealista_favorites.py --dump /tmp/favorites.json --apply
""".strip()


def _photos_in_dump(listings: Sequence[FavoriteListing]) -> tuple[int, int]:
    with_photos = 0
    for listing in listings:
        items = (listing.photos or {}).get("items") or []
        if items:
            with_photos += 1
    return with_photos, len(listings)


def usage_hints(listings: Sequence[FavoriteListing]) -> List[str]:
    """Short operator hints from a parsed dump. Empty when nothing to say."""
    with_photos, total = _photos_in_dump(listings)
    if total == 0:
        return []
    missing = total - with_photos
    if missing == 0:
        return []
    if with_photos == 0:
        return [
            "HINT: 0 listings have photos. Recapture from article.item"
            "[data-element-id] (not closest .item), paginate, and include "
            "photo_urls / Save As HTML. --fetch will not get images."
        ]
    return [
        f"HINT: {missing}/{total} listings have no photos in this dump. "
        "Those rows will stay without photographs unless you recapture "
        "the missing cards (lazy gallery / other pages)."
    ]


def _print_report(
    *,
    apply: bool,
    source: str,
    listings: Sequence[FavoriteListing],
    plans: Sequence[Dict[str, Any]],
    results: Optional[Sequence[Dict[str, Any]]],
) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== import_idealista_favorites ({mode}) ===")
    print(f"source={source}")
    print(f"parsed={len(listings)}")
    with_photos, total = _photos_in_dump(listings)
    print(f"photos_in_dump={with_photos}/{total}")
    counts: Dict[str, int] = {}
    for plan in plans:
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    print(
        "plan: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        if counts
        else "plan: (empty)"
    )
    for plan in plans:
        print(
            f"  {plan['action'].upper()} listing={plan['listing_id']} "
            f"price={plan.get('price')} area={plan.get('area')} "
            f"photos={plan.get('photos') or 0} "
            f"title={plan.get('title')!r} url={plan.get('url')}"
            + (
                f" property_id={plan['property_id']}"
                if plan.get("property_id") is not None
                else ""
            )
        )
    if apply and results is not None:
        print(f"applied={len(results)}")
        for item in results:
            print(
                f"  DONE action={item.get('action')} listing={item.get('listing_id')} "
                f"property_id={item.get('property_id')}"
            )
    for hint in usage_hints(listings):
        print(hint)


def run(
    *,
    dump: Optional[str] = None,
    fetch: bool = False,
    cookies: Optional[str] = None,
    url: str = DEFAULT_FAVORITES_URL,
    apply: bool = False,
    profile_id: Optional[int] = None,
    parse_only: bool = False,
) -> int:
    if not dump and not fetch:
        raise SystemExit("provide --dump PATH (preferred) or --fetch with cookies")

    listings: List[FavoriteListing]
    source: str
    if dump:
        path = Path(dump).expanduser()
        listings = load_dump_file(path)
        source = f"dump:{path}"
    else:
        html = fetch_favorites_html(url=url, cookies_path=resolve_cookies_path(cookies))
        listings = sniff_and_parse(html, hint="html")
        source = f"fetch:{url}"
        if not listings:
            raise DumpParseError(
                "fetch succeeded but no /imovel/<id>/ cards parsed; "
                "save the page HTML manually and pass --dump"
            )

    if parse_only:
        _print_report(
            apply=False, source=source, listings=listings, plans=[], results=None
        )
        for listing in listings:
            print(json.dumps(asdict(listing), ensure_ascii=False))
        return 0

    from app import create_app
    from config import Config

    app = create_app()
    with app.app_context():
        if getattr(Config, "MARKET_COUNTRY", "ES") != "PT":
            raise SystemExit(
                f"refusing to run: MARKET_COUNTRY={Config.MARKET_COUNTRY!r} (need PT)"
            )
        existing = _load_existing_map(listing.listing_id for listing in listings)
        plans = plan_actions(listings, existing_by_listing_id=existing)
        results = None
        if apply:
            resolved_profile = (
                profile_id if profile_id is not None else _default_profile_id()
            )
            results = apply_plans(listings, plans, profile_id=resolved_profile)
        _print_report(
            apply=apply,
            source=source,
            listings=listings,
            plans=plans,
            results=results,
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import Idealista.pt favorites into Property (is_favorite=True). "
            "Prefer --dump of saved HTML/JSON from a logged-in browser. "
            "Dry-run unless --apply."
        ),
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dump",
        help=(
            "Saved favorites HTML or captured JSON (preferred). "
            "Must include listing photos (article gallery / photo_urls) "
            "or apply will create rows without photographs."
        ),
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Secondary: HTTP GET favorites using a private Cookie file.",
    )
    parser.add_argument(
        "--cookies",
        help=(
            "Cookie header / Netscape jar path "
            f"(default: IDEALISTA_FAVORITES_COOKIES or {DEFAULT_COOKIES_PATH})."
        ),
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_FAVORITES_URL,
        help=f"Favorites URL for --fetch (default: {DEFAULT_FAVORITES_URL}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write Property rows / set is_favorite (default is dry-run).",
    )
    parser.add_argument(
        "--profile-id",
        type=int,
        default=None,
        help="search_profiles.id for newly created rows (default: first visible).",
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="Parse dump/fetch and print listings as JSON; skip DB.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(
            dump=args.dump,
            fetch=bool(args.fetch),
            cookies=args.cookies,
            url=args.url,
            apply=bool(args.apply),
            profile_id=args.profile_id,
            parse_only=bool(args.parse_only),
        )
    except DumpBlockedError as exc:
        logger.error("%s", exc)
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    except (DumpParseError, FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
