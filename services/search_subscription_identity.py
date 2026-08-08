"""Identity of an Idealista saved search, taken from its own search URL.

A saved search used to be identified by the name parsed out of the alert
subject. That name is a *label*: the mail server folds it (#101), Idealista
rewords it, the owner renames it. Every alert email also links to the search
page itself, and that link encodes the subscription's filters, so it - not the
label - is what distinguishes one saved search from another (#102).

    https://www.idealista.com/en/areas/venta-terrenos/con-precio-hasta_150000,
        metros-cuadrados-mas-de_100,...,publicado_ultimo-mes/?shape=((u}ygG~...

This module turns such a link into a stable fingerprint:

    idealista:v1:<sha256 of the canonical form>

The version prefix exists so a future change to the canonicalization rules can
be told apart from today's keys instead of silently re-pointing them.

Canonicalization normalizes only what is *provably* cosmetic, because anything
else silently merges two subscriptions:

* scheme, host case, a leading ``www.``, and the ``/en/`` vs ``/es/`` UI
  language segment (Idealista sends the same search in either language);
* a trailing slash, the fragment, and HTML-escaped ``&amp;`` separators;
* percent-encoding, normalized by decoding to bytes and re-encoding once, so
  ``%7D`` and a literal ``}`` cannot produce two different keys;
* query parameters: only :data:`IDENTITY_QUERY_PARAMS` survive, sorted. Every
  ``utm_*`` is dropped - ``utm_notification_id`` is per email, so keeping it
  would make every alert its own subscription.

Deliberately *not* normalized:

* the path is opaque. Its comma-separated filter segments are not sorted and
  not lowercased - a reordered path is a different search until Idealista is
  shown to emit both forms for one subscription;
* ``shape`` is compared verbatim. It is not rounded, parsed, or compared
  geometrically: a different polygon is a different subscription.

Only ``/areas/`` searches (the custom-drawn-area kind the mailbox actually
receives) are recognized. Any other saved search yields no identity, and the
caller falls back to the pre-existing label/matcher resolution rather than
guessing.

``utils/idealista_extractors.py::extract_url`` deliberately prefers
``/inmueble/<id>`` links and cannot be reused here.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote_from_bytes, unquote_to_bytes, urlsplit

logger = logging.getLogger(__name__)

SEARCH_KEY_PREFIX = "idealista:v1:"
# 13-char prefix + a 64-char sha256 digest.
SEARCH_KEY_LENGTH = len(SEARCH_KEY_PREFIX) + 64

# Query parameters that are part of the subscription's identity. Everything
# else - every utm_*, every tracking id - is dropped.
IDENTITY_QUERY_PARAMS = frozenset({"shape"})

SEARCH_HOSTS = frozenset({"idealista.com"})
SEARCH_PATH_ROOT = "areas"
ALLOWED_SCHEMES = frozenset({"http", "https"})

# The diagnostic column is TEXT, but the value comes from an email, so it is
# bounded. Generous enough for a large `shape` polygon; the key is computed
# from the whole URL either way, so a truncated diagnostic cannot affect
# identity.
MAX_STORED_URL_LENGTH = 4000

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
_LANGUAGE_SEGMENT_RE = re.compile(r"[a-z]{2}")

# Percent-encoding safe sets. `,` stays literal in the path because Idealista
# writes its filters that way; nothing in a query value is left literal.
_PATH_SAFE = ","
_QUERY_SAFE = ""


@dataclass(frozen=True)
class SearchIdentityResult:
    """What an email says about which saved search it belongs to.

    Three outcomes, and the caller must not collapse them:

    * an identity - resolve against it;
    * *absent* (no recognizable search link) - the caller may fall back to the
      label and its own matchers;
    * *ambiguous* (links to several different searches) - resolution stops.
      Falling back to the label here would land the listing in whichever
      same-named subscription happens to exist, which is precisely the guess
      the extractor refused to make.
    """

    identity: Optional["SearchSubscriptionIdentity"] = None
    conflicting: Tuple[str, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return len(self.conflicting) > 1


@dataclass(frozen=True)
class SearchSubscriptionIdentity:
    """One saved search, as identified by the URL an alert email carries."""

    key: str
    canonical: str
    url: str
    """The link exactly as it appeared in the email - diagnostics only."""

    @property
    def label_hint(self) -> str:
        """The search-type path segment, e.g. ``venta-terrenos``.

        Used only to build a readable placeholder label for a subscription
        whose email carried no name.
        """
        segments = self.canonical.partition("?")[0].split("/")
        # canonical == "<host>/areas/<type>/<filters>"
        return segments[2][:40] if len(segments) > 2 else "search"


def _normalize_percent_encoding(raw: str, safe: str) -> str:
    """Re-encode a URL component from its decoded bytes.

    Byte-exact and deterministic in both directions: no charset is guessed, so
    a component that is already percent-encoded and one that is not converge
    on the same output instead of on two different keys.
    """
    return quote_from_bytes(unquote_to_bytes(raw), safe=safe)


def _decoded_text(raw: str) -> str:
    """A component decoded for comparison against an ASCII allowlist."""
    return unquote_to_bytes(raw).decode("utf-8", errors="replace")


def _canonical_host(split) -> Optional[str]:
    host = (split.hostname or "").rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host not in SEARCH_HOSTS:
        return None
    return host


def _canonical_path(raw_path: str) -> Optional[str]:
    segments = [segment for segment in raw_path.split("/") if segment]
    if segments and _LANGUAGE_SEGMENT_RE.fullmatch(segments[0].lower()):
        # The UI language segment ("/en/", "/es/"): the same saved search is
        # linked in whichever language the email was rendered in.
        segments = segments[1:]
    if not segments or segments[0].lower() != SEARCH_PATH_ROOT:
        return None
    return "/" + "/".join(
        _normalize_percent_encoding(segment, safe=_PATH_SAFE) for segment in segments
    )


def _canonical_query(raw_query: str) -> str:
    identity_params = []
    for chunk in raw_query.split("&"):
        if not chunk:
            continue
        raw_name, _, raw_value = chunk.partition("=")
        if _decoded_text(raw_name) not in IDENTITY_QUERY_PARAMS:
            continue
        identity_params.append(
            (
                _normalize_percent_encoding(raw_name, safe=_QUERY_SAFE),
                _normalize_percent_encoding(raw_value, safe=_QUERY_SAFE),
            )
        )
    identity_params.sort()
    return "&".join(f"{name}={value}" for name, value in identity_params)


def canonicalize_search_url(url: str) -> Optional[str]:
    """Return the canonical form of an Idealista search URL, or None.

    None means "this is not a saved-search link this code understands", which
    the caller must treat as "no identity available", never as an error.
    """
    if not url:
        return None

    candidate = html.unescape(str(url)).strip()
    if not candidate:
        return None

    try:
        split = urlsplit(candidate)
    except ValueError:
        logger.debug("Unparseable URL in email body: %r", candidate[:120])
        return None

    if split.scheme.lower() not in ALLOWED_SCHEMES:
        return None

    host = _canonical_host(split)
    if host is None:
        return None

    path = _canonical_path(split.path)
    if path is None:
        return None

    query = _canonical_query(split.query)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def search_key_for_canonical(canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{SEARCH_KEY_PREFIX}{digest}"


def search_key_for_url(url: str) -> Optional[str]:
    canonical = canonicalize_search_url(url)
    if canonical is None:
        return None
    return search_key_for_canonical(canonical)


def _candidate_urls(body: str) -> List[str]:
    """Links worth inspecting, hrefs first.

    Bare-text scanning is a fallback for text/plain-only emails: a URL written
    in prose has no unambiguous end, so a truncated copy of the same link
    would look like a second subscription. Whenever the body has anchors at
    all, they are authoritative.
    """
    text = body or ""
    hrefs = _HREF_RE.findall(text)
    if hrefs:
        return hrefs
    return [match.rstrip(".") for match in _BARE_URL_RE.findall(text)]


def extract_search_identity(body: str) -> SearchIdentityResult:
    """Identify the saved search an alert email belongs to.

    Never guesses. An email linking to several different searches yields an
    *ambiguous* result, not an absent one: the conflict is logged and left for
    a human, and the caller must stop rather than fall back (#102).
    """
    identities: dict[str, SearchSubscriptionIdentity] = {}

    for raw in _candidate_urls(body):
        canonical = canonicalize_search_url(raw)
        if canonical is None:
            continue
        key = search_key_for_canonical(canonical)
        if key in identities:
            continue
        identities[key] = SearchSubscriptionIdentity(
            key=key,
            canonical=canonical,
            url=html.unescape(str(raw)).strip()[:MAX_STORED_URL_LENGTH],
        )

    if not identities:
        return SearchIdentityResult()

    if len(identities) > 1:
        logger.warning(
            "Email carries %d different Idealista search links (%s); refusing to "
            "guess which saved search it belongs to",
            len(identities),
            ", ".join(sorted(identity.canonical for identity in identities.values())),
        )
        return SearchIdentityResult(conflicting=tuple(sorted(identities)))

    return SearchIdentityResult(identity=next(iter(identities.values())))
