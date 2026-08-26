"""The link from a listing to the dossier written about it.

Some of what is known about a listing is not a measurement and never will be:
the cadastral archaeology that identified the parcel, the reading of the
municipal plan, the two agencies' contradictions, the photographs somebody
looked through. That work goes into a per-object dossier site, and until now
nothing in this application pointed at it -- the dossier linked *here*
(``/properties/<id>``, where the enrichment lives) and the return path did not
exist. Somebody with the tracker open had no way to discover that the row had
a dossier at all.

**Why a field and not a convention.** The dossier for property 1282 lives at
``https://1282.cervantes50.com``, and it would be one line of template to
derive that from the id. That line would be wrong the first time a dossier
lands anywhere else -- a different host, a page inside a bigger site, a shared
document -- and it would claim a link for all 1 269 rows, of which fewer than
ten have one. A stored pointer says *this row has a dossier and here it is*;
a derived one says *every row does*, which is #98's defect wearing a URL.

**Where it is stored and why not a column.** ``enrichment["dossier"]``, beside
the other blocks, because it is written once by a person and read by the page.
A column would need a migration and would still hold the same one string.
``enrichment["location"]`` (GEO-002) is the precedent this follows: a
hand-established fact, in its own key, with a writer of its own
(``utils/set_property_dossier.py``) so that recording one is not another
``docker exec`` writing an inference into a field that means something else.

**The reader is total and fail-closed.** ``read_dossier`` never raises and
never returns a URL it has not checked: a block that is not a mapping, a
missing or non-string ``url``, a scheme that is not http/https, an empty host
-- each reads as *no dossier*, exactly as a block nobody wrote does. That is
not tidiness. The value is rendered into an ``href``, Jinja's autoescaping
protects the attribute's quoting and says nothing about its scheme, and
``javascript:`` in an href is script execution on the property page. The
allowed schemes are the guard, and they are checked in the one place both the
template and ``to_dict`` read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

ENRICHMENT_KEY = "dossier"

# The only two schemes that may reach an href on the property page. `mailto:`
# and `file:` are not useful here and `javascript:` is the reason this list
# exists at all.
ALLOWED_SCHEMES = ("http", "https")

MAX_URL_LENGTH = 2000
MAX_TITLE_LENGTH = 200


class DossierError(ValueError):
    """A dossier link that cannot be stored, named by what is wrong with it."""


def normalise_url(url: Any) -> Optional[str]:
    """Return the URL if it may be linked to, else ``None``.

    Shared by the reader and the writer so that what the writer accepts and
    what the page will render can never drift apart -- a stored value the
    reader then refuses would be a link that exists in the database and
    nowhere else.
    """
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    return candidate


def read_dossier(prop: Any) -> Optional[Dict[str, Any]]:
    """What dossier this row points at, or ``None``.

    Pure: no session, no query. The list calls it once per row.
    """
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return None
    block = enrichment.get(ENRICHMENT_KEY)
    if not isinstance(block, dict):
        return None
    url = normalise_url(block.get("url"))
    if url is None:
        return None
    title = block.get("title")
    if not isinstance(title, str) or not title.strip():
        title = urlparse(url).netloc or url
    else:
        title = title.strip()[:MAX_TITLE_LENGTH]
    recorded_at = block.get("recorded_at")
    by = block.get("by")
    return {
        "url": url,
        "title": title,
        "recorded_at": recorded_at if isinstance(recorded_at, str) else None,
        "by": by if isinstance(by, str) else None,
    }


def has_dossier(prop: Any) -> bool:
    return read_dossier(prop) is not None


def record_dossier(
    prop: Any,
    *,
    url: str,
    title: Optional[str] = None,
    by: str = "manual",
    commit: bool = True,
) -> Dict[str, Any]:
    """Store the dossier link on one row, under the row's lock.

    Validated before the lock, per ``services/enrichment_write``'s contract:
    a caller that cannot honour ``commit``, or a URL nothing would render,
    should be told so before a row is held.
    """
    from services.enrichment_write import check_writable, locked_write

    normalised = normalise_url(url)
    if normalised is None:
        raise DossierError(
            "a dossier URL must be an absolute http(s) address with a host; "
            f"got {url!r}"
        )
    clean_title = None
    if isinstance(title, str) and title.strip():
        clean_title = title.strip()[:MAX_TITLE_LENGTH]

    locked = check_writable(prop, commit)

    block = {
        "url": normalised,
        "by": by,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if clean_title:
        block["title"] = clean_title

    with locked_write(prop, locked=locked, commit=commit):
        enrichment = dict(getattr(prop, "enrichment", None) or {})
        enrichment[ENRICHMENT_KEY] = block
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

    return block


def clear_dossier(prop: Any, *, commit: bool = True) -> bool:
    """Remove the pointer. Returns whether there was one.

    Nothing else is touched: the block holds no measurement, so there is
    nothing underneath it to restore.
    """
    from services.enrichment_write import check_writable, locked_write

    locked = check_writable(prop, commit)
    removed = False
    with locked_write(prop, locked=locked, commit=commit):
        enrichment = dict(getattr(prop, "enrichment", None) or {})
        if ENRICHMENT_KEY in enrichment:
            enrichment.pop(ENRICHMENT_KEY)
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            removed = True
    return removed
