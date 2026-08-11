"""What a lookup will and will not accept as the place it is named after.

Lifted out of `services.property_travel_service` unchanged when the legacy
`Land` enrichment path had to apply the same rules (see
`EnrichmentService._airport_candidates`). Two copies of "what counts as an
airport" would drift, and the copy that drifts is the one nobody is looking
at -- which is exactly how the legacy path came to record 145 helipads as
airports while `/properties` refused them correctly.

Deliberately a leaf module: standard library only, no repo imports, so any
service can import it without an import cycle. The *patterns* still live with
the presets in `services.search_profile_service`; this is only the matcher.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class PlaceRules:
    """What a preset will and will not accept as its nearest place.

    Google's place types are broad: `airport` covers helipads and any business
    that claimed the tag, `hospital` covers dentists and cosmetic clinics, and
    the nearest such hit is routinely not the thing the preset is named after.
    A deny-list alone does not survive contact with the data -- refusing one
    tagged business just promotes the next one -- so a preset may also require
    the place to *say* what it is. Nothing qualifying nearby is reported as
    not found, which the scorer treats as absent rather than as zero.
    """

    require_name_patterns: Tuple[str, ...] = ()
    reject_name_patterns: Tuple[str, ...] = ()
    reject_types: Tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        payload = "#".join(
            (
                "|".join(self.require_name_patterns),
                "|".join(self.reject_name_patterns),
                "|".join(self.reject_types),
            )
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]

    def rejects(self, candidate: Dict[str, Any]) -> bool:
        name = str(candidate.get("name") or "").casefold()
        if self.require_name_patterns and not any(
            pattern in name for pattern in self.require_name_patterns
        ):
            return True
        if any(pattern in name for pattern in self.reject_name_patterns):
            return True
        candidate_types = candidate.get("types")
        if isinstance(candidate_types, list):
            lowered = {str(t).casefold() for t in candidate_types}
            if lowered & set(self.reject_types):
                return True
        return False


def place_rules_from(preset_def: Dict[str, Any]) -> Optional[PlaceRules]:
    if not isinstance(preset_def, dict):
        return None

    def _patterns(key: str) -> Tuple[str, ...]:
        value = preset_def.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(str(item).casefold() for item in value if str(item).strip())

    require = _patterns("require_name_patterns")
    reject_names = _patterns("reject_name_patterns")
    reject_types = _patterns("reject_types")
    if not require and not reject_names and not reject_types:
        return None
    return PlaceRules(
        require_name_patterns=require,
        reject_name_patterns=reject_names,
        reject_types=reject_types,
    )
