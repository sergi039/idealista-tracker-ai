"""Owner searches and retained ingestion subscriptions; never delete data."""

import hashlib
import json

from app import db
from models import Property, SearchProfile
from services.search_profile_service import SearchProfileService

CONFIG_FIELDS = (
    "criteria",
    "classification_rules",
    "travel_targets",
    "scoring_config",
    "ai_config",
)


def state(profile):
    config = profile.ui_config if isinstance(profile.ui_config, dict) else {}
    management = config.get("search_management")
    return management.get("state", "owner") if isinstance(management, dict) else "owner"


def set_state(profile, value):
    config = dict(profile.ui_config or {})
    management = dict(config.get("search_management") or {})
    management["state"] = value
    config["search_management"] = management
    profile.ui_config = config


def owner_search(profile):
    return profile.routed_to is None and state(profile) == "owner"


def _selection(source_ids, target_id, action, *, lock=False):
    if action not in {"merge", "remove", "promote", "technical"}:
        raise ValueError("invalid_selection")
    if (
        not source_ids
        or len(source_ids) > 100
        or any(type(i) is not int for i in source_ids)
    ):
        raise ValueError("invalid_selection")
    ids = set(source_ids)
    if action == "merge":
        if type(target_id) is not int or target_id in ids:
            raise ValueError("invalid_selection")
        ids.add(target_id)
    else:
        target_id = None
    query = SearchProfile.query.filter(SearchProfile.id.in_(sorted(ids))).order_by(
        SearchProfile.id
    )
    if lock:
        query = query.populate_existing().with_for_update()
    profiles = {p.id: p for p in query.all()}
    if len(profiles) != len(ids):
        raise ValueError("invalid_selection")
    sources = [profiles[i] for i in sorted(set(source_ids))]
    if any(p.is_default or p.routed_to is not None for p in sources):
        raise ValueError("protected_profile")
    target = profiles.get(target_id)
    if target and (
        target.is_default
        or not owner_search(target)
        or target.is_hidden
        or not target.is_active
    ):
        raise ValueError("invalid_target")
    if action == "merge":
        for source in sources:
            if (source.auto_route_from_pattern or "").strip():
                raise ValueError("source_carries_a_pattern")
            if SearchProfile.query.filter_by(routed_to=source.id).first():
                raise ValueError("source_is_a_route_target")
    rows = Property.query.filter(Property.search_profile_id.in_(sorted(ids))).order_by(
        Property.id
    )
    if lock:
        rows = rows.populate_existing().with_for_update()
    return sources, target, rows.all()


def preview(source_ids, target_id=None, action="merge", *, lock=False):
    sources, target, rows = _selection(source_ids, target_id, action, lock=lock)
    profiles = sources + ([target] if target else [])
    # Include complete rows: owner feedback or configuration changed after the
    # preview must never be accepted under an old confirmation. No payload is
    # embedded in the token; only this digest and the explicit selection.
    basis = {
        "profiles": [
            {c.name: getattr(p, c.name) for c in SearchProfile.__table__.columns}
            for p in profiles
        ],
        "properties": [
            {c.name: getattr(p, c.name) for c in Property.__table__.columns}
            for p in rows
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(basis, sort_keys=True, default=str).encode()
    ).hexdigest()
    source_set = {p.id for p in sources}
    moved = [p for p in rows if p.search_profile_id in source_set]
    conflicts = []
    if target:
        for source in sources:
            for field in CONFIG_FIELDS:
                value = getattr(source, field)
                if value and value != getattr(target, field):
                    conflicts.append(
                        {
                            "source": source.name,
                            "field": field,
                            "source_value": value,
                            "target_value": getattr(target, field),
                        }
                    )
    return {
        "sources": sources,
        "target": target,
        "action": action,
        "fingerprint": fingerprint,
        "conflicts": conflicts,
        "properties": len(moved),
        "favorites": sum(bool(p.is_favorite) for p in moved),
        "reviews": sum(
            p.owner_verdict not in (None, "undecided") or bool(p.owner_verdict_reason)
            for p in moved
        ),
    }


def apply(source_ids, target_id, action, fingerprint, *, acknowledge=False):
    """One confirmation, one transaction; routed_to has only its existing writer."""
    try:
        report = preview(source_ids, target_id, action, lock=True)
        if report["fingerprint"] != fingerprint:
            raise ValueError("stale_preview")
        if report["conflicts"] and not acknowledge:
            raise ValueError("acknowledge_conflicts")
        for source in report["sources"]:
            if action == "merge":
                result = SearchProfileService.route_profile(
                    source.id, target_id, commit=False
                )
                if result["status"] != "ok":
                    raise ValueError(result["reason"])
                set_state(source, "technical")
            elif action == "technical":
                set_state(source, "technical")
                source.is_hidden = True
            elif action == "remove":
                set_state(source, "deleted")
                source.is_hidden = True
                # Activity controls matcher-based ingestion too. Removing an
                # owner search must retain its previous routing behavior.
                # An already archived source must likewise stay archived.
            else:
                set_state(source, "owner")
                source.is_hidden = False
                source.is_active = True
        db.session.commit()
        return report
    except Exception:
        db.session.rollback()
        raise
