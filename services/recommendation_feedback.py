"""Owner pilot ratings, separate from purchase verdicts and favorites.

The importer is explicit, scoped and atomic. Existing JSON write locks protect
the feedback block just as they protect every other enrichment writer.
No network calls, model calls or purchase-state writes occur here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models import Property, db
from services.dossier import normalise_url
from services.enrichment_write import merge_json_object_changes_under_lock
from utils.review_json import canonical, valid_reason, valid_timestamp

KEY = "recommendation_feedback"
EXPOSURE_KEY = "recommendation_exposure"
MEANING = "recommendation_appeal_not_purchase_verdict"


def _listing_identity(value: Any) -> str | None:
    """Match frozen listing URLs, ignoring only tracking and fragment changes.

    Unlike held-out deduplication, a meaningful query remains part of this
    write guard: changing ?id=100 into ?id=999 must not transfer an old vote.
    """
    url = normalise_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ]
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def has_recorded_feedback(prop: Any) -> bool:
    """Historical exposure, even when a retained receipt no longer applies.

    A blind test must fail closed on an occupied feedback namespace. URL/scope
    changes or malformed legacy metadata cannot prove the owner never reviewed
    this row. This does not make that feedback valid for current ranking.
    """
    enrichment = getattr(prop, "enrichment", None)
    return isinstance(enrichment, dict) and (
        enrichment.get(KEY) is not None or enrichment.get(EXPOSURE_KEY) is not None
    )


def exposed_alias_ids(prop: Any) -> set[int]:
    """Frozen identities stay seen even after an algorithmic feedback rollback."""
    enrichment = getattr(prop, "enrichment", None)
    if not isinstance(enrichment, dict):
        return set()
    ids = set()
    for key in (KEY, EXPOSURE_KEY):
        record = enrichment.get(key)
        if isinstance(record, dict) and isinstance(record.get("row_ids"), list):
            ids.update(i for i in record["row_ids"] if type(i) is int and i > 0)
    return ids


def read_feedback(prop: Any) -> dict[str, Any] | None:
    enrichment = getattr(prop, "enrichment", None)
    item = enrichment.get(KEY) if isinstance(enrichment, dict) else None
    if not isinstance(item, dict):
        return None
    if (
        item.get("schema_version") != 1
        or item.get("answer_meaning") != MEANING
        or item.get("answer") not in ("yes", "no")
        or item.get("profile_id") != getattr(prop, "search_profile_id", None)
        or not isinstance(item.get("row_ids"), list)
        or getattr(prop, "id", None) not in item["row_ids"]
        or not _listing_identity(item.get("listing_url"))
        or _listing_identity(item.get("listing_url"))
        != _listing_identity(getattr(prop, "url", None))
        or not valid_reason(item.get("reason"))
        or type(item.get("property_id")) is not int
        or item["property_id"] not in item["row_ids"]
        or not isinstance(item.get("batch_id"), str)
        or not isinstance(item.get("source_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", item["source_sha256"])
    ):
        return None
    return copy.deepcopy(item)


def validate_batch(
    manifest: Any, feedback: Any, profile_id: int
) -> list[dict[str, Any]]:
    """Bind every rating to its frozen card/aliases/baseline before any write."""
    if (
        not isinstance(manifest, dict)
        or not isinstance(feedback, dict)
        or feedback.get("schema_version") != 1
        or feedback.get("artifact_kind") != "idealista_owner_batch_feedback"
        or feedback.get("answer_meaning") != MEANING
        or feedback.get("batch_id") != manifest.get("batch_id")
        or feedback.get("media_revision", "original")
        != manifest.get("media_revision", "original")
        or manifest.get("protocol", {}).get("source_profile_id") != profile_id
        or not isinstance(feedback.get("batch_fingerprint"), str)
        or feedback["batch_fingerprint"]
        != hashlib.sha256(canonical(manifest).encode()).hexdigest()
    ):
        raise ValueError("feedback does not name this profile/frozen pilot")
    candidates = manifest.get("candidates")
    answers = feedback.get("answers")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 100:
        raise ValueError("a pilot must contain 1..100 cards")
    if not isinstance(answers, list) or len(answers) != len(candidates):
        raise ValueError("one completed answer is required for every card")
    if any(not isinstance(c, dict) or type(c.get("id")) is not int for c in candidates):
        raise ValueError("invalid frozen cards")
    cards = {c["id"]: c for c in candidates}
    if len(cards) != len(candidates):
        raise ValueError("duplicate frozen cards")
    seen: set[int] = set()
    aliases: set[int] = set()
    records = []
    source_hash = digest(feedback)
    for answer in answers:
        if not isinstance(answer, dict):
            raise ValueError("invalid answer")
        ident = answer.get("property_id")
        if type(ident) is not int or ident in seen or ident not in cards:
            raise ValueError("unknown or duplicate answer")
        seen.add(ident)
        card = cards[ident]
        frozen_url = _listing_identity(card.get("url"))
        sources = card.get("source_urls", [])
        if not frozen_url or not isinstance(sources, list):
            raise ValueError("frozen card needs a listing URL and source URL list")
        alias_urls = {
            identity
            for item in sources
            if (
                identity := _listing_identity(
                    item.get("url") if isinstance(item, dict) else item
                )
            )
        } | {frozen_url}
        row_ids = sorted(card.get("row_ids", [ident]))
        if (
            any(type(i) is not int or i < 1 for i in row_ids)
            or ident not in row_ids
            or len(set(row_ids)) != len(row_ids)
            or aliases.intersection(row_ids)
            or answer.get("row_ids") != row_ids
            or answer.get("baseline")
            != {"similarity": card.get("similarity"), "rank": card.get("rank")}
            or answer.get("answer") not in {"yes", "no"}
            or not valid_reason(answer.get("reason"))
        ):
            raise ValueError("answer aliases/baseline/value do not match its card")
        aliases.update(row_ids)
        stamp = answer.get("updated_at")
        if not valid_timestamp(stamp):
            raise ValueError("invalid answer timestamp")
        records.append(
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "batch_id": feedback["batch_id"],
                "batch_fingerprint": feedback["batch_fingerprint"],
                "answer_meaning": MEANING,
                "source_sha256": source_hash,
                "property_id": ident,
                "row_ids": row_ids,
                "answer": answer["answer"],
                "reason": answer["reason"],
                "updated_at": stamp,
                "baseline": answer["baseline"],
                "frozen_listing_url": frozen_url,
                "frozen_alias_urls": sorted(alias_urls),
            }
        )
    return records


def import_batch(
    manifest: Any,
    feedback: Any,
    *,
    profile_id: int,
    apply: bool = False,
    snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    records = validate_batch(manifest, feedback, profile_id)
    ids = sorted({i for record in records for i in record["row_ids"]})
    query = Property.query.filter(Property.id.in_(ids)).order_by(Property.id)
    try:
        rows = (
            query.with_for_update().populate_existing().all() if apply else query.all()
        )
        by_id = {row.id: row for row in rows}
        if any(row.search_profile_id != profile_id for row in rows):
            raise ValueError("all aliases must exist in the selected subscription")
        for record in records:
            surviving = sorted(set(record["row_ids"]) & by_id.keys())
            if not surviving:
                raise ValueError(
                    f"#{record['property_id']} has no surviving frozen alias"
                )
            for ident in surviving:
                row = by_id[ident]
                allowed = (
                    {record["frozen_listing_url"]}
                    if ident == record["property_id"]
                    else set(record["frozen_alias_urls"])
                )
                if _listing_identity(row.url) not in allowed:
                    raise ValueError(
                        f"#{ident} listing URL changed since the frozen pilot"
                    )
                previous = read_feedback(row)
                stored = (row.enrichment or {}).get(KEY)
                exposure = (row.enrichment or {}).get(EXPOSURE_KEY)
                if exposure is not None and (
                    not isinstance(exposure, dict)
                    or not isinstance(exposure.get("row_ids"), list)
                    or any(type(i) is not int or i < 1 for i in exposure["row_ids"])
                ):
                    raise ValueError(f"#{ident} has unreadable exposure history")
                if stored is not None and (
                    previous is None
                    or previous["source_sha256"] != record["source_sha256"]
                ):
                    raise ValueError(
                        f"#{ident} already has another rating; preserve its history before replacement"
                    )
        changed = 0
        changes = []
        if apply:
            if snapshot_path is None:
                raise ValueError("apply requires a new rollback snapshot path")
            for record in records:
                for ident in record["row_ids"]:
                    if ident not in by_id:
                        continue
                    row = by_id[ident]
                    if read_feedback(row):
                        continue
                    base = copy.deepcopy(row.enrichment or {})
                    item = {
                        **record,
                        "listing_url": row.url,
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    }
                    changes.append(
                        {
                            "id": ident,
                            "profile_id": profile_id,
                            "listing_url": row.url,
                            "before": {"present": KEY in base, "value": base.get(KEY)},
                            "after": {"present": True, "value": item},
                        }
                    )
                    # Rolling back the ranking signal cannot undo the fact
                    # that the owner has already seen this frozen card.
                    updated = {**base, KEY: item}
                    if base.get(EXPOSURE_KEY) is None:
                        updated[EXPOSURE_KEY] = {
                            name: record[name]
                            for name in (
                                "schema_version",
                                "batch_id",
                                "property_id",
                                "row_ids",
                                "source_sha256",
                            )
                        }
                    else:
                        updated[EXPOSURE_KEY] = {
                            **base[EXPOSURE_KEY],
                            "row_ids": sorted(
                                set(base[EXPOSURE_KEY]["row_ids"])
                                | set(record["row_ids"])
                            ),
                        }
                    merge_json_object_changes_under_lock(
                        row, "enrichment", base=base, updated=updated
                    )
                    changed += 1
            _write_snapshot(
                snapshot_path, {"schema_version": 1, "kind": KEY, "changes": changes}
            )
            db.session.commit()
        return {
            "ratings": len(records),
            "aliases": len(by_id),
            "changed": changed,
            "applied": apply,
            **(
                {"missing_alias_ids": sorted(set(ids) - by_id.keys())}
                if set(ids) != set(by_id)
                else {}
            ),
        }
    except Exception:
        db.session.rollback()
        raise


def _write_snapshot(path: str | Path, payload: dict[str, Any]) -> None:
    """Exclusive durable receipt before commit; an existing receipt is never replaced."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(str(Path(path).resolve().parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def restore_batch(snapshot: Any, *, apply: bool = False) -> dict[str, Any]:
    """Restore only our block, atomically, while every expected write still holds."""
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != 1
        or snapshot.get("kind") != KEY
        or not isinstance(snapshot.get("changes"), list)
    ):
        raise ValueError("invalid feedback rollback snapshot")
    changes = snapshot["changes"]
    ids = [item["id"] for item in changes]
    if any(type(ident) is not int for ident in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid rollback rows")
    try:
        query = Property.query.filter(Property.id.in_(ids)).order_by(Property.id)
        rows = (
            query.with_for_update().populate_existing().all() if apply else query.all()
        )
        by_id = {row.id: row for row in rows}
        for item in changes:
            if item["id"] not in by_id:
                continue
            row = by_id[item["id"]]
            block = row.enrichment or {}
            if (
                row.search_profile_id != item["profile_id"]
                or row.url != item["listing_url"]
                or {"present": KEY in block, "value": block.get(KEY)} != item["after"]
            ):
                raise ValueError(f"#{row.id} changed since import; rollback refused")
        if apply:
            for item in changes:
                if item["id"] not in by_id:
                    continue
                row = by_id[item["id"]]
                base = copy.deepcopy(row.enrichment or {})
                updated = copy.deepcopy(base)
                if item["before"]["present"]:
                    updated[KEY] = item["before"]["value"]
                else:
                    updated.pop(KEY, None)
                merge_json_object_changes_under_lock(
                    row, "enrichment", base=base, updated=updated
                )
            db.session.commit()
        return {
            "restored": len(rows) if apply else 0,
            "rows": len(rows),
            "applied": apply,
            **(
                {"missing_alias_ids": sorted(set(ids) - by_id.keys())}
                if set(ids) != set(by_id)
                else {}
            ),
        }
    except Exception:
        db.session.rollback()
        raise
