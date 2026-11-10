"""Group confirmed advertisements of the same home in recommendations."""

from collections import defaultdict
from urllib.parse import urlsplit

from services import owner_review
from services.yaencontre_source import listing_id_from_url
from utils.idealista_extractors import extract_idealista_property_id
from utils.listing_source import IDEALISTA, YAENCONTRE, source_of

# Cross-portal listing IDs are independently assigned. These links are
# corroborated individually, including the Seaia advert with different figures.
_CONFIRMED_ALIASES = {
    (YAENCONTRE, 93478124): (IDEALISTA, 93478124),  # DP-4305, matching cover
    (YAENCONTRE, 106962112): (IDEALISTA, 93478124),  # Seaia, same facade
    (YAENCONTRE, 108812772): (IDEALISTA, 108812772),  # Taboido, matching cover
    (YAENCONTRE, 105654216): (IDEALISTA, 105654216),  # Seiruga, matching listing facts
    (IDEALISTA, 112319968): (IDEALISTA, 105654216),  # Seiruga, dossier's other_listings
}
# Read by services/property_duplicates.py as `confirmed`, source "seed".
CONFIRMED_ALIASES = _CONFIRMED_ALIASES
_CONFIRMED_REFERENCES = frozenset(_CONFIRMED_ALIASES.values())


def _listing_key(row):
    source = source_of(row)
    if source == IDEALISTA:
        url = row.url.strip()
        path = urlsplit(url if "//" in url else f"//{url}").path
        listing_id = extract_idealista_property_id(path)
    elif source == YAENCONTRE:
        listing_id = listing_id_from_url(row.url)
    else:
        return None
    return (source, listing_id) if listing_id is not None else None


def duplicate_ids(rows, readings, excluded_ids, *, focus_id=None):
    """Return aliases with an eligible representative in the filtered result."""
    groups = defaultdict(list)
    for row in rows:
        if row.id in excluded_ids:
            continue
        listing_key = _listing_key(row)
        if listing_key is None:
            continue
        reference_key = _CONFIRMED_ALIASES.get(listing_key, listing_key)
        # Exact repeats are grouped within a search. Confirmed links identify
        # the home even when the portals routed their ads to different searches.
        profile_id = (
            None if reference_key in _CONFIRMED_REFERENCES else row.search_profile_id
        )
        groups[profile_id, reference_key].append(
            (
                row,
                reference_key in _CONFIRMED_REFERENCES and listing_key == reference_key,
            )
        )

    def priority(entry):
        row, confirmed_reference = entry
        return (
            row.id == focus_id,
            # Keep the selected reference whenever it passes the current filters.
            confirmed_reference,
            readings[row.id].get("state") == "reference",
            bool(row.is_favorite),
            owner_review.read_decision(row)["state"]
            in (owner_review.INTERESTED, owner_review.WAITING),
            row.listing_status not in {"removed", "sold"},
            row.location_accuracy == "precise",
            -row.id,
        )

    hidden = set()
    for group in groups.values():
        if len(group) > 1:
            representative, _ = max(group, key=priority)
            hidden.update(row.id for row, _ in group if row.id != representative.id)
    return hidden
