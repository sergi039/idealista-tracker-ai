"""Focused leakage regressions for held-out taste-evaluation manifests."""

from types import SimpleNamespace

import pytest

from services import taste_evaluation


def test_learning_entity_is_reserved_even_when_learning_row_is_skipped(monkeypatch):
    """A duplicate candidate must not enter after its learning row is skipped."""
    learning = SimpleNamespace(
        id=1,
        idealista_property_id="ABC",
        is_favorite=False,
        owner_verdict="liked",
    )
    duplicate_candidate = SimpleNamespace(
        id=2,
        idealista_property_id="ABC",
        is_favorite=False,
        owner_verdict=None,
    )
    eligible_candidate = SimpleNamespace(
        id=3,
        idealista_property_id="DEF",
        is_favorite=False,
        owner_verdict=None,
    )
    monkeypatch.setattr(
        taste_evaluation.taste_descriptors,
        "build_descriptor",
        lambda prop: {"input_fingerprint": f"descriptor-{prop.id}"},
    )

    manifest = taste_evaluation.build_manifest(
        [learning, duplicate_candidate, eligible_candidate],
        learning_property_ids={1},
        activity_property_ids=set(),
    )

    assert [candidate["property_id"] for candidate in manifest["candidates"]] == [3]


def test_missing_learning_rows_fail_closed_before_their_entity_can_leak():
    candidate = SimpleNamespace(
        id=2,
        idealista_property_id="ABC",
        is_favorite=False,
        owner_verdict=None,
    )

    with pytest.raises(ValueError, match="include every learning/activity row"):
        taste_evaluation.build_manifest(
            [candidate], learning_property_ids={1}, activity_property_ids=set()
        )
