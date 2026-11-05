"""Pilot import -> prepared evidence -> real recommendation/held-out selection."""

import copy
import hashlib
import json
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import (
    favorite_similarity,
    recommendation_feedback as feedback,
    taste_recommendation,
    taste_service,
)
from tests import setup_test_environment
from utils.review_json import canonical


@pytest.fixture
def scope(tmp_path):
    setup_test_environment()
    app = create_app()
    with app.app_context():
        db.create_all()
        profile = SearchProfile(name="Pilot")
        other = SearchProfile(name="Other")
        db.session.add_all([profile, other])
        db.session.flush()
        rows = []
        for i in range(6):
            row = Property(
                source_email_id=f"pilot:{i}",
                title="Casa independiente",
                price=275000 if i != 4 else 50000,
                area=275,
                area_type="built",
                property_category="housing",
                property_subtype="house",
                search_profile_id=other.id if i == 5 else profile.id,
                municipality="Laxe",
                location_lat=43.2,
                location_lon=-8.9,
                location_accuracy="precise",
                url=f"https://www.idealista.com/inmueble/{100 + i}/",
                enrichment={"unrelated": {"keep": True}},
            )
            db.session.add(row)
            rows.append(row)
        db.session.commit()
        db.session.add(
            TasteProfile(
                provider="fixture",
                model="none",
                signals_fingerprint="0" * 64,
                profile={"likes": [{"trait": "existing owner words"}]},
                source={"signals": []},
            )
        )
        db.session.commit()
        original = feedback.import_batch
        counter = iter(range(100))

        def imported(*args, **kwargs):
            if kwargs.get("apply"):
                kwargs.setdefault(
                    "snapshot_path", tmp_path / f"receipt-{next(counter)}.json"
                )
            return original(*args, **kwargs)

        with patch.object(feedback, "import_batch", imported):
            yield app, profile, rows
        db.session.remove()
        db.drop_all()


def batch(profile, rows, *, alias=False):
    cards = [
        {
            "id": rows[0].id,
            "row_ids": [rows[0].id, rows[2].id] if alias else [rows[0].id],
            "similarity": 90,
            "rank": 1,
            "url": rows[0].url,
            "source_urls": [
                {"url": row.url} for row in ([rows[0], rows[2]] if alias else [rows[0]])
            ],
        },
        {
            "id": rows[1].id,
            "row_ids": [rows[1].id],
            "similarity": 85,
            "rank": 2,
            "url": rows[1].url,
        },
    ]
    manifest = {
        "batch_id": "synthetic-pilot",
        "protocol": {"source_profile_id": profile.id},
        "candidates": cards,
    }
    answers = {
        "schema_version": 1,
        "artifact_kind": "idealista_owner_batch_feedback",
        "batch_id": manifest["batch_id"],
        "batch_fingerprint": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
        "answer_meaning": feedback.MEANING,
        "answers": [
            {
                "property_id": c["id"],
                "row_ids": c["row_ids"],
                "baseline": {"similarity": c["similarity"], "rank": c["rank"]},
                "answer": "yes" if i == 0 else "no",
                "reason": "",
                "updated_at": "2026-11-04T13:00:00Z",
            }
            for i, c in enumerate(cards)
        ],
    }
    return manifest, answers


def test_import_is_scoped_atomic_idempotent_and_preserves_purchase_state(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    assert (
        feedback.import_batch(manifest, answers, profile_id=profile.id)["changed"] == 0
    )
    assert feedback.read_feedback(rows[0]) is None
    result = feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert result == {"ratings": 2, "aliases": 3, "changed": 3, "applied": True}
    assert (
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)[
            "changed"
        ]
        == 0
    )
    for row in rows:
        assert row.owner_verdict is None and row.is_favorite is False
        assert row.enrichment["unrelated"] == {"keep": True}
    assert feedback.read_feedback(rows[2])["property_id"] == rows[0].id
    changed = copy.deepcopy(answers)
    changed["answers"][0]["answer"] = "no"
    with pytest.raises(ValueError, match="another rating"):
        feedback.import_batch(manifest, changed, profile_id=profile.id, apply=True)
    assert feedback.read_feedback(rows[0])["answer"] == "yes"


def test_invalid_scope_or_baseline_writes_nothing(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows)
    answers["answers"][1]["baseline"]["rank"] = 999
    with pytest.raises(ValueError):
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert all(feedback.read_feedback(row) is None for row in rows)


@pytest.mark.parametrize("changed_index", [0, 2])
def test_listing_identity_changed_after_freeze_cannot_receive_old_rating(
    scope, changed_index
):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    rows[changed_index].url = "https://www.idealista.com/inmueble/999/"
    db.session.commit()
    with pytest.raises(ValueError, match="listing URL"):
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert all(feedback.read_feedback(row) is None for row in rows)


def test_tracking_query_change_keeps_the_same_frozen_listing(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows)
    rows[0].url += "?utm_source=alert&utm_campaign=example"
    db.session.commit()
    feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert feedback.read_feedback(rows[0])["answer"] == "yes"
    rows[0].url += "&utm_medium=email"
    db.session.commit()
    assert feedback.read_feedback(rows[0])["answer"] == "yes"
    assert (
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)[
            "changed"
        ]
        == 0
    )


def test_inactive_old_listing_feedback_is_not_silently_overwritten(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows)
    feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    stored = copy.deepcopy(rows[0].enrichment[feedback.KEY])
    rows[0].url = "https://www.idealista.com/inmueble/999/"
    db.session.commit()
    assert feedback.read_feedback(rows[0]) is None
    new_manifest, new_answers = batch(profile, rows)
    with pytest.raises(ValueError, match="another rating"):
        feedback.import_batch(
            new_manifest, new_answers, profile_id=profile.id, apply=True
        )
    assert rows[0].enrichment[feedback.KEY] == stored


def test_frozen_fingerprint_and_mid_write_failure_leave_no_partial_import(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["candidates"][0]["url"] = "https://example.org/changed"
    with pytest.raises(ValueError, match="frozen pilot"):
        feedback.import_batch(
            changed_manifest, answers, profile_id=profile.id, apply=True
        )
    original = feedback.merge_json_object_changes_under_lock
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected write failure")
        return original(*args, **kwargs)

    with patch.object(feedback, "merge_json_object_changes_under_lock", fail_second):
        with pytest.raises(RuntimeError, match="injected"):
            feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    db.session.expire_all()
    assert calls == 2
    assert all(feedback.read_feedback(row) is None for row in rows)
    manifest, answers = batch(profile, [rows[0], rows[5]])
    with pytest.raises(ValueError, match="subscription"):
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert all(feedback.read_feedback(row) is None for row in rows)


def test_feedback_refresh_and_ranking_need_no_model_and_do_not_invent_clauses(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    answers["answers"][1]["reason"] = "фейк генерация"
    feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    with patch(
        "services.subscription_transport.complete",
        side_effect=AssertionError("no model call"),
    ):
        current = taste_service.refresh_feedback_snapshot()["data"]
        assert current["source"]["positive_reference_ids"] == [rows[0].id]
        assert current["source"]["clauses"] == []
        assert current["profile"]["likes"] == [{"trait": "existing owner words"}]
        summary = taste_service.recommendation_profile_state(current)
        assert summary["state"] == "current"
        ctx = taste_recommendation.build_context(
            rows,
            current,
            summary,
            similarity_ctx=favorite_similarity.build_context([profile.id]),
        )
        assert ctx.readings[rows[1].id]["eligibility"] == "excluded"
        assert (
            ctx.readings[rows[3].id]["nearest_positive_reference"]["id"] == rows[0].id
        )
        assert (
            ctx.readings[rows[3].id]["rank_value"]
            > ctx.readings[rows[4].id]["rank_value"]
        )
        assert ctx.readings[rows[5].id]["nearest_positive_reference"] is None
        assert taste_service.refresh_feedback_snapshot()["changed"] is False
        assert favorite_similarity.build_context([profile.id]) is None
        assert all(
            row.owner_verdict is None and row.is_favorite is False for row in rows
        )


def test_hazard_scan_is_not_evidence_of_residential_neighbours(scope):
    _, _, rows = scope
    row = rows[0]
    row.location_accuracy = "approximate"
    row.enrichment = {
        "hazards": {
            "status": "ok",
            "origin": {"lat": row.location_lat, "lon": row.location_lon},
            "searched_m": 5984,
            "item_count": 1,
            "truncated": False,
            "items": [
                {"kind": "quarry", "severity": "moderate", "origin_distance_m": 3000}
            ],
        }
    }
    facts = taste_service.gather_facts(row)
    assert not any(line.startswith("INDUSTRIAL NEIGHBOURS") for line in facts)
    scan = next(line for line in facts if line.startswith("REGIONAL HAZARD SCAN"))
    assert "5,984 m" in scan and "5,000 m" in scan and "NOT a count" in scan
    assert any("0 m to 8,000 m" in line for line in facts)
    row.location_lat += 1
    assert any("stale_origin" in line for line in taste_service.gather_facts(row))


def test_restore_preserves_other_writes_and_refuses_partial_rollback(scope, tmp_path):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    receipt = tmp_path / "rollback.json"
    feedback.import_batch(
        manifest, answers, profile_id=profile.id, apply=True, snapshot_path=receipt
    )
    snapshot = json.loads(receipt.read_text())
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert feedback.restore_batch(snapshot)["rows"] == 3
    for row in rows:
        row.enrichment = {**row.enrichment, "later": 123}
    changed = copy.deepcopy(rows[1].enrichment)
    changed[feedback.KEY]["reason"] = "later edit"
    rows[1].enrichment = changed
    db.session.commit()
    with pytest.raises(ValueError, match="changed since import"):
        feedback.restore_batch(snapshot, apply=True)
    assert feedback.read_feedback(rows[0])
    rows[1].enrichment = {
        **rows[1].enrichment,
        feedback.KEY: next(
            item["after"]["value"]
            for item in snapshot["changes"]
            if item["id"] == rows[1].id
        ),
    }
    db.session.commit()
    assert feedback.restore_batch(snapshot, apply=True)["restored"] == 3
    assert all(feedback.read_feedback(row) is None for row in rows)
    assert all(row.enrichment["later"] == 123 for row in rows)
    with pytest.raises(ValueError, match="changed since import"):
        feedback.restore_batch(snapshot, apply=True)


def test_receipt_failure_rolls_back_every_change(scope, tmp_path):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows)
    receipt = tmp_path / "occupied.json"
    receipt.write_text("do not replace")
    with pytest.raises(FileExistsError):
        feedback.import_batch(
            manifest, answers, profile_id=profile.id, apply=True, snapshot_path=receipt
        )
    assert all(feedback.read_feedback(row) is None for row in rows)
    assert receipt.read_text() == "do not replace"


def test_only_completed_checkpoint_is_importable():
    from utils.import_recommendation_feedback import completed_feedback

    for value in (
        {},
        {"ok": True, "completed_at": None},
        {
            "ok": True,
            "completed_at": "2026-11-04T13:05:34Z",
            "feedback": {"answers": [{"answer": None}]},
        },
    ):
        with pytest.raises(ValueError):
            completed_feedback(value)


def test_binary_no_is_hidden_only_from_recommendation_page_and_csv(scope):
    from tests.test_recommendation_export_agrees import _page_and_export

    app, profile, rows = scope
    manifest, answers = batch(profile, rows)
    manifest["candidates"][1]["row_ids"] = [rows[1].id, rows[4].id]
    manifest["candidates"][1]["source_urls"] = [{"url": rows[4].url}]
    answers["answers"][1]["row_ids"] = [rows[1].id, rows[4].id]
    answers["batch_fingerprint"] = hashlib.sha256(
        canonical(manifest).encode()
    ).hexdigest()
    feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    taste_service.refresh_feedback_snapshot()
    client = app.test_client()
    normal, normal_csv = _page_and_export(client, mode="all", profile_id=profile.id)
    recommended, exported = _page_and_export(
        client, mode="recommendation", profile_id=profile.id
    )
    assert set(recommended) == set(exported)
    for row in (rows[1], rows[4]):
        assert row.id in normal and row.id in normal_csv
        assert row.id not in recommended and row.id not in exported
        assert row.owner_verdict is None and not row.is_favorite


def test_missing_primary_uses_only_verified_surviving_frozen_alias_once(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows, alias=True)
    missing = rows[0].id
    db.session.delete(rows[0])
    db.session.commit()
    report = feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert report["ratings"] == 2 and report["aliases"] == 2
    assert report["missing_alias_ids"] == [missing]
    stored = feedback.read_feedback(rows[2])
    assert (
        stored["property_id"] == missing
        and stored["reference_property_id"] == rows[2].id
    )
    current = taste_service.refresh_feedback_snapshot()["data"]
    assert current["source"]["positive_reference_ids"] == [rows[2].id]
    assert (
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)[
            "changed"
        ]
        == 0
    )
    rows[2].url = "https://www.idealista.com/inmueble/9999/"
    db.session.commit()
    with pytest.raises(ValueError, match="listing URL"):
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)


def test_card_without_any_surviving_alias_refuses_whole_batch(scope):
    _, profile, rows = scope
    manifest, answers = batch(profile, rows)
    db.session.delete(rows[0])
    db.session.commit()
    with pytest.raises(ValueError, match="no surviving"):
        feedback.import_batch(manifest, answers, profile_id=profile.id, apply=True)
    assert feedback.read_feedback(rows[1]) is None
