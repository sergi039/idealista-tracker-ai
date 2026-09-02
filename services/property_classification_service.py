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

# What a listing IS, for the similarity reading (services/favorite_similarity
# .py): category and subtype folded into one word. `land` is one kind
# whatever the legacy `developed` word from utils/email_parser.py says --
# docs/PROPERTY_TYPES.md knows `plot` as land's only subtype, and sub 17
# stars 14 plots beside 2 "developed" parcels. A housing row without a
# subtype has no kind: it is compared, never gated (#98).
LAND_SUBTYPES = ("plot", "developed")
KIND_LAND = "land"
KIND_HOUSE = "house"


def listing_kind(category: Any, subtype: Any) -> Optional[str]:
    """One word for what the listing is, or None when nothing says."""
    category_word = str(category or "").strip().lower()
    subtype_word = str(subtype or "").strip().lower()
    if category_word == "land" or subtype_word in LAND_SUBTYPES:
        return KIND_LAND
    if subtype_word == KIND_HOUSE:
        return KIND_HOUSE
    if subtype_word:
        return subtype_word
    return None


# The house typology the title head states, on the owner's own definition
# (/agencies, #474): "Detached house = idealista's chalet independiente +
# casa rustica (casa de pueblo / rural / casona); adosados and pareados are
# excluded". Read from `title_head` only, never from the address (#223: a
# street called Pareada is not a terrace). A bare "Chalet" or "Casa", the
# yaencontre shape, states nothing and reads None.
TYPOLOGY_DETACHED = "detached"
TYPOLOGY_ATTACHED = "attached"
_DETACHED_RE = re.compile(
    r"chalet\s+independiente|casa\s+independiente|independiente|casa\s+rural|"
    r"casa\s+de\s+pueblo|casona|casa\s+r[uú]stica|casa\s+de\s+campo",
    re.IGNORECASE,
)
_ATTACHED_RE = re.compile(
    r"adosad[ao]|paread[ao]|terraced|semi-?detached|townhouse|end\s+of\s+terrace",
    re.IGNORECASE,
)


def house_typology(title: Any) -> Optional[str]:
    """`detached`, `attached`, or None when the title head states neither.
    An attached word wins over a detached one in the same head ("chalet
    independiente pareado" is not a thing anybody writes, but a head that
    says both is not evidence of either)."""
    head = PropertyClassificationService.title_head(title)
    if not head:
        return None
    attached = bool(_ATTACHED_RE.search(head))
    detached = bool(_DETACHED_RE.search(head))
    if attached and detached:
        return None
    if attached:
        return TYPOLOGY_ATTACHED
    if detached:
        return TYPOLOGY_DETACHED
    return None


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
    def classify_sources(
        cls,
        title: Any,
        subject: Any,
        body: Any,
        rules: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[str]]:
        """The order every caller classifies in: type first, address never.

        Ingestion used to run its own sequence over the raw title, so a plot in
        "Caserio **Casa** de Anes" was stored as housing while the manual
        reclassify tool called it land (#223). One order, one place.
        """
        texts = [
            # The type first, before any address can claim a rule.
            cls.title_head(title),
            str(title or ""),
            str(subject or ""),
            str(body or ""),
        ]
        for text in texts:
            category, subtype = cls.classify_text(text, rules)
            if category:
                return category, subtype
        return None, None

    @classmethod
    def classify_property(
        cls, prop: Property, profile: Optional[SearchProfile] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Classify a Property using profile-specific rules (fallback to global defaults)."""
        return cls.classify_sources(
            prop.title,
            prop.email_subject,
            prop.description,
            SearchProfileService.get_classification_rules(profile),
        )

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

        if update_area_type and cls.reconcile_area_type(prop):
            changed = True

        return changed

    @staticmethod
    def reconcile_area_type(prop: Property) -> bool:
        """Make `area_type` agree with `property_category`, in place.

        Land is measured as a plot, whatever anyone wrote in `area_type`
        first. Kept as its own method because the portal doors build a
        Property without ever calling `apply_classification`, and a second
        copy of the rule is how the two came to disagree: production carried
        rows classified `land` still holding `area_type='built'`, so the
        parcel was counted as floor space.
        """
        if (prop.property_category or "").strip().lower() == "land":
            if prop.area_type != "plot":
                prop.area_type = "plot"
                return True
            return False
        if prop.area is not None:
            if (prop.area_type or "unknown").strip().lower() in ("", "unknown"):
                prop.area_type = "built"
                return True
            return False
        if not prop.area_type:
            prop.area_type = "unknown"
            return True
        return False
