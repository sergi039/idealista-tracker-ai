"""Owner operations retain real imported receipts and the original identities."""

import copy
import re
from unittest.mock import patch

import pytest

from app import db
from models import Property, SearchProfile, PropertyActivity
from services import (
    owner_review,
    recommendation_feedback as feedback,
    search_management as management,
)
from services.search_profile_service import SearchProfileService
from tests.test_recommendation_feedback import scope as feedback_scope, batch

scope = feedback_scope


def confirmation(client, ids, *, action="merge", target=None):
    response = client.get(
        "/profiles/manage",
        query_string={"source_id": ids, "target_id": target, "action": action},
    )
    assert response.status_code == 200
    return re.search(r'name="preview_token" value="([^"]+)"', response.text).group(1)


def test_merge_keeps_owner_history_and_projects_imported_receipts(scope):
    app, source, rows = scope
    target = db.session.get(SearchProfile, rows[5].search_profile_id)
    manifest, answers = batch(source, rows, alias=True)
    feedback.import_batch(manifest, answers, profile_id=source.id, apply=True)
    owner_review.set_review(
        rows[0], decision="interested", reason="Water and view", action="Call"
    )
    rows[0].is_favorite = True
    db.session.commit()
    before = [copy.deepcopy(row.enrichment) for row in rows[:3]]
    history = [(r.id, copy.deepcopy(r.snapshot)) for r in PropertyActivity.query.all()]
    ids = [r.id for r in rows]
    report = management.preview([source.id], target.id)
    management.apply(
        [source.id], target.id, "merge", report["fingerprint"], acknowledge=True
    )
    assert [r.id for r in Property.query.order_by(Property.id)] == ids
    assert rows[0].is_favorite and rows[0].owner_verdict_reason == "Water and view"
    assert rows[0].next_action == "Call"
    assert [(r.id, r.snapshot) for r in PropertyActivity.query.all()] == history
    assert source.routed_to == target.id and source.is_hidden
    for row, previous in zip(rows[:3], before):
        assert row.enrichment[feedback.KEY] == previous[feedback.KEY]
        assert row.enrichment[feedback.EXPOSURE_KEY] == previous[feedback.EXPOSURE_KEY]
        assert feedback.read_feedback(row)["answer"] == previous[feedback.KEY]["answer"]
        assert (
            target.id,
            feedback._listing_identity(row.url),
        ) in feedback.listing_reservations(row)
    alias = Property(
        source_email_id="newalias", search_profile_id=target.id, url=rows[1].url
    )
    assert (
        feedback.feedback_for_listing(alias, feedback.negative_listing_feedback(rows))[
            "answer"
        ]
        == "no"
    )
    rows[0].url = "https://www.idealista.com/inmueble/999999/"
    assert feedback.read_feedback(rows[0]) is None


def test_unrecorded_manual_move_does_not_transfer_feedback(scope):
    _, source, rows = scope
    manifest, answers = batch(source, rows)
    feedback.import_batch(manifest, answers, profile_id=source.id, apply=True)
    rows[0].search_profile_id = rows[5].search_profile_id
    assert feedback.read_feedback(rows[0]) is None


@pytest.mark.parametrize("change", ["feedback", "criteria", "new_listing"])
def test_stale_preview_cannot_mutate_anything(scope, change):
    _, source, rows = scope
    target_id = rows[5].search_profile_id
    report = management.preview([source.id], target_id)
    if change == "feedback":
        rows[0].owner_verdict_reason = "New owner answer"
    elif change == "criteria":
        source.criteria = {"max_price_eur": 200000}
    else:
        db.session.add(Property(source_email_id="late", search_profile_id=source.id))
    db.session.commit()
    with pytest.raises(ValueError, match="stale_preview"):
        management.apply([source.id], target_id, "merge", report["fingerprint"])
    assert source.routed_to is None
    assert rows[0].search_profile_id == source.id


def test_multi_merge_is_atomic_on_late_failure(scope):
    _, source, rows = scope
    second = SearchProfile(name="Second source")
    db.session.add(second)
    db.session.commit()
    target_id = rows[5].search_profile_id
    preview = management.preview([source.id, second.id], target_id)
    original = SearchProfileService.route_profile

    def fail_second(source_id, target_id, commit):
        return (
            original(source_id, target_id, commit)
            if source_id == source.id
            else {"status": "refused", "reason": "changed"}
        )

    with patch.object(SearchProfileService, "route_profile", fail_second):
        with pytest.raises(ValueError, match="changed"):
            management.apply(
                [source.id, second.id], target_id, "merge", preview["fingerprint"]
            )
    assert source.routed_to is None and rows[0].search_profile_id == source.id
    assert "profile_transfers" not in rows[0].enrichment


def test_conflicts_require_ack_and_do_not_overwrite_configs(scope):
    _, source, rows = scope
    target = db.session.get(SearchProfile, rows[5].search_profile_id)
    source.criteria = {"max_price_eur": 200000}
    target.criteria = {"max_price_eur": 300000}
    db.session.commit()
    report = management.preview([source.id], target.id)
    assert report["conflicts"]
    with pytest.raises(ValueError, match="acknowledge_conflicts"):
        management.apply([source.id], target.id, "merge", report["fingerprint"])
    management.apply(
        [source.id], target.id, "merge", report["fingerprint"], acknowledge=True
    )
    assert source.criteria == {"max_price_eur": 200000}
    assert target.criteria == {"max_price_eur": 300000}


def test_remove_keeps_inbound_routes_and_restore_is_explicit(scope):
    app, source, rows = scope
    stub = SearchProfile(name="Incoming subscription", is_auto_created=True)
    db.session.add(stub)
    db.session.commit()
    SearchProfileService.route_profile(stub.id, source.id)
    original_count = Property.query.count()
    report = management.preview([source.id], action="remove")
    management.apply([source.id], None, "remove", report["fingerprint"])
    assert (
        not management.owner_search(source)
        and source.is_hidden
        and not source.is_active
    )
    assert Property.query.count() == original_count
    assert (
        SearchProfileService.resolve_profile(
            "", "See all listings for Incoming subscription"
        ).id
        == source.id
    )
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post(f"/profiles/{source.id}/visibility", data={"hidden": "off"})
    client.post(
        f"/profiles/{source.id}/edit",
        data={"action": "save_profile_settings", "is_active": "on"},
    )
    assert source.is_hidden and management.state(source) == "deleted"
    report = management.preview([source.id], action="promote")
    management.apply([source.id], None, "promote", report["fingerprint"])
    assert management.owner_search(source) and not source.is_hidden and source.is_active


def test_default_and_route_roots_are_protected(scope):
    _, source, rows = scope
    target_id = rows[5].search_profile_id
    source.is_default = True
    db.session.commit()
    for action in ("remove", "merge", "promote"):
        with pytest.raises(ValueError, match="protected_profile"):
            management.preview([source.id], target_id, action)
    source.is_default = False
    db.session.commit()
    stub = SearchProfile(name="Stub")
    db.session.add(stub)
    db.session.commit()
    SearchProfileService.route_profile(stub.id, source.id)
    with pytest.raises(ValueError, match="source_is_a_route_target"):
        management.preview([source.id], target_id)
    with pytest.raises(ValueError, match="protected_profile"):
        management.preview([stub.id], action="promote")


def test_new_ingestion_is_technical_and_promoted_only_by_owner(scope):
    app, _, _ = scope
    technical = SearchProfileService.get_or_create_profile_by_name(
        "A newly discovered town"
    )
    assert technical.is_hidden and management.state(technical) == "technical"
    assert (
        SearchProfileService.resolve_profile(
            "", "See all listings for A newly discovered town"
        ).id
        == technical.id
    )
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    body = client.get("/profiles").text
    assert f'data-profile-id="{technical.id}"' not in body
    assert "Needs attention" in body
    assert (
        f'data-profile-id="{technical.id}"'
        in client.get("/profiles?view=technical").text
    )
    token = confirmation(client, [technical.id], action="promote")
    assert (
        client.post("/profiles/manage", data={"preview_token": token}).status_code
        == 302
    )
    assert management.owner_search(technical) and not technical.is_hidden


def test_confirmation_is_session_bound_and_replay_safe(scope):
    app, source, rows = scope
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    token = confirmation(client, [source.id], target=rows[5].search_profile_id)
    other = app.test_client()
    other.post("/profiles/manage", data={"preview_token": token})
    assert source.routed_to is None
    client.post("/profiles/manage", data={"preview_token": token, "acknowledge": "on"})
    assert source.routed_to == rows[5].search_profile_id
    before = copy.deepcopy(rows[0].enrichment)
    client.post("/profiles/manage", data={"preview_token": token, "acknowledge": "on"})
    assert rows[0].enrichment == before


def test_csrf_and_malformed_preview_do_not_change_data(scope):
    app, source, rows = scope
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    assert (
        client.post("/profiles/manage", data={"preview_token": "bad"}).status_code
        == 400
    )
    assert source.routed_to is None
    assert client.get("/profiles/manage?source_id=nope").status_code == 302


def test_move_to_technical_retains_objects_for_attention(scope):
    _, source, rows = scope
    original = [
        (row.id, row.search_profile_id, copy.deepcopy(row.enrichment)) for row in rows
    ]
    report = management.preview([source.id], action="technical")
    management.apply([source.id], None, "technical", report["fingerprint"])
    assert (
        source.is_hidden
        and source.is_active
        and management.state(source) == "technical"
    )
    assert [(row.id, row.search_profile_id, row.enrichment) for row in rows] == original


def test_cli_requires_exact_preview_and_exclusive_private_receipt(
    scope, tmp_path, capsys
):
    from utils.manage_searches import main

    _, source, rows = scope
    target_id = rows[5].search_profile_id
    assert main(["--source", str(source.id), "--target", str(target_id)]) == 0
    import json

    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False and source.routed_to is None
    receipt = tmp_path / "operation.json"
    main(
        [
            "--source",
            str(source.id),
            "--target",
            str(target_id),
            "--apply",
            "--expected",
            result["fingerprint"],
            "--snapshot",
            str(receipt),
        ]
    )
    assert source.routed_to == target_id
    saved = json.loads(receipt.read_text())
    assert saved["properties"][0]["search_profile_id"] == source.id
    assert receipt.stat().st_mode & 0o777 == 0o600
