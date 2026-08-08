import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from app import db
from models import Property, SearchProfile
from services.search_subscription_identity import (
    SearchSubscriptionIdentity,
    extract_search_identity,
)
from services.settings_service import SettingsService

logger = logging.getLogger(__name__)


DEFAULT_PROFILE_NAME = "Default"

# How many times identity resolution re-reads after losing a row to a
# concurrent ingestion. Bounded: an unbounded retry would spin against a
# livelock instead of reporting one.
IDENTITY_RESOLUTION_ATTEMPTS = 3

# Sentinel accepted in the `profile_id` query param to mean "no profile
# filter, show every profile at once". An empty string means the same thing
# (the "All profiles" <option value=""> in the filter form submits this).
PROFILE_ALL_SENTINEL = "all"

TRAVEL_PRESET_DEFS: Dict[str, Dict[str, Any]] = {
    "airport": {"label": "Nearest airport", "place_types": ["airport"]},
    "train_station": {
        "label": "Nearest train station",
        "place_types": ["train_station"],
    },
    "hospital": {"label": "Nearest hospital", "place_types": ["hospital"]},
    "police": {"label": "Nearest police station", "place_types": ["police"]},
    "supermarket": {"label": "Nearest supermarket", "place_types": ["supermarket"]},
    "school": {"label": "Nearest school", "place_types": ["school"]},
}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _clean_profile_name(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None

    # Strip surrounding quotes and trailing punctuation.
    raw = raw.strip().strip('"').strip("'").strip()
    raw = raw.rstrip("!.,;:").strip()
    raw = " ".join(raw.split())
    # Idealista sometimes prefixes names with "Search"/"Búsqueda"; normalize it away.
    raw = re.sub(
        r"^(?:search|búsqueda|busqueda)\s+", "", raw, flags=re.IGNORECASE
    ).strip()
    if not raw:
        return None

    # Drop duplicate trailing segment like ", Alicante" when it's already in the name.
    if "," in raw:
        head, tail = raw.rsplit(",", 1)
        tail = tail.strip()
        if tail and re.search(rf"\b{re.escape(tail)}\b", head, re.IGNORECASE):
            raw = head.strip()

    return raw[:120]


def _canonical_profile_name(value: str) -> Optional[str]:
    cleaned = _clean_profile_name(value)
    if not cleaned:
        return None
    return cleaned.lower()


def extract_search_name(subject: str, body: str) -> Optional[str]:
    """Extract Idealista saved-search name from email subject/body (best-effort).

    Examples:
    - "New detached house in your search: Search Junio!"
    - "See all listings for \"Search Junio\""
    """
    text = f"{subject}\n{body}"

    patterns = [
        # Subject: "... in your search: Search Junio!"
        r"\bin your search:\s*(?:Search|Búsqueda)\s+(?P<name>[^\n\r!]+)",
        r"\ben tu búsqueda:\s*(?:Search|Búsqueda)\s+(?P<name>[^\n\r!]+)",
        # Subject: "... in your search: Homes in Ciudad Quesada" (no Search/Búsqueda prefix)
        r"\bin your search:\s*(?P<name>[^\n\r!]+)",
        r"\ben tu búsqueda:\s*(?P<name>[^\n\r!]+)",
        # Body: See all listings for 'Search Junio'
        r"See all listings for\s+['\"]?Search\s+(?P<name>[^'\"\n\r]+)",
        r"Ver todos los anuncios de\s+['\"]?Búsqueda\s+(?P<name>[^'\"\n\r]+)",
        # Body: See all listings for 'Homes in Ciudad Quesada' (no Search/Búsqueda prefix)
        r"See all listings for\s+['\"]?(?P<name>[^'\"\n\r]+)",
        r"Ver todos los anuncios de\s+['\"]?(?P<name>[^'\"\n\r]+)",
        # Body: for “Search Junio” (smart quotes)
        r"See all listings for\s+[“\"]Search\s+(?P<name>[^”\"\n\r]+)[”\"]",
        r"Ver todos los anuncios de\s+[“\"]Búsqueda\s+(?P<name>[^”\"\n\r]+)[”\"]",
        # Body: for “Homes in Ciudad Quesada” (smart quotes, no prefix)
        r"See all listings for\s+[“\"](?P<name>[^”\"\n\r]+)[”\"]",
        r"Ver todos los anuncios de\s+[“\"](?P<name>[^”\"\n\r]+)[”\"]",
    ]

    for pattern in patterns:
        try:
            match = re.search(pattern, text, re.IGNORECASE)
        except re.error:
            continue
        if not match:
            continue
        name = _clean_profile_name(match.group("name"))
        if name:
            return name

    return None


def default_travel_targets_config() -> Dict[str, Any]:
    return {
        "presets": {
            key: {"enabled": True, "mode": "driving"}
            for key in TRAVEL_PRESET_DEFS.keys()
        },
        "custom": [],
    }


def normalize_travel_targets_config(value: Any) -> Dict[str, Any]:
    """Normalize travel_targets JSON into the canonical structure.

    Canonical shape:
    {
      "presets": { "<preset_key>": {"enabled": bool, "mode": "driving"} },
      "custom": [ {"id": "...", "name": "...", "lat": .., "lon": .., "mode": "driving", ...}, ... ]
    }
    """
    presets: Dict[str, Dict[str, Any]] = {}
    custom: List[Dict[str, Any]] = []
    allowed_modes = {"driving", "walking", "transit", "bicycling"}

    if isinstance(value, dict):
        raw_presets = value.get("presets")
        raw_custom = value.get("custom")
    elif isinstance(value, list):
        raw_presets = {}
        raw_custom = []
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = (item.get("kind") or "").strip().lower()
            if kind == "preset" or "preset" in item:
                key = str(item.get("preset") or item.get("key") or "").strip()
                if key:
                    raw_presets[key] = item
            else:
                raw_custom.append(item)
    else:
        raw_presets = None
        raw_custom = None

    if isinstance(raw_presets, dict):
        for key in TRAVEL_PRESET_DEFS.keys():
            item = raw_presets.get(key, {})
            if isinstance(item, dict):
                enabled = bool(item.get("enabled", True))
                mode = str(item.get("mode") or "driving").strip().lower() or "driving"
                if mode not in allowed_modes:
                    mode = "driving"
            else:
                enabled = True
                mode = "driving"
            presets[key] = {"enabled": enabled, "mode": mode}
    else:
        presets = {
            key: {"enabled": True, "mode": "driving"}
            for key in TRAVEL_PRESET_DEFS.keys()
        }

    if isinstance(raw_custom, list):
        for item in raw_custom:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                lat = float(item.get("lat"))
                lon = float(item.get("lon"))
            except Exception:
                continue
            mode = str(item.get("mode") or "driving").strip().lower() or "driving"
            if mode not in allowed_modes:
                mode = "driving"
            custom.append(
                {
                    "id": str(item.get("id") or "").strip() or None,
                    "name": name[:120],
                    "lat": lat,
                    "lon": lon,
                    "mode": mode,
                    "address": item.get("address"),
                    "formatted_address": item.get("formatted_address"),
                }
            )

    return {"presets": presets, "custom": custom}


class SearchProfileService:
    @staticmethod
    def find_unidentified_by_name(name: str) -> Optional[SearchProfile]:
        """A profile carrying this label that is not somebody's saved search.

        Labels stopped being unique in #102, so any lookup by name may now hit
        a real subscription: a saved search can be called "Default" without
        being the catch-all, or "Legacy Lands" without being the archive.
        Restricting to rows with no search key - of which the partial unique
        index keeps at most one - is what makes a name lookup safe again.

        This is the shared primitive for that. Every by-name lookup outside the
        deliberate conflict *detectors* should go through it.
        """
        return SearchProfile.query.filter(
            SearchProfile.name == name,
            SearchProfile.source_search_key.is_(None),
        ).first()

    @staticmethod
    def get_default_profile(create: bool = True) -> Optional[SearchProfile]:
        """Return the default profile, creating one if missing (best-effort)."""
        profile = SearchProfile.query.filter_by(is_default=True).first()
        if profile:
            return profile

        profile = SearchProfileService.find_unidentified_by_name(DEFAULT_PROFILE_NAME)
        if profile:
            if not profile.is_default:
                profile.is_default = True
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            return profile

        if not create:
            return None

        try:
            profile = SearchProfile(
                name=DEFAULT_PROFILE_NAME,
                description="Autocreated default profile",
                is_active=True,
                is_default=True,
                travel_targets=default_travel_targets_config(),
            )
            db.session.add(profile)
            db.session.commit()
            return profile
        except Exception as e:
            # Losing the insert race is normal now that the partial unique
            # index enforces one keyless profile per label: read the winner
            # instead of reporting "no default profile".
            logger.warning("Failed to auto-create default SearchProfile: %s", e)
            db.session.rollback()
            return SearchProfile.query.filter_by(
                is_default=True
            ).first() or SearchProfileService.find_unidentified_by_name(
                DEFAULT_PROFILE_NAME
            )

    @staticmethod
    def list_profiles(active_only: bool = True) -> List[SearchProfile]:
        q = SearchProfile.query
        if active_only:
            q = q.filter(SearchProfile.is_active.is_(True))
        return q.order_by(
            SearchProfile.is_default.desc(), SearchProfile.name.asc()
        ).all()

    @staticmethod
    def get_or_create_profile_by_name(name: str) -> Optional[SearchProfile]:
        """Resolve an email by its label alone - only ever a last resort.

        Since #102 a label can be shared by several *identified* saved
        searches, so it no longer picks out one subscription on its own. An
        unidentified profile with that label wins (the partial unique index
        keeps at most one), a single canonical match is still honoured, and a
        label claimed by several different subscriptions resolves to nothing
        rather than to whichever row came back first.
        """
        cleaned = _clean_profile_name(name)
        if not cleaned:
            return None

        profile = SearchProfileService.find_unidentified_by_name(cleaned)
        if profile:
            return profile

        canonical = _canonical_profile_name(cleaned)
        if canonical:
            matches = [
                candidate
                for candidate in SearchProfile.query.order_by(
                    SearchProfile.id.asc()
                ).all()
                if _canonical_profile_name(candidate.name) == canonical
            ]
            if len(matches) == 1:
                return matches[0]
            if matches:
                logger.warning(
                    "Label %r is claimed by %d saved searches %s; an email that "
                    "carries no search URL cannot say which one it belongs to",
                    cleaned,
                    len(matches),
                    [candidate.id for candidate in matches],
                )
                return None

        try:
            profile = SearchProfile(
                name=cleaned,
                description="Autocreated from Idealista saved search name",
                is_active=True,
                is_default=False,
                # The label came out of an email, so a later email carrying
                # this subscription's URL may correct it (#102).
                is_auto_created=True,
                travel_targets=default_travel_targets_config(),
            )
            db.session.add(profile)
            db.session.commit()
            return profile
        except Exception as e:
            # Losing the insert race to a concurrent ingestion is normal. Read
            # back the *unidentified* winner: a plain lookup by name could
            # return a keyed profile that appeared alongside it, handing this
            # email to somebody else's subscription.
            logger.warning("Failed to create SearchProfile %r: %s", cleaned, e)
            db.session.rollback()
            return SearchProfileService.find_unidentified_by_name(cleaned)

    @staticmethod
    def merge_duplicate_profiles(commit: bool = True) -> Dict[str, Any]:
        """Merge profiles that normalize to the same canonical name.

        A shared label is no longer evidence of a duplicate: since #102 two
        saved searches may legitimately carry the same name with a different
        `shape`. A group holding more than one distinct search key is
        therefore reported as a conflict and left completely alone - merging
        it would delete a real subscription.
        """

        profiles = SearchProfile.query.order_by(SearchProfile.id.asc()).all()
        groups: Dict[str, List[SearchProfile]] = {}
        for profile in profiles:
            canonical = _canonical_profile_name(profile.name)
            if not canonical:
                continue
            groups.setdefault(canonical, []).append(profile)

        merged = 0
        reassigned = 0
        deleted = 0
        renamed = 0
        details: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []

        for canonical, group in groups.items():
            search_keys = {p.source_search_key for p in group if p.source_search_key}

            # Two reasons to refuse a group outright. Both would destroy an
            # invariant that cannot be reconstructed afterwards, so they are
            # reported for a human instead of resolved by guessing.
            refusal = None
            if len(search_keys) > 1:
                refusal = "different saved-search keys"
            elif search_keys and any(p.is_default for p in group):
                # The default is the fallback for everything that matches
                # nothing. Merging here would either pin its key onto the
                # catch-all (it sorts first, so it becomes the primary) or make
                # one subscription the recipient of all unmatched mail.
                refusal = "the default profile and an identified saved search"

            if refusal:
                logger.warning(
                    "Refusing to merge %d profiles labelled %r: the group holds "
                    "%s (%s)",
                    len(group),
                    canonical,
                    refusal,
                    sorted(search_keys),
                )
                conflicts.append(
                    {
                        "canonical": canonical,
                        "profile_ids": [p.id for p in group],
                        "search_keys": sorted(search_keys),
                        "reason": refusal,
                    }
                )
                continue

            if len(group) <= 1:
                primary = group[0]
                cleaned_name = _clean_profile_name(primary.name)
                if cleaned_name and cleaned_name != primary.name:
                    primary.name = cleaned_name
                    renamed += 1
                continue

            counts = {
                p.id: Property.query.filter_by(search_profile_id=p.id).count()
                for p in group
            }
            group_sorted = sorted(
                group,
                key=lambda p: (not p.is_default, -counts.get(p.id, 0), p.id),
            )
            primary = group_sorted[0]
            cleaned_name = _clean_profile_name(primary.name)
            if cleaned_name and cleaned_name != primary.name:
                primary.name = cleaned_name

            # The group holds at most one search key (the guard above), and
            # the primary is picked by property count, so the keyless row
            # usually wins. Deleting the keyed one would delete the
            # subscription's identity with it, and nothing records which saved
            # search a stored row came from, so it could not be recovered.
            carried_key = next(
                (p.source_search_key for p in group_sorted if p.source_search_key), None
            )
            carried_url = next(
                (p.source_search_url for p in group_sorted if p.source_search_key), None
            )

            for dup in group_sorted[1:]:
                if dup.is_default and not primary.is_default:
                    primary.is_default = True
                if dup.is_active and not primary.is_active:
                    primary.is_active = True
                if (
                    not (primary.description or "").strip()
                    and (dup.description or "").strip()
                ):
                    primary.description = dup.description

                primary_targets = normalize_travel_targets_config(
                    primary.travel_targets
                )
                dup_targets = normalize_travel_targets_config(dup.travel_targets)
                primary_custom = list(primary_targets.get("custom") or [])
                dup_custom = list(dup_targets.get("custom") or [])
                if dup_custom:
                    seen = {
                        (
                            str(item.get("name") or "").strip().lower(),
                            item.get("lat"),
                            item.get("lon"),
                        )
                        for item in primary_custom
                    }
                    for item in dup_custom:
                        key = (
                            str(item.get("name") or "").strip().lower(),
                            item.get("lat"),
                            item.get("lon"),
                        )
                        if key in seen:
                            continue
                        primary_custom.append(item)
                        seen.add(key)
                    primary_targets["custom"] = primary_custom
                    primary.travel_targets = normalize_travel_targets_config(
                        primary_targets
                    )

                updated = Property.query.filter_by(search_profile_id=dup.id).update(
                    {"search_profile_id": primary.id}
                )
                reassigned += updated
                db.session.delete(dup)
                deleted += 1

            if carried_key and not primary.source_search_key:
                # Flush the deletes first: the unique index would reject the
                # instant both rows hold the same key.
                db.session.flush()
                primary.source_search_key = carried_key
                primary.source_search_url = carried_url
                logger.info(
                    "Merged group %r kept saved-search key %s on profile %s",
                    canonical,
                    carried_key,
                    primary.id,
                )

            merged += 1
            details.append(
                {
                    "canonical": canonical,
                    "primary_id": primary.id,
                    "removed_ids": [p.id for p in group_sorted[1:]],
                    "properties_moved": counts,
                }
            )

        if commit and (merged or renamed):
            db.session.commit()

        return {
            "merged_groups": merged,
            "profiles_deleted": deleted,
            "properties_reassigned": reassigned,
            "profiles_renamed": renamed,
            "details": details,
            "conflicts": conflicts,
        }

    @staticmethod
    def _commit_profile_change(profile: SearchProfile, what: str) -> None:
        try:
            db.session.commit()
        except Exception as e:
            logger.warning("Failed to %s for profile %s: %s", what, profile.id, e)
            db.session.rollback()

    @staticmethod
    def _profiles_named(name: str, exclude_id: Optional[int] = None) -> List[int]:
        """Ids of the profiles whose label normalizes to ``name``."""
        canonical = _canonical_profile_name(name)
        if not canonical:
            return []
        return [
            profile.id
            for profile in SearchProfile.query.order_by(SearchProfile.id.asc()).all()
            if profile.id != exclude_id
            and _canonical_profile_name(profile.name) == canonical
        ]

    @staticmethod
    def _relabel_if_auto_created(
        profile: SearchProfile, search_name: Optional[str]
    ) -> bool:
        """Follow a reworded saved-search name, but only on our own labels.

        A label the owner chose is never rewritten (`is_auto_created`, a real
        column rather than a guess at the description text), and neither is a
        label that another profile already carries - that is an identity
        conflict, reported and left alone rather than resolved by guessing.
        """
        if not search_name or profile.name == search_name:
            return False

        conflicting = SearchProfileService._profiles_named(
            search_name, exclude_id=profile.id
        )
        if conflicting:
            logger.warning(
                "Saved-search identity conflict: search key %s belongs to profile "
                "%s (%r) but the email label %r belongs to profile(s) %s; leaving "
                "both alone",
                profile.source_search_key,
                profile.id,
                profile.name,
                search_name,
                conflicting,
            )
            return False

        if not profile.is_auto_created:
            logger.info(
                "Profile %s was named by the owner (%r); not relabelling it to %r",
                profile.id,
                profile.name,
                search_name,
            )
            return False

        logger.info(
            "Saved search %s was relabelled: %r -> %r",
            profile.source_search_key,
            profile.name,
            search_name,
        )
        profile.name = search_name
        return True

    @staticmethod
    def _adopt_keyless_profile(
        identity: SearchSubscriptionIdentity, search_name: Optional[str]
    ) -> Tuple[Optional[SearchProfile], bool]:
        """Bind the search key to the existing profile of the same name.

        Returns `(profile, contested)`. `contested` means a candidate existed
        but was claimed by someone else first, so the caller should look again
        rather than immediately create a twin.

        This is the upgrade path: profiles created before #102 have no key, so
        the first email that carries a URL attaches the identity to the row
        that already holds the listings instead of starting an empty twin.

        The default profile is excluded on purpose. It is the catch-all for
        everything that matches nothing, so pinning one subscription's key to
        it would route that subscription's future emails through the same row
        that keeps collecting unrelated mail.
        """
        if not search_name:
            return None, False

        canonical = _canonical_profile_name(search_name)
        if not canonical:
            return None, False

        candidates = [
            profile
            for profile in SearchProfile.query.filter(
                SearchProfile.source_search_key.is_(None)
            )
            .order_by(SearchProfile.id.asc())
            .all()
            if not profile.is_default
            and _canonical_profile_name(profile.name) == canonical
        ]
        if not candidates:
            return None, False

        if len(candidates) > 1:
            logger.warning(
                "Label %r matches %d keyless profiles %s; binding search key %s to "
                "the oldest one that is still free and leaving the rest untouched",
                search_name,
                len(candidates),
                [candidate.id for candidate in candidates],
                identity.key,
            )

        for candidate in candidates:
            if SearchProfileService._claim_keyless_profile(candidate, identity):
                return (
                    SearchProfile.query.filter_by(
                        source_search_key=identity.key
                    ).first(),
                    False,
                )

        # Every candidate was taken between the SELECT and the UPDATE.
        return None, True

    @staticmethod
    def _claim_keyless_profile(
        profile: SearchProfile, identity: SearchSubscriptionIdentity
    ) -> bool:
        """Bind the key to a profile, but only while the row still has none.

        The candidate list above is a snapshot. Two ingestions overlap
        routinely - the scheduled run and a manual one, across four gunicorn
        threads - and two subscriptions may share a label, so both can select
        the same keyless row. An unconditional UPDATE lets the second one
        silently re-point that profile and hand its stored listings to the
        wrong saved search, so the claim is conditional on the row still being
        unclaimed and the caller retries when it loses.
        """
        claimed = SearchProfile.query.filter(
            SearchProfile.id == profile.id,
            SearchProfile.source_search_key.is_(None),
        ).update(
            {
                SearchProfile.source_search_key: identity.key,
                SearchProfile.source_search_url: identity.url,
            },
            synchronize_session=False,
        )

        if not claimed:
            # Rolling back also expires the stale snapshot, so the retry reads
            # the row as it now is.
            db.session.rollback()
            logger.warning(
                "Profile %s was claimed by another saved search before %s could "
                "bind to it; not re-pointing it",
                profile.id,
                identity.key,
            )
            return False

        try:
            db.session.commit()
            return True
        except Exception as e:
            logger.warning(
                "Failed to bind search key %s to profile %s: %s",
                identity.key,
                profile.id,
                e,
            )
            db.session.rollback()
            return False

    @staticmethod
    def _create_profile_for_identity(
        identity: SearchSubscriptionIdentity, search_name: Optional[str]
    ) -> Optional[SearchProfile]:
        name = search_name or f"Idealista {identity.label_hint} ({identity.key[-8:]})"
        try:
            profile = SearchProfile(
                name=name[:120],
                description="Autocreated from an Idealista saved-search URL",
                is_active=True,
                is_default=False,
                is_auto_created=True,
                source_search_key=identity.key,
                source_search_url=identity.url,
                travel_targets=default_travel_targets_config(),
            )
            db.session.add(profile)
            db.session.commit()
            return profile
        except Exception as e:
            # A concurrent ingestion may have inserted the same key first; the
            # unique index is what makes that safe to retry as a read.
            logger.warning("Failed to create SearchProfile for %s: %s", identity.key, e)
            db.session.rollback()
            return SearchProfile.query.filter_by(source_search_key=identity.key).first()

    @staticmethod
    def resolve_profile_by_identity(
        identity: SearchSubscriptionIdentity, search_name: Optional[str]
    ) -> Optional[SearchProfile]:
        """Resolve a saved search by its URL fingerprint.

        Order: the search key, then an existing same-named profile that has no
        key yet, then a new profile. Nothing here ever falls through to the
        default profile - an email that names its own saved search must not
        land in the catch-all.

        The whole sequence retries when a concurrent ingestion claims the row
        first: by then that row may even hold *this* key, so the retry starts
        again from the key lookup rather than creating a twin.
        """
        for _ in range(IDENTITY_RESOLUTION_ATTEMPTS):
            profile = SearchProfile.query.filter_by(
                source_search_key=identity.key
            ).first()
            if profile is not None:
                changed = SearchProfileService._relabel_if_auto_created(
                    profile, search_name
                )
                if profile.source_search_url != identity.url:
                    # Diagnostics: keep the most recent link, so the row shows
                    # what the mailbox is actually sending for this search.
                    profile.source_search_url = identity.url
                    changed = True
                if changed:
                    SearchProfileService._commit_profile_change(
                        profile, "update the label"
                    )
                return profile

            adopted, contested = SearchProfileService._adopt_keyless_profile(
                identity, search_name
            )
            if adopted is not None:
                return adopted
            if contested:
                continue

            return SearchProfileService._create_profile_for_identity(
                identity, search_name
            )

        logger.error(
            "Gave up resolving saved search %s after %d contested attempts; the "
            "email is left unassigned rather than bound to a guess",
            identity.key,
            IDENTITY_RESOLUTION_ATTEMPTS,
        )
        return None

    @staticmethod
    def resolve_profile(subject: str, body: str) -> Optional[SearchProfile]:
        """Pick a profile for an incoming email.

        The saved-search URL in the body is the identity (#102); the name in
        the subject is only a label. Emails that carry no recognizable search
        URL keep the older resolution: saved-search name, then the profile's
        own `email_matchers`, then the default profile.

        Returns None for an email that links to several *different* searches.
        That is not the same as an email with no link: falling back to the
        label there would bind the listing to whichever same-named
        subscription happens to exist, which is the guess this whole change
        exists to prevent. The listing is stored unassigned instead, and the
        conflict is in the log.
        """
        search_name = extract_search_name(subject, body)

        # 1) The saved search's own URL, which encodes its filters.
        found = extract_search_identity(body)
        if found.is_ambiguous:
            logger.warning(
                "Refusing to resolve a profile for an email that links to %d "
                "different saved searches (%s)",
                len(found.conflicting),
                ", ".join(found.conflicting),
            )
            return None
        if found.identity is not None:
            profile = SearchProfileService.resolve_profile_by_identity(
                found.identity, search_name
            )
            if profile is None:
                # The email said which saved search it belongs to and we could
                # not act on it (contested retries exhausted, or the insert
                # failed). Falling through to the label would be worse than
                # leaving it unassigned: labels are no longer unique among
                # identified profiles, so the name could resolve to a profile
                # carrying somebody else's search key.
                logger.error(
                    "Saved search %s was identified but could not be resolved; "
                    "leaving the email unassigned rather than matching it by label",
                    found.identity.key,
                )
            return profile

        # 2) The saved search name embedded in the email.
        if search_name:
            profile = SearchProfileService.get_or_create_profile_by_name(search_name)
            if profile:
                return profile

        # 3) Fallback: custom regex matchers.
        text = f"{subject}\n{body}"

        candidates = SearchProfileService.list_profiles(active_only=True)
        best: Optional[Tuple[int, SearchProfile]] = None

        for profile in candidates:
            rules = _as_list(profile.email_matchers)
            for rule in rules:
                if isinstance(rule, str):
                    pattern = rule
                    priority = 0
                elif isinstance(rule, dict):
                    pattern = str(rule.get("pattern") or "").strip()
                    try:
                        priority = int(rule.get("priority") or 0)
                    except Exception:
                        priority = 0
                else:
                    continue

                if not pattern:
                    continue

                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        if best is None or priority > best[0]:
                            best = (priority, profile)
                except re.error:
                    continue

        if best:
            return best[1]

        return SearchProfileService.get_default_profile(create=True)

    @staticmethod
    def get_classification_rules(
        profile: Optional[SearchProfile],
    ) -> List[Dict[str, Any]]:
        """Return classification rules for a profile, falling back to global defaults."""
        if (
            profile
            and isinstance(profile.classification_rules, list)
            and profile.classification_rules
        ):
            rules = [r for r in profile.classification_rules if isinstance(r, dict)]
            rules.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
            return rules
        return SettingsService.get_property_classification_rules()

    @staticmethod
    def get_travel_targets_config(profile: Optional[SearchProfile]) -> Dict[str, Any]:
        if profile and profile.travel_targets:
            return normalize_travel_targets_config(profile.travel_targets)
        return default_travel_targets_config()

    @staticmethod
    def get_travel_preset_defs() -> List[Dict[str, Any]]:
        return [{"key": k, **v} for k, v in TRAVEL_PRESET_DEFS.items()]

    @staticmethod
    def get_ai_market_context(profile: Optional[SearchProfile]) -> str:
        """Return AI market context text for a profile (override), else global default."""
        try:
            if profile and isinstance(getattr(profile, "ai_config", None), dict):
                raw = str((profile.ai_config or {}).get("market_context") or "").strip()
                if raw:
                    return raw
        except Exception:
            pass
        return SettingsService.get_ai_market_context()

    @staticmethod
    def parse_profile_selection(args: Any) -> Tuple[str, Optional[int]]:
        """Parse a `profile_id` query parameter into an explicit selection state.

        Returns `(state, profile_id)`:
          - `("auto", None)`: the param is absent entirely -- the caller should
            apply its own default/auto-select fallback (existing behaviour,
            so old bookmarked/saved links keep working unchanged).
          - `("all", None)`: the user explicitly asked to see every profile at
            once (`profile_id=` empty or `profile_id=all`) -- do not filter.
          - `("specific", <int>)`: a single profile was explicitly requested.

        An unparseable value that is present but neither empty/"all" nor a
        valid integer is treated as `("auto", None)`, matching the previous
        `request.args.get("profile_id", type=int)` behaviour, which silently
        returned `None` on a bad value instead of erroring.
        """
        if "profile_id" not in args:
            return "auto", None

        raw = (args.get("profile_id") or "").strip()
        if raw == "" or raw.lower() == PROFILE_ALL_SENTINEL:
            return "all", None

        try:
            return "specific", int(raw)
        except (TypeError, ValueError):
            return "auto", None

    @staticmethod
    def resolve_richest_active_profile_id(
        default_profile: Optional[SearchProfile], profiles: List[SearchProfile]
    ) -> Optional[int]:
        """Auto-select a sensible profile when the caller didn't request one.

        Prefers the default profile, but only if it actually has properties --
        profiles are auto-created from email search names, so a fresh install's
        "Default" profile is often empty and would otherwise render an empty
        list. Falls back to the most recently active profile, then the first
        active profile, then None (no active profiles at all).
        """
        default_has_props = False
        if default_profile:
            default_has_props = (
                Property.query.filter(Property.search_profile_id == default_profile.id)
                .limit(1)
                .first()
                is not None
            )
        if default_profile and default_has_props:
            return default_profile.id

        recent = (
            db.session.query(
                Property.search_profile_id,
                func.max(Property.created_at).label("latest"),
            )
            .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
            .filter(SearchProfile.is_active.is_(True))
            .group_by(Property.search_profile_id)
            .order_by(func.max(Property.created_at).desc())
            .first()
        )
        if recent and recent[0] is not None:
            return int(recent[0])
        if default_profile:
            return default_profile.id
        if profiles:
            return profiles[0].id
        return None
