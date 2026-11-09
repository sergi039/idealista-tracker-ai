"""Recent, independently observed publication on explicitly matched adverts."""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from services.listing_browser import identity
from services.listing_verification import TERMINAL_STATUSES

KEY = "alternate_publications"
WINDOW = timedelta(hours=24)
REFUSALS = frozenset({"blocked", "backing_off", "not_the_listing_page"})
LABELS = {"yaencontre": "Yaencontre", "fotocasa": "Fotocasa"}
_YAE = re.compile(
    r"https://(?:www\.yaencontre\.com/venta/casa/inmueble|ca\.yaencontre\.com/venda/casa/immoble)"
    r"-[0-9]{1,12}-([0-9]{1,20})/?"
)
_FOTO = re.compile(
    r"https://www\.fotocasa\.es/es/comprar/vivienda/[a-z0-9-]+/[a-z0-9-]+/[0-9]{4,12}/d/?"
)


def receipt(primary_url, source_url, observed_at):
    """Validate a public-index observation; never fetch the supplied URL."""
    primary_id = identity(primary_url)[2]
    if not isinstance(source_url, str) or len(source_url) > 350:
        raise ValueError("Expected a public listing URL")
    match = _YAE.fullmatch(source_url)
    if match and match.group(1) == primary_id:
        source = "yaencontre"
    elif _FOTO.fullmatch(source_url):
        # Different portal ID: the operator explicitly attests the match.
        source = "fotocasa"
    else:
        raise ValueError("Alternate URL is unsupported or its listing ID differs")
    if not isinstance(observed_at, str) or len(observed_at) > 40:
        raise ValueError("Expected an ISO observation timestamp")
    timestamp = datetime.fromisoformat(observed_at)
    if timestamp.tzinfo is None:
        raise ValueError("Observation timestamp must include its timezone")
    return source, {
        "primary_url": primary_url,
        "url": source_url,
        "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "method": "public_index",
    }


def _fresh(observed_at, now):
    return now - WINDOW <= datetime.fromisoformat(observed_at) <= now


def current_publications(prop, now):
    if (getattr(prop, "listing_status", None) or "").lower() in TERMINAL_STATUSES:
        return []
    enrichment = getattr(prop, "enrichment", None)
    receipts = enrichment.get(KEY) if isinstance(enrichment, dict) else None
    if not isinstance(receipts, dict):
        return []
    publications = []
    for source, label in LABELS.items():
        saved = receipts.get(source)
        if not isinstance(saved, dict) or saved.get("primary_url") != prop.url:
            continue
        try:
            provider, evidence = receipt(
                prop.url, saved.get("url"), saved.get("observed_at")
            )
            if (
                provider == source
                and saved.get("method") == "public_index"
                and _fresh(evidence["observed_at"], now)
            ):
                date = datetime.fromisoformat(evidence["observed_at"])
                publications.append(
                    {
                        "label": label,
                        "url": evidence["url"],
                        "observed_local": date.astimezone(
                            ZoneInfo("Europe/Madrid")
                        ).strftime("%Y-%m-%d %H:%M"),
                    }
                )
        except (ValueError, OverflowError):
            continue
    return publications


def record_publication(prop, primary_url, source_url, observed_at, now):
    """Record an operator-verified cross-listing match without changing status."""
    from services.enrichment_write import check_writable, locked_write

    source, evidence = receipt(primary_url, source_url, observed_at)
    if not _fresh(evidence["observed_at"], now):
        raise ValueError("Observation must be within the past 24 hours")
    locked = check_writable(prop, True)
    with locked_write(prop, locked=locked, commit=True):
        if (
            prop.url != primary_url
            or (prop.listing_status or "").lower() in TERMINAL_STATUSES
        ):
            raise ValueError("Property URL changed or the property is removed/sold")
        enrichment = dict(prop.enrichment or {})
        existing = enrichment.get(KEY)
        receipts = dict(existing) if isinstance(existing, dict) else {}
        prior = receipts.get(source)
        if prior == evidence:
            return False
        if (
            isinstance(prior, dict)
            and prior.get("primary_url") == primary_url
            and isinstance(prior.get("observed_at"), str)
            and prior["observed_at"] >= evidence["observed_at"]
        ):
            raise ValueError(
                "Refusing to replace an equally recent or newer observation"
            )
        receipts[source] = evidence
        enrichment[KEY] = receipts
        prop.enrichment = enrichment
    return True
