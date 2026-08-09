import re
from typing import Any, Dict, List, Optional, Tuple

from models import Property, SearchProfile
from services.search_profile_service import SearchProfileService

# Idealista titles read "<what it is> in <where it is>": "Land plot in
# Caserio Casa de Anes, 267, Siero", "Casa o chalet en venta en Siero". Only
# the head names the type; the tail is an address, and matching rules against
# the whole line let a street name decide the category -- that plot in
# "Caserio **Casa** de Anes" was filed as a house inside a land subscription.
_TITLE_LOCATION_SPLIT = re.compile(r"\s+(?:in|en)\s+", re.IGNORECASE)


class PropertyClassificationService:
    """Centralized category/subtype classification for Properties (regex-driven)."""

    @staticmethod
    def title_head(title: Any) -> str:
        """The part of a listing title before its address.

        A title with no location separator is its own head, so this only ever
        narrows the text -- and `classify_property` still falls back to the
        full title, which is what keeps the phrases a head cannot carry on its
        own ("Solar **en venta** en Sevilla") classified exactly as before.
        """
        return _TITLE_LOCATION_SPLIT.split(str(title or ""), maxsplit=1)[0].strip()

    @staticmethod
    def classify_text(
        text: str, rules: List[Dict[str, Any]]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Apply ordered regex rules to text, returning (category, subtype)."""
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern")
            if not pattern:
                continue
            try:
                if re.search(pattern, text or "", re.IGNORECASE):
                    return rule.get("category"), rule.get("subtype")
            except re.error:
                continue
        return None, None

    @classmethod
    def classify_property(
        cls, prop: Property, profile: Optional[SearchProfile] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Classify a Property using profile-specific rules (fallback to global defaults)."""
        rules = SearchProfileService.get_classification_rules(profile)
        texts = [
            # The type first, before any address can claim a rule.
            cls.title_head(prop.title),
            str(prop.title or ""),
            str(prop.email_subject or ""),
            str(prop.description or ""),
        ]
        for text in texts:
            category, subtype = cls.classify_text(text, rules)
            if category:
                return category, subtype
        return None, None

    @classmethod
    def apply_classification(
        cls,
        prop: Property,
        profile: Optional[SearchProfile] = None,
        *,
        update_area_type: bool = True,
        allow_clear: bool = False,
        respect_lock: bool = True,
    ) -> bool:
        """Update Property fields in-place and return True if anything changed."""
        if not prop:
            return False

        if respect_lock:
            attrs = prop.attributes if isinstance(prop.attributes, dict) else {}
            if bool(attrs.get("classification_locked")):
                return False

        category, subtype = cls.classify_property(prop, profile)
        changed = False

        if category:
            if (prop.property_category or "") != category:
                prop.property_category = category
                changed = True
        elif allow_clear and prop.property_category is not None:
            prop.property_category = None
            changed = True

        if subtype:
            if (prop.property_subtype or "") != subtype:
                prop.property_subtype = subtype
                changed = True
        elif allow_clear and prop.property_subtype is not None:
            prop.property_subtype = None
            changed = True

        if update_area_type:
            if (prop.property_category or "").strip().lower() == "land":
                if prop.area_type != "plot":
                    prop.area_type = "plot"
                    changed = True
            elif prop.area is not None:
                if (prop.area_type or "unknown").strip().lower() in ("", "unknown"):
                    prop.area_type = "built"
                    changed = True
            else:
                if not prop.area_type:
                    prop.area_type = "unknown"
                    changed = True

        return changed
