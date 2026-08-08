import html
import re
from typing import Any, Dict, List, Optional, Tuple


_PROPERTY_ID_RE = re.compile(r"/inmueble/(\d+)", re.IGNORECASE)

# Two mutually exclusive number "grammars": in the first, '.' groups thousands
# and ',' introduces 1-2 decimal digits (e.g. "1.234,56"); in the second the
# roles are swapped (e.g. "1,234.56"). Both are anchored with a
# lookbehind/lookahead so a match can never start or end in the middle of a
# longer digit/separator run (root cause of GH issue #21: unanchored patterns
# matched the trailing "000" fragment of "59.000 €" as its own, wrong "0.0"
# price), and each grammar requires internally consistent separator usage, so
# a genuinely mixed/invalid number like "1.234,567" (3 "decimal" digits under
# the dot-group grammar) matches neither grammar and is correctly rejected
# rather than silently truncated (PR #33 review follow-up finding).
_NUMBER_DOT_GROUP = r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?(?![\d.,])"
_NUMBER_COMMA_GROUP = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d.,])"
# Either grammar as one alternation with 4 capture groups: exactly one
# (dot_int, dot_dec) or (comma_int, comma_dec) pair is populated, depending on
# which grammar matched. Parse with _parse_number_groups().
_PRICE_NUMBER = rf"(?:{_NUMBER_DOT_GROUP}|{_NUMBER_COMMA_GROUP})"

# Price patterns (supports Spanish/English thousand separators, decimal
# endings, and plain digits with no separator at all, e.g. "59000 €").
_PRICE_PATTERNS = [
    rf"(?:Price|Precio):?\s*{_PRICE_NUMBER}\s*€",
    rf"{_PRICE_NUMBER}\s*€",
]

# Price change patterns: extract (old, new) from "from X€ to Y€" / "de X€ a Y€".
# Groups 1-4 are the old price (dot_int, dot_dec, comma_int, comma_dec);
# groups 5-8 are the new price, in the same layout.
_PRICE_CHANGE_PATTERNS = [
    rf"\bfrom\s+{_PRICE_NUMBER}\s*€\s+to\s+{_PRICE_NUMBER}\s*€",
    rf"\bde\s+{_PRICE_NUMBER}\s*€\s+a\s+{_PRICE_NUMBER}\s*€",
]

# Area patterns (m² / m2; no minimum size threshold -- this extractor covers
# every property type, from small apartments to large plots).
#
# Reuses the same anchored, dual-grammar grouped-number regex as prices
# (_PRICE_NUMBER above) because the underlying defect is identical (GH #22,
# same root cause as #21 for extract_price()): an unanchored
# `\d{1,3}(?:[.,]\d{3})*` pattern can match a trailing digit-group fragment of
# a longer number, e.g. matching "373" out of "1.373 m²" instead of the whole
# "1.373". Anchoring makes that fragment match structurally impossible.
_AREA_NUMBER = _PRICE_NUMBER
_AREA_PATTERNS = [
    rf"Superficie:?\s*{_AREA_NUMBER}\s*m[²2]",
    rf"{_AREA_NUMBER}\s*m[²2]",
]

# A parsed area of 0 (or negative) is never a real size -- reject it rather
# than returning a bogus sub-1m² value from a mis-scan.
_AREA_SANITY_FLOOR = 0.0

_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<text>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_CTA_TEXT_RE = re.compile(
    r"^\s*(?:"
    r"see\s+\d+\s+photos|ver\s+\d+\s+fotos|"
    r"contact|contactar|"
    r"see\s+all\s+listings.*|ver\s+todos\s+los\s+anuncios.*|"
    r"download\s+the\s+idealista\s+app|descarga\s+la\s+app\s+de\s+idealista|"
    r"stop\s+receiving.*|dejar\s+de\s+recibir.*"
    r")\s*$",
    re.IGNORECASE,
)

_TITLE_HINT_RE = re.compile(
    r"\b("
    # Housing
    r"piso|apartamento|apartament|apartment|flat|estudio|studio|loft|"
    r"ático|atico|penthouse|"
    r"dúplex|duplex|"
    r"casa|chalet|vivienda|house|villa|adosado|pareado|bungalow|"
    r"detached|semi[-\s]?detached|terraced|townhouse|"
    # Garage / storage
    r"garaje|garage|trastero|storage|parking|plaza\s+de\s+garaje|"
    # Commercial
    r"oficina|office|despacho|"
    r"local\s+comercial|commercial\s+premises|shop|retail|"
    r"nave|warehouse|industrial|almac[eé]n|almacen|"
    # Land
    r"terreno|parcela|plot|land|"
    r"suelo\s+(?:en\s+venta|urbanizable|rústico|rustico)|"
    r"solar\s+(?:urbano|en\s+venta)|"
    r"finca\s+(?:rústica|rustica|en\s+venta)|"
    # Building / developments
    r"edificio|building|bloque|"
    r"obra\s+nueva|promoción|promocion|new\s+development"
    r")\b",
    re.IGNORECASE,
)


def extract_idealista_property_id(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    match = _PROPERTY_ID_RE.search(url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def extract_url(text: str) -> Optional[str]:
    """Extract Idealista URL from text (prefer listing URL over logo/homepage)."""
    if not text:
        return None

    # Prefer property-specific URL (with /inmueble/<id>/)
    property_pattern = r'https?://www\.idealista\.com(?:/[a-z]{2})?/inmueble/\d+[^"\s]*'
    match = re.search(property_pattern, text, re.IGNORECASE)
    if match:
        return match.group(0).strip().rstrip("\"'")

    # Fallback: first idealista URL (skip logo links)
    generic_pattern = r"https?://www\.idealista\.com/[^\s]+"
    match = re.search(generic_pattern, text, re.IGNORECASE)
    if not match:
        return None

    url = match.group(0).strip().rstrip("\"'")
    if "logo" in url:
        return None
    if not url.startswith("http"):
        return None
    return url


def _parse_price_match(
    int_part: Optional[str], dec_part: Optional[str]
) -> Optional[float]:
    """Combine a group-separated integer part with an optional 1-2 digit
    decimal part into a float. `int_part` is expected to contain only digits
    and a single, consistent group separator (already enforced by whichever
    _NUMBER_*_GROUP grammar produced it)."""
    if int_part is None:
        return None
    digits = re.sub(r"[.,]", "", int_part)
    if not digits:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if dec_part:
        try:
            value += int(dec_part) / (10 ** len(dec_part))
        except ValueError:
            return None
    return value


def _parse_number_groups(groups: Tuple[Optional[str], ...]) -> Optional[float]:
    """Parse a (dot_int, dot_dec, comma_int, comma_dec) 4-tuple captured by
    _PRICE_NUMBER: exactly one grammar's (int, dec) pair is populated."""
    dot_int, dot_dec, comma_int, comma_dec = groups
    if dot_int is not None:
        return _parse_price_match(dot_int, dot_dec)
    if comma_int is not None:
        return _parse_price_match(comma_int, comma_dec)
    return None


def extract_price(text: str) -> Optional[float]:
    if not text:
        return None

    # Prefer the "new" price when this is a price-change email.
    old_price, new_price = extract_price_change(text)
    if new_price is not None:
        return new_price

    plain = _strip_html(text)
    matches: list[tuple[int, float]] = []
    seen: set[tuple[int, float]] = set()
    for pattern in _PRICE_PATTERNS:
        for m in re.finditer(pattern, plain, re.IGNORECASE):
            parsed = _parse_number_groups(m.groups())
            if parsed is None:
                continue
            key = (m.start(), parsed)
            if key in seen:
                continue
            seen.add(key)
            matches.append(key)

    if not matches:
        return None

    matches.sort(key=lambda x: x[0])
    # Price-change emails may include both old+new prices; returning the last match is safer.
    return matches[-1][1]


def extract_price_change(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Extract (old_price, new_price) from price-change emails."""
    if not text:
        return None, None

    # HTML formatting fallback: old price is struck through, new price follows.
    try:
        strike_re = re.compile(
            rf"(?is)(?:<s[^>]*>|<del[^>]*>|text-decoration\s*:\s*line-through[^>]*>)\s*{_PRICE_NUMBER}\s*€",
        )
        strike_match = strike_re.search(text)
        if strike_match:
            old_price = _parse_number_groups(strike_match.groups())
            tail_plain = _strip_html(text[strike_match.end() :])
            m = re.search(rf"{_PRICE_NUMBER}\s*€", tail_plain)
            if m:
                new_price = _parse_number_groups(m.groups())
                if old_price is not None and new_price is not None:
                    return old_price, new_price
    except Exception:
        pass

    plain = _strip_html(text)
    for pattern in _PRICE_CHANGE_PATTERNS:
        match = re.search(pattern, plain, re.IGNORECASE)
        if not match:
            continue
        old_price = _parse_number_groups(match.groups()[0:4])
        new_price = _parse_number_groups(match.groups()[4:8])
        if old_price is not None and new_price is not None:
            return old_price, new_price

    all_prices: list[tuple[int, float]] = []
    for m in re.finditer(rf"{_PRICE_NUMBER}\s*€", plain):
        parsed = _parse_number_groups(m.groups())
        if parsed is None:
            continue
        all_prices.append((m.start(), parsed))

    if len(all_prices) >= 2:
        all_prices.sort(key=lambda x: x[0])
        return all_prices[0][1], all_prices[-1][1]

    return None, None


def extract_area_m2(text: str) -> Optional[float]:
    if not text:
        return None
    plain = _strip_html(text)

    # Collect every match from every pattern (instead of first-pattern-wins)
    # and prefer the longest leftmost match: a longer match at the same
    # position captures the whole number rather than a trailing fragment of
    # it, and the leftmost position mirrors how the value appeared in the
    # original one-search-per-pattern code for ordinary single-area text.
    candidates: list[tuple[int, int, float]] = []
    for pattern in _AREA_PATTERNS:
        for m in re.finditer(pattern, plain, re.IGNORECASE):
            parsed = _parse_number_groups(m.groups())
            if parsed is None or parsed <= _AREA_SANITY_FLOOR:
                continue
            candidates.append((m.start(), -(m.end() - m.start()), parsed))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][2]


def _strip_html(value: str) -> str:
    """Best-effort HTML-to-text for small snippets."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _rank_title_candidate(text: str) -> Tuple[int, int]:
    """Return a stable score tuple for sorting title candidates."""
    t = (text or "").strip()
    if not t:
        return (0, 0)
    score = 0
    if "," in t:
        score += 10
    if re.search(r"\b(in|en)\b", t, re.IGNORECASE):
        score += 6
    if _TITLE_HINT_RE.search(t):
        score += 5
    score += min(len(t), 120) // 4
    return (score, len(t))


def extract_listing_title(
    text: str, idealista_property_id: Optional[int] = None
) -> Optional[str]:
    """Extract listing title (card headline) from an Idealista email body."""
    if not text:
        return None

    candidates: List[str] = []

    id_value = idealista_property_id
    if id_value is None:
        id_match = _PROPERTY_ID_RE.search(text)
        if id_match:
            try:
                id_value = int(id_match.group(1))
            except ValueError:
                id_value = None

    for match in _ANCHOR_RE.finditer(text):
        href = (match.group("href") or "").strip()
        raw_text = match.group("text") or ""
        if "/inmueble/" not in href:
            continue
        if id_value is not None and f"/inmueble/{id_value}" not in href:
            continue

        cleaned = _strip_html(raw_text)
        if not cleaned:
            continue
        if _CTA_TEXT_RE.match(cleaned):
            continue
        candidates.append(cleaned)

    # Fallback: title may appear in plain text without anchors (text/plain part).
    if not candidates:
        re_text = re.compile(
            r"(?:^|\n|\r)\s*(?P<title>[^<>\n\r]{10,160}?)\s*(?:€|m[²2]|$)",
            re.IGNORECASE,
        )
        for m in re_text.finditer(text):
            cleaned = _strip_html(m.group("title") or "")
            if not cleaned:
                continue
            if _CTA_TEXT_RE.match(cleaned):
                continue
            if _TITLE_HINT_RE.search(cleaned):
                candidates.append(cleaned)

    if not candidates:
        return None

    candidates = [c[:160] for c in candidates if c]
    candidates.sort(key=_rank_title_candidate, reverse=True)
    return candidates[0] if candidates else None


def extract_municipality_from_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None

    cleaned = " ".join(str(title).replace("\xa0", " ").split()).strip()
    if not cleaned:
        return None

    cleaned = re.sub(r"\s+\d[\d.,]*\s*€.*$", "", cleaned).strip()
    cleaned = re.sub(r"\s+\d[\d.,]*\s*m[²2].*$", "", cleaned).strip()

    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) >= 2:
        candidate = parts[-1]
    else:
        m = re.search(r"\b(?:in|en)\s+(.+)$", cleaned, re.IGNORECASE)
        candidate = (m.group(1).strip() if m else cleaned).strip()

    if len(candidate) > 80:
        return None
    return candidate or None


def extract_bedrooms(text: str) -> Optional[int]:
    if not text:
        return None
    patterns = [
        r"\b(\d{1,2})\s*(?:bed|beds|bedroom|bedrooms)\b",
        r"\b(\d{1,2})\s*(?:hab|habitaci[oó]n|habitaciones|dormitorio|dormitorios)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            continue
    return None


def extract_bathrooms(text: str) -> Optional[int]:
    if not text:
        return None
    patterns = [
        r"\b(\d{1,2})\s*(?:bath|baths|bathroom|bathrooms)\b",
        r"\b(\d{1,2})\s*(?:baño|baños)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            continue
    return None


def extract_property_attributes(text: str) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    beds = extract_bedrooms(text)
    baths = extract_bathrooms(text)
    if beds is not None:
        attrs["bedrooms"] = beds
    if baths is not None:
        attrs["bathrooms"] = baths
    return attrs
