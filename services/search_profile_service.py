import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from app import db
from models import Property, SearchProfile
from services.settings_service import SettingsService

logger = logging.getLogger(__name__)


DEFAULT_PROFILE_NAME = "Default"

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
    def get_default_profile(create: bool = True) -> Optional[SearchProfile]:
        """Return the default profile, creating one if missing (best-effort)."""
        profile = SearchProfile.query.filter_by(is_default=True).first()
        if profile:
            return profile

        profile = SearchProfile.query.filter_by(name=DEFAULT_PROFILE_NAME).first()
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
            logger.warning("Failed to auto-create default SearchProfile: %s", e)
            db.session.rollback()
            return None

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
        cleaned = _clean_profile_name(name)
        if not cleaned:
            return None

        profile = SearchProfile.query.filter_by(name=cleaned).first()
        if profile:
            return profile

        canonical = _canonical_profile_name(cleaned)
        if canonical:
            existing = SearchProfile.query.all()
            for candidate in existing:
                if _canonical_profile_name(candidate.name) == canonical:
                    return candidate

        try:
            profile = SearchProfile(
                name=cleaned,
                description="Autocreated from Idealista saved search name",
                is_active=True,
                is_default=False,
                travel_targets=default_travel_targets_config(),
            )
            db.session.add(profile)
            db.session.commit()
            return profile
        except Exception as e:
            logger.warning("Failed to create SearchProfile %r: %s", cleaned, e)
            db.session.rollback()
            return SearchProfile.query.filter_by(name=cleaned).first()

    @staticmethod
    def merge_duplicate_profiles(commit: bool = True) -> Dict[str, Any]:
        """Merge profiles that normalize to the same canonical name."""

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

        for canonical, group in groups.items():
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
        }

    @staticmethod
    def resolve_profile(subject: str, body: str) -> Optional[SearchProfile]:
        """Pick a profile for an incoming email using profile.email_matchers.

        If no matchers match, falls back to the default profile.
        """
        # 1) Try to derive profile from the saved search name embedded in the email.
        search_name = extract_search_name(subject, body)
        if search_name:
            profile = SearchProfileService.get_or_create_profile_by_name(search_name)
            if profile:
                return profile

        # 2) Fallback: custom regex matchers.
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
