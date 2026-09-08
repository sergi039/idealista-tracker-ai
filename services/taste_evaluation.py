"""Leak-resistant manifest builder for later blind recommendation evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from services import taste_descriptors

SCHEMA_VERSION = 1


def entity_key(prop: Any) -> str:
    """Best stored entity identity, never a claim that two rows are distinct."""
    portal_id = getattr(prop, "idealista_property_id", None)
    if portal_id is not None:
        return f"idealista:{portal_id}"
    url = str(getattr(prop, "url", None) or "").strip()
    if url:
        parts = urlsplit(url)
        canonical = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
        )
        return f"url:{canonical}"
    source = str(getattr(prop, "source_email_id", None) or "").strip()
    return f"source:{source}" if source else f"row:{getattr(prop, 'id', '?')}"


def build_manifest(
    props: Iterable[Any],
    *,
    learning_property_ids: set[int],
    activity_property_ids: set[int],
    baseline: Mapping[int, Mapping[str, Any]] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Freeze an unlabeled candidate pool for a later owner rating.

    No verdict, favorite, activity, duplicate entity, or learning-set row can
    enter.  The absence of those signals is called ``no_recorded_activity``;
    it is never described as proof the owner has not seen the listing.
    """
    if not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    baseline = baseline or {}
    rows = sorted(props, key=lambda row: row.id)
    excluded_row_ids = learning_property_ids | activity_property_ids

    def excluded(prop: Any) -> bool:
        return (
            prop.id in excluded_row_ids
            or bool(getattr(prop, "is_favorite", False))
            or getattr(prop, "owner_verdict", None) is not None
        )

    # Every known-label/activity row reserves its entity before candidate
    # eligibility is considered. A row skipped for feedback does not make a
    # second representation of that already-seen listing safe for evaluation.
    seen_entities = {entity_key(prop) for prop in rows if excluded(prop)}
    candidates: list[dict[str, Any]] = []
    for prop in rows:
        if excluded(prop):
            continue
        key = entity_key(prop)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        descriptor = taste_descriptors.build_descriptor(prop)
        base = baseline.get(prop.id, {})
        candidates.append(
            {
                "property_id": prop.id,
                "entity_key_hash": hashlib.sha256(key.encode()).hexdigest(),
                "descriptor_fingerprint": descriptor["input_fingerprint"],
                "eligibility": "no_recorded_activity",
                "baseline": {
                    "taste": base.get("taste"),
                    "similar": base.get("similar"),
                },
                "new_ranking": base.get("new_ranking"),
            }
        )
        if len(candidates) == limit:
            break
    basis = json.dumps(candidates, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "basis_fingerprint": hashlib.sha256(basis.encode()).hexdigest(),
        "owner_labels_included": False,
        "utility_claim": "not_measured",
    }
