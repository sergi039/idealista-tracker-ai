"""Recommendation behavior at the shared descriptor/profile/page boundary.

Regression: legacy Taste omitted dossier plot area and read a scalar sea-view
state as if it had to be a dict.  These tests exercise the public descriptor
and reranker outputs, including uncertainty and scope, rather than helpers.
"""

import json
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import (
    subscription_transport,
    taste_descriptors,
    taste_evaluation,
    taste_preferences,
    taste_recommendation,
    taste_service,
)
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _profile(name="Galicia"):
    row = SearchProfile(name=name, is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _property(profile, **overrides):
    values = {
        "source_email_id": f"recommendation:{overrides.get('title', 'row')}",
        "title": "Casa",
        "area": 180,
        "area_type": "built",
        "search_profile_id": profile.id,
    }
    values.update(overrides)
    row = Property(**values)
    db.session.add(row)
    db.session.commit()
    return row


def test_descriptor_reads_dossier_plot_and_scalar_sea_view(app):
    profile = _profile()
    row = _property(
        profile,
        title="969",
        plot_area=None,
        attributes={"plot_area_cadastre_m2": 1616},
        enrichment={
            "environment": {"sea_view": "likely"},
            "cadastre": {"geometry": {"area_m2": 1616.2}},
        },
    )

    descriptor = taste_descriptors.build_descriptor(row)

    plot = taste_descriptors.aspect_values(descriptor, "plot_area_m2")
    assert {(item["source_kind"], item["value"]) for item in plot} == {
        ("research_claim", 1616.0),
        ("measurement", 1616.2),
    }
    assert {item["status"] for item in plot} == {"claimed", "supported"}
    assert (
        taste_descriptors.aspect_values(descriptor, "sea_view")[0]["value"] == "likely"
    )
    assert "SEA VIEW: likely" in taste_service.gather_facts(row)
    assert any(line.startswith("PLOT AREA") for line in taste_service.gather_facts(row))


def test_material_plot_disagreement_stays_conflicting(app):
    profile = _profile()
    row = _property(
        profile,
        plot_area=1500,
        attributes={"plot_area_cadastre_m2": 2100},
        enrichment={"cadastre": {"geometry": {"area_m2": 1505}}},
    )
    plot = taste_descriptors.aspect_values(
        taste_descriptors.build_descriptor(row), "plot_area_m2"
    )
    assert {item["status"] for item in plot} == {"conflicting"}


def test_clauses_preserve_opposite_signals_conditions_and_semantic_scope():
    signals = [
        {
            "property_id": 774,
            "profile_id": 19,
            "verdict": "rejected",
            "reason": "ИСКЛЮЧЕНО: L-образная форма никогда не подходит; ровный вытянутый прямоугольник подходит.",
        },
        {
            "property_id": 969,
            "profile_id": 24,
            "verdict": "interested",
            "reason": "Нравится: участок правильной формы. Терпимые минусы: моря не видно, сырость наверху.",
        },
        {
            "property_id": 976,
            "profile_id": 24,
            "verdict": "rejected",
            "reason": "За 300 - смотри только готовое к жизни.",
        },
        {
            "property_id": 1445,
            "profile_id": 24,
            "verdict": "rejected",
            "reason": "Отдельная эстетическая причина без поддерживаемого словаря.",
        },
    ]
    clauses = taste_preferences.compile_signals(signals)

    global_shape = next(
        clause
        for clause in clauses
        if clause["source_property_id"] == 774 and clause["aspect_id"] == "plot_outline"
    )
    assert global_shape["scope"] == "global"
    assert global_shape["strength"] == "hard"
    tolerated = [clause for clause in clauses if clause["source_property_id"] == 969]
    assert any(clause["polarity"] == "tradeoff" for clause in tolerated)
    budget = next(
        clause for clause in clauses if clause["aspect_id"] == "budget_condition"
    )
    assert budget["mapping_state"] == "unresolved_condition"
    assert any(clause["mapping_state"] == "unmapped" for clause in clauses)


def test_every_observed_reason_has_each_paper_mapped_aspect_or_visible_unapplied():
    observed = {
        1405: (
            "нет земли рядом, дом под реконструкцию",
            {"land_presence", "house_condition"},
        ),
        1010: (
            "маленькие комнаты, рядом много построек, рядом сельхоз постройки",
            {"room_scale", "nearby_buildings", "agricultural_context"},
        ),
        774: (
            "ИСКЛЮЧЕНО: L-shaped с шеей никогда не подходит; ровный вытянутый прямоугольник подходит",
            {"plot_outline"},
        ),
        1445: (
            "визуально не нравится владельцу - старый дом под реконструкцию",
            {"visual_appeal", "house_condition"},
        ),
        1742: ("это участок, а не дом", {"property_kind"}),
        1563: ("это участок, а не дом что мы ищем", {"property_kind"}),
        786: (
            "форма компактная, Núcleo Rural. Открыты канализация и certificado urbanístico",
            {"plot_outline", "planning_usability", "utilities"},
        ),
        2117: ("разрушенный дом", {"house_condition"}),
        969: (
            "участок правильной формы; всё пешком, пляж 1,0 км; оптика 1 Гбит/с; 606 €/м² против медианы; Терпимые минусы: моря не видно, сырость, расхождения кадастра",
            {
                "plot_outline",
                "beach_access",
                "fiber",
                "price_value",
                "sea_view",
                "house_condition",
                "area_consistency",
            },
        ),
        976: (
            "не изолирован, заброшен и требуется много ремонта. за 300 - только готовое",
            {"neighbor_privacy", "house_condition", "budget_condition"},
        ),
        985: (
            "сх постройки рядом, огороды, не красиво",
            {"agricultural_context", "visual_appeal"},
        ),
        1009: (
            "возле дороги, далеко от пляжей, старый крестьянский дом",
            {"road_proximity", "beach_access", "house_character"},
        ),
        970: ("чужие дома близко", {"neighbor_privacy"}),
        1282: (
            "вид на море; пляж 488 м пешком; участок правильной формы; кадастр сходится; мало соседей. Минусы: выше медианы; скважина и септик; оптики на парцеле нет; нужна письменная справка мэрии",
            {
                "sea_view",
                "beach_access",
                "plot_outline",
                "area_consistency",
                "neighbor_privacy",
                "price_value",
                "utilities",
                "fiber",
                "planning_usability",
            },
        ),
        2072: ("слишком большая реконструкция", {"house_condition"}),
    }
    for property_id, (reason, expected) in observed.items():
        clauses = taste_preferences.compile_signal(
            {
                "property_id": property_id,
                "profile_id": 24,
                "verdict": "rejected"
                if property_id not in {786, 969, 1282}
                else "interested",
                "reason": reason,
            }
        )
        represented = {clause["aspect_id"] for clause in clauses}
        assert expected <= represented, (property_id, expected - represented)
        assert all(
            clause["mapping_state"]
            in {"executable", "unmapped", "unresolved_condition"}
            for clause in clauses
        )


def test_reasonless_rejected_favorite_is_not_a_positive_anchor(app):
    profile = _profile()
    favorite = _property(profile, title="reasonless", is_favorite=True)
    rejected = _property(
        profile,
        title="rejected favorite",
        is_favorite=True,
        owner_verdict="rejected",
        owner_verdict_reason="разрушенный дом",
    )
    signals = taste_service.collect_signals()
    by_id = {signal["property_id"]: signal for signal in signals}
    assert by_id[favorite.id]["positive_anchor"] is True
    assert by_id[favorite.id]["usable"] is False
    assert by_id[rejected.id]["positive_anchor"] is False


def test_page_rerank_is_pure_and_unknown_cannot_enforce_hard_rule(app):
    profile = _profile()
    reference = _property(
        profile,
        title="reference",
        is_favorite=True,
        owner_verdict="interested",
        owner_verdict_reason="Нравится участок правильной формы.",
    )
    candidate = _property(
        profile, title="candidate", description="Casa sin forma de parcela indicada"
    )
    signals = taste_service.collect_signals()
    source = {
        "recommendation_schema_version": 1,
        "signals": signals,
        "descriptors": {str(s["property_id"]): s["descriptor"] for s in signals},
        "clauses": taste_preferences.compile_signals(signals),
        "positive_reference_ids": [reference.id],
    }
    profile_data = {
        "version": 1,
        "signals_fingerprint": taste_service.signals_fingerprint(signals),
        "source": source,
    }
    summary = {"state": "current", "version": 1}

    with patch.object(
        subscription_transport,
        "complete",
        side_effect=AssertionError("page rerank called the bridge"),
    ):
        context = taste_recommendation.build_context(
            [reference, candidate], profile_data, summary
        )

    assert context.readings[reference.id]["state"] == "reference"
    reading = context.readings[candidate.id]
    assert reading["state"] == "candidate"
    assert reading["group"] == "potential"
    assert reading["needs_verification"]
    assert not any(item.get("hard") for item in reading["conflicts"])


def test_star_change_marks_a_published_profile_dirty(app):
    profile = _profile()
    reference = _property(
        profile,
        title="reference",
        is_favorite=True,
        owner_verdict="interested",
        owner_verdict_reason="Нравится вид на море.",
    )
    signals = taste_service.collect_signals()
    data = {
        "version": 1,
        "signals_fingerprint": taste_service.signals_fingerprint(signals),
        "source": {
            "recommendation_schema_version": 1,
            "clauses": [],
            "signals": signals,
            "positive_reference_ids": [reference.id],
        },
    }
    assert taste_service.recommendation_profile_state(data)["state"] == "current"
    reference.is_favorite = False
    db.session.commit()
    assert taste_service.recommendation_profile_state(data)["state"] == "dirty"


def test_held_out_manifest_excludes_signals_activity_and_duplicate_entities(app):
    profile = _profile()
    learning = _property(profile, title="learning", idealista_property_id=10)
    starred = _property(profile, title="starred", is_favorite=True)
    activity = _property(profile, title="activity")
    first = _property(
        profile,
        title="first",
        url="https://www.idealista.com/inmueble/44/?utm_source=test",
    )
    duplicate = _property(
        profile,
        title="duplicate",
        url="https://www.idealista.com/inmueble/44/",
    )
    manifest = taste_evaluation.build_manifest(
        [learning, starred, activity, first, duplicate],
        learning_property_ids={learning.id},
        activity_property_ids={activity.id},
    )
    assert [row["property_id"] for row in manifest["candidates"]] == [first.id]
    assert manifest["owner_labels_included"] is False
    assert manifest["utility_claim"] == "not_measured"
    assert manifest["candidates"][0]["eligibility"] == "no_recorded_activity"


def test_legacy_taste_rescore_preserves_visual_descriptor(app):
    profile = _profile()
    reference = _property(
        profile,
        title="reference",
        is_favorite=True,
        owner_verdict="interested",
        owner_verdict_reason="Нравится вид на море.",
    )
    candidate = _property(profile, title="candidate")
    candidate.taste = {
        "visual_descriptor": {
            "schema_version": 1,
            "input_fingerprint": "a" * 64,
            "property_fingerprint": taste_descriptors.input_fingerprint(candidate),
            "visual_observations": [],
        }
    }
    db.session.commit()
    profile_data = {
        "version": 1,
        "profile": {"likes": [{"trait": "sea"}]},
        "source": {
            "signals": [
                {
                    "property_id": reference.id,
                    "verdict": "interested",
                    "reason": "Нравится вид на море.",
                }
            ],
            "provisional": True,
        },
    }
    answer = {
        "results": [
            {
                "property_id": candidate.id,
                "score": 50,
                "reasons_ru": ["Недостаточно данных."],
                "matched_likes": [],
                "matched_dislikes": [],
                "closest_reference_id": reference.id,
                "confidence": "low",
            }
        ]
    }
    with patch.object(
        subscription_transport,
        "complete",
        return_value={"text": json.dumps(answer), "model": "test"},
    ):
        outcome = taste_service.score_batch([candidate], profile_data)
    assert outcome["status"] == "ok"
    assert candidate.taste["visual_descriptor"]["input_fingerprint"] == "a" * 64


class _SimilarityReadings:
    """Minimal request-scoped similarity context at the recommender boundary."""

    def __init__(self, readings):
        self._readings = readings

    def read(self, property_id):
        return self._readings.get(property_id, {"score": None})


def _recommendation_profile(
    reference_ids, *, clauses=None, descriptors=None, reference_profile_ids=None
):
    reference_profile_ids = reference_profile_ids or {
        reference_id: 1 for reference_id in reference_ids
    }
    return {
        "source": {
            "recommendation_schema_version": 1,
            "positive_reference_ids": reference_ids,
            "signals": [
                {
                    "property_id": reference_id,
                    "profile_id": reference_profile_ids[reference_id],
                    "positive_anchor": True,
                }
                for reference_id in reference_ids
            ],
            "clauses": clauses or [],
            "descriptors": descriptors or {},
        }
    }


def _compiled_profile_from_current_signals():
    """The production source snapshot shape, without a model call."""
    signals = taste_service.collect_signals()
    return {
        "source": {
            "recommendation_schema_version": 1,
            "positive_reference_ids": [
                signal["property_id"]
                for signal in signals
                if signal.get("positive_anchor")
            ],
            "signals": signals,
            "clauses": taste_preferences.compile_signals(signals),
            "descriptors": {
                str(signal["property_id"]): signal["descriptor"] for signal in signals
            },
        }
    }


def _photo_aspect(value, *, status="claimed"):
    return {
        "value": value,
        "status": status,
        "source_kind": "photo",
        "source_id": "photo:source",
        "evidence": {
            "source_kind": "photo",
            "source_id": "photo:source",
            "image_sha256": "c" * 64,
            "image_index": 0,
        },
    }


def _visual_descriptor(prop, observations, *, identity="a" * 64):
    return {
        "schema_version": 1,
        "input_fingerprint": identity,
        "property_fingerprint": taste_descriptors.input_fingerprint(prop),
        "visual_observations": observations,
    }


def test_claimed_visual_match_stays_potential_without_supported_coverage(app):
    """A photo claim may explain a match but cannot create confident evidence."""
    profile = _profile()
    reference = _property(
        profile,
        title="reference",
        is_favorite=True,
        owner_verdict="interested",
    )
    candidate = _property(profile, title="claimed visual candidate")
    candidate.taste = {
        "visual_descriptor": {
            "schema_version": 1,
            "input_fingerprint": "a" * 64,
            "property_fingerprint": taste_descriptors.input_fingerprint(candidate),
            "visual_observations": [
                {
                    "aspect_id": "house_condition",
                    "value": "damp",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "image-1",
                        "image_sha256": "b" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.6,
                    "limitation": "Single exterior image only.",
                }
            ],
        }
    }
    db.session.commit()
    profile_data = _recommendation_profile(
        [reference.id],
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "house_condition",
                "source_property_id": reference.id,
                "source_profile_id": profile.id,
                "scope": "profile",
                "polarity": "prefer",
                "strength": "soft",
            }
        ],
        descriptors={
            str(reference.id): {
                "aspects": {
                    "house_condition": [{"value": "damp", "status": "supported"}]
                }
            }
        },
    )

    reading = taste_recommendation.build_context(
        [reference, candidate], profile_data, {"state": "current"}
    ).readings[candidate.id]

    assert reading["group"] == "potential"
    assert reading["coverage"] == {"supported": 0, "relevant": 1}
    assert reading["matches"][0]["status"] == "claimed"


def test_similarity_reading_sets_nearest_reference_and_numerically_breaks_ties(app):
    """Request similarity supplies the nearest positive anchor and float tie-break."""
    profile = _profile()
    first_reference = _property(
        profile, title="first reference", is_favorite=True, owner_verdict="interested"
    )
    nearest_reference = _property(
        profile, title="nearest reference", is_favorite=True, owner_verdict="interested"
    )
    higher = _property(profile, title="higher similarity")
    lower = _property(profile, title="lower similarity")
    similarity_ctx = _SimilarityReadings(
        {
            higher.id: {
                "reference_id": nearest_reference.id,
                "compared": ["area", "geography"],
                "score": 80.21,
            },
            lower.id: {
                "reference_id": first_reference.id,
                "compared": ["area", "geography"],
                "score": 80.20,
            },
        }
    )

    context = taste_recommendation.build_context(
        [first_reference, nearest_reference, higher, lower],
        _recommendation_profile([first_reference.id, nearest_reference.id]),
        {"state": "current"},
        similarity_ctx=similarity_ctx,
    )

    nearest = context.readings[higher.id]["nearest_positive_reference"]
    assert nearest["id"] == nearest_reference.id
    assert nearest["matched_aspect_ids"] == ["area", "geography"]
    assert (
        context.readings[higher.id]["rank_value"]
        > context.readings[lower.id]["rank_value"]
    )


def test_recommendation_sort_expression_orders_candidate_ranks_in_sql(app):
    """The SQL ORDER BY keeps a higher candidate recommendation ahead of a lower one."""
    profile = _profile()
    reference = _property(
        profile, title="reference", is_favorite=True, owner_verdict="interested"
    )
    rejected = _property(profile, title="rejected", owner_verdict="rejected")
    higher = _property(profile, title="higher candidate")
    lower = _property(profile, title="lower candidate")
    context = taste_recommendation.build_context(
        [reference, rejected, higher, lower],
        _recommendation_profile([reference.id]),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {
                higher.id: {"reference_id": reference.id, "score": 91.7},
                lower.id: {"reference_id": reference.id, "score": 12.5},
            }
        ),
    )

    ordered = (
        Property.query.filter(Property.owner_verdict.is_(None))
        .order_by(
            taste_recommendation.sort_expression(Property, context).desc().nullslast(),
            Property.id.asc(),
        )
        .all()
    )

    assert [row.id for row in ordered] == [higher.id, lower.id]


def test_empty_recommendation_search_renders_without_an_empty_case_expression(app):
    """The live route may legitimately have no ids to place in its SQL CASE."""
    _profile()

    response = app.test_client().get(
        "/properties?profile_id=all&mode=recommendation&sort=recommendation"
        "&search=definitely-no-such-property"
    )

    assert response.status_code == 200
    assert b"An error occurred while loading properties" not in response.data


def test_reference_visual_snapshot_preserves_image_evidence_and_invalidates_profile(
    app,
):
    profile = _profile()
    reference = _property(
        profile, title="visual reference", is_favorite=True, owner_verdict="interested"
    )
    reference.taste = {
        "visual_descriptor": _visual_descriptor(
            reference,
            [
                {
                    "aspect_id": "house_condition",
                    "value": "weathered",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "property:1:dossier:0",
                        "image_sha256": "d" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Only the front facade is visible.",
                }
            ],
        )
    }
    db.session.commit()

    before = taste_service.collect_signals()
    photo = before[0]["descriptor"]["aspects"]["house_condition"][-1]
    assert photo["evidence"] == {
        "source_kind": "photo",
        "source_id": "property:1:dossier:0",
        "image_sha256": "d" * 64,
        "image_index": 0,
    }
    assert photo["source_kind"] == "photo"
    before_fingerprint = taste_service.signals_fingerprint(before)
    profile_data = {
        "version": 1,
        "signals_fingerprint": before_fingerprint,
        "source": {"recommendation_schema_version": 1},
    }
    assert (
        taste_service.recommendation_profile_state(profile_data)["state"] == "current"
    )

    updated_taste = dict(reference.taste)
    updated_visual = dict(updated_taste["visual_descriptor"])
    updated_observation = dict(updated_visual["visual_observations"][0])
    updated_observation["value"] = "damp"
    updated_visual["visual_observations"] = [updated_observation]
    updated_taste["visual_descriptor"] = updated_visual
    reference.taste = updated_taste
    db.session.commit()
    assert taste_service.recommendation_profile_state(profile_data)["state"] == "dirty"


def test_equal_numeric_similarity_uses_photo_texture_as_a_separate_rank_channel(app):
    profile = _profile()
    numeric_reference = _property(
        profile, title="numeric reference", is_favorite=True, owner_verdict="interested"
    )
    textured_reference = _property(
        profile,
        title="textured reference",
        is_favorite=True,
        owner_verdict="interested",
    )
    textured_candidate = _property(profile, title="textured candidate")
    untextured_candidate = _property(profile, title="untextured candidate")
    textured_candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            textured_candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "old_farmhouse",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:textured",
                        "image_sha256": "e" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Only the facade is visible.",
                }
            ],
        )
    }
    untextured_candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            untextured_candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "modern_house",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:untextured",
                        "image_sha256": "f" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Only the facade is visible.",
                }
            ],
        )
    }
    db.session.commit()
    descriptors = {
        str(numeric_reference.id): {
            "aspects": {"house_character": [_photo_aspect("rural_house")]}
        },
        str(textured_reference.id): {
            "aspects": {"house_character": [_photo_aspect("old_farmhouse")]}
        },
    }
    context = taste_recommendation.build_context(
        [
            numeric_reference,
            textured_reference,
            textured_candidate,
            untextured_candidate,
        ],
        _recommendation_profile(
            [numeric_reference.id, textured_reference.id], descriptors=descriptors
        ),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {
                textured_candidate.id: {
                    "reference_id": numeric_reference.id,
                    "score": 80.0,
                    "compared": ["area", "geography"],
                },
                untextured_candidate.id: {
                    "reference_id": numeric_reference.id,
                    "score": 80.0,
                    "compared": ["area", "geography"],
                },
            }
        ),
    )

    textured = context.readings[textured_candidate.id]
    untextured = context.readings[untextured_candidate.id]
    assert textured["rank_value"] > untextured["rank_value"]
    assert textured["group"] == "potential"
    assert textured["coverage"] == {"supported": 0, "relevant": 0}
    ordered = (
        Property.query.filter(
            Property.id.in_([textured_candidate.id, untextured_candidate.id])
        )
        .order_by(
            taste_recommendation.sort_expression(Property, context).desc().nullslast(),
            Property.id.asc(),
        )
        .all()
    )
    assert [row.id for row in ordered] == [
        textured_candidate.id,
        untextured_candidate.id,
    ]


def test_composite_reference_truthfully_reports_visual_winner_over_numeric_nearest(app):
    profile = _profile()
    numeric_reference = _property(
        profile, title="numeric reference", is_favorite=True, owner_verdict="interested"
    )
    visual_reference = _property(
        profile, title="visual reference", is_favorite=True, owner_verdict="interested"
    )
    candidate = _property(profile, title="composite candidate")
    candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "old_farmhouse",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:character",
                        "image_sha256": "a" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Exterior only.",
                },
                {
                    "aspect_id": "house_condition",
                    "value": "weathered",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:condition",
                        "image_sha256": "b" * 64,
                        "image_index": 1,
                    },
                    "confidence": 0.7,
                    "limitation": "Exterior only.",
                },
            ],
        )
    }
    db.session.commit()
    context = taste_recommendation.build_context(
        [numeric_reference, visual_reference, candidate],
        _recommendation_profile(
            [numeric_reference.id, visual_reference.id],
            descriptors={
                str(numeric_reference.id): {
                    "aspects": {"house_character": [_photo_aspect("rural_house")]}
                },
                str(visual_reference.id): {
                    "aspects": {
                        "house_character": [_photo_aspect("old_farmhouse")],
                        "house_condition": [_photo_aspect("weathered")],
                    }
                },
            },
        ),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {
                candidate.id: {
                    "reference_id": numeric_reference.id,
                    "score": 95.0,
                    "compared": ["area", "geography"],
                }
            }
        ),
    )

    nearest = context.readings[candidate.id]["nearest_positive_reference"]
    assert nearest["id"] == visual_reference.id
    assert nearest["visual_matched_aspect_ids"] == [
        "house_character",
        "house_condition",
    ]
    assert nearest["numeric_reference_id"] == numeric_reference.id
    assert nearest["numeric_score"] == 95.0


def test_similarity_score_from_a_nonpositive_reference_is_ignored(app):
    profile = _profile()
    positive = _property(
        profile, title="positive", is_favorite=True, owner_verdict="interested"
    )
    rejected = _property(
        profile, title="rejected", is_favorite=True, owner_verdict="rejected"
    )
    candidate = _property(profile, title="candidate")
    context = taste_recommendation.build_context(
        [positive, rejected, candidate],
        _recommendation_profile([positive.id]),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {candidate.id: {"reference_id": rejected.id, "score": 99.9}}
        ),
    )

    reading = context.readings[candidate.id]
    assert reading["nearest_positive_reference"] is None
    assert reading["rank_value"] == 2000.0


def test_reference_rejected_after_snapshot_stops_anchoring_before_refresh(app):
    profile = _profile()
    reference = _property(
        profile, title="later rejected", is_favorite=True, owner_verdict="interested"
    )
    candidate = _property(profile, title="matching candidate")
    for prop, source_id in (
        (reference, "photo:reference"),
        (candidate, "photo:candidate"),
    ):
        prop.taste = {
            "visual_descriptor": _visual_descriptor(
                prop,
                [
                    {
                        "aspect_id": "house_character",
                        "value": "stone_house",
                        "status": "claimed",
                        "evidence": {
                            "source_kind": "photo",
                            "source_id": source_id,
                            "image_sha256": ("a" if prop is reference else "b") * 64,
                            "image_index": 0,
                        },
                        "confidence": 0.7,
                        "limitation": "Facade only.",
                    }
                ],
            )
        }
    db.session.commit()
    snapshot_signals = taste_service.collect_signals()
    profile_data = {
        "version": 1,
        "signals_fingerprint": taste_service.signals_fingerprint(snapshot_signals),
        "source": {
            "recommendation_schema_version": 1,
            "positive_reference_ids": [reference.id],
            "signals": snapshot_signals,
            "clauses": [],
            "descriptors": {
                str(signal["property_id"]): signal["descriptor"]
                for signal in snapshot_signals
            },
        },
    }

    reference.owner_verdict = "rejected"
    db.session.commit()
    summary = taste_service.recommendation_profile_state(profile_data)
    context = taste_recommendation.build_context(
        [reference, candidate],
        profile_data,
        summary,
        similarity_ctx=_SimilarityReadings(
            {candidate.id: {"reference_id": reference.id, "score": 99.9}}
        ),
    )

    assert summary["state"] == "dirty"
    assert summary["current_positive_reference_ids"] == []
    assert context.readings[reference.id]["state"] == "rejected"
    assert context.readings[candidate.id]["nearest_positive_reference"] is None
    assert context.readings[candidate.id]["rank_value"] == 500.0


def test_candidate_cannot_borrow_a_foreign_profile_visual_or_numeric_reference(app):
    local_profile = _profile("Local")
    foreign_profile = _profile("Foreign")
    local_reference = _property(
        local_profile,
        title="local reference",
        is_favorite=True,
        owner_verdict="interested",
    )
    foreign_reference = _property(
        foreign_profile,
        title="foreign reference",
        is_favorite=True,
        owner_verdict="interested",
    )
    candidate = _property(local_profile, title="local candidate")
    candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "old_farmhouse",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:foreign-match",
                        "image_sha256": "a" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Exterior facade only.",
                }
            ],
        )
    }
    db.session.commit()

    context = taste_recommendation.build_context(
        [local_reference, foreign_reference, candidate],
        _recommendation_profile(
            [local_reference.id, foreign_reference.id],
            reference_profile_ids={
                local_reference.id: local_profile.id,
                foreign_reference.id: foreign_profile.id,
            },
            descriptors={
                str(local_reference.id): {
                    "aspects": {"house_character": [_photo_aspect("rural_house")]}
                },
                str(foreign_reference.id): {
                    "aspects": {"house_character": [_photo_aspect("old_farmhouse")]}
                },
            },
        ),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {candidate.id: {"reference_id": foreign_reference.id, "score": 99.9}}
        ),
    )

    reading = context.readings[candidate.id]
    assert context.readings[foreign_reference.id]["state"] == "reference"
    assert reading["nearest_positive_reference"] is None
    assert reading["rank_value"] == 2000.0


def test_explicit_global_clause_still_applies_across_reference_profiles(app):
    foreign_profile = _profile("Foreign")
    local_profile = _profile("Local")
    source = _property(
        foreign_profile,
        title="global rule source",
        is_favorite=True,
        owner_verdict="interested",
    )
    candidate = _property(local_profile, title="known global violation")
    profile_data = _recommendation_profile(
        [source.id],
        reference_profile_ids={source.id: foreign_profile.id},
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "plot_outline",
                "source_property_id": source.id,
                "source_profile_id": foreign_profile.id,
                "scope": "global",
                "polarity": "avoid",
                "strength": "hard",
            }
        ],
        descriptors={
            str(source.id): {
                "aspects": {"plot_outline": [{"value": "notched", "status": "claimed"}]}
            }
        },
    )

    with patch.object(
        taste_recommendation.taste_descriptors,
        "build_descriptor",
        return_value={
            "aspects": {"plot_outline": [{"value": "notched", "status": "supported"}]}
        },
    ):
        context = taste_recommendation.build_context(
            [candidate], profile_data, {"state": "current"}
        )

    reading = context.readings[candidate.id]
    assert reading["eligibility"] == "excluded"
    assert reading["nearest_positive_reference"] is None


def test_each_clause_compares_only_the_value_named_in_that_clause(app):
    """Opposite preferences on one source aspect must remain independent."""
    profile = _profile()
    source = _property(profile, title="mixed shape source", is_favorite=True)
    candidate = _property(profile, title="regular candidate")
    profile_data = _recommendation_profile(
        [source.id],
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "plot_outline",
                "source_property_id": source.id,
                "source_profile_id": profile.id,
                "scope": "profile",
                "polarity": "avoid",
                "strength": "hard",
                "values": ["notched"],
            },
            {
                "mapping_state": "executable",
                "aspect_id": "plot_outline",
                "source_property_id": source.id,
                "source_profile_id": profile.id,
                "scope": "profile",
                "polarity": "prefer",
                "strength": "soft",
                "values": ["regular"],
            },
        ],
        descriptors={
            str(source.id): {
                "aspects": {
                    "plot_outline": [
                        {"value": "notched", "status": "claimed"},
                        {"value": "regular", "status": "claimed"},
                    ]
                }
            }
        },
    )

    with patch.object(
        taste_recommendation.taste_descriptors,
        "build_descriptor",
        return_value={
            "aspects": {"plot_outline": [{"value": "regular", "status": "supported"}]}
        },
    ):
        reading = taste_recommendation.build_context(
            [candidate], profile_data, {"state": "current"}
        ).readings[candidate.id]

    assert reading["conflicts"] == []
    assert [facet["aspect_id"] for facet in reading["matches"]] == ["plot_outline"]
    assert reading["eligibility"] == "eligible"


def test_hard_exclusion_uses_the_status_of_the_violating_value(app):
    """Supported upkeep cannot promote a claimed ruined value into a hard fact."""
    profile = _profile()
    source = _property(profile, title="avoid ruined source", is_favorite=True)
    candidate = _property(profile, title="mixed support candidate")
    profile_data = _recommendation_profile(
        [source.id],
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "house_condition",
                "source_property_id": source.id,
                "source_profile_id": profile.id,
                "scope": "profile",
                "polarity": "avoid",
                "strength": "hard",
                "values": ["ruined"],
            }
        ],
    )

    with patch.object(
        taste_recommendation.taste_descriptors,
        "build_descriptor",
        return_value={
            "aspects": {
                "house_condition": [
                    {"value": "well_maintained", "status": "supported"},
                    {"value": "ruined", "status": "claimed"},
                ]
            }
        },
    ):
        reading = taste_recommendation.build_context(
            [candidate], profile_data, {"state": "current"}
        ).readings[candidate.id]

    assert reading["conflicts"][0]["status"] == "claimed"
    assert reading["conflicts"][0]["hard"] is False
    assert reading["coverage"] == {"supported": 0, "relevant": 1}
    assert reading["eligibility"] == "eligible"


def test_avoided_value_nonoverlap_is_neutral_not_a_positive_rank_match(app):
    profile = _profile()
    source = _property(profile, title="avoid renovation source", is_favorite=True)
    candidate = _property(profile, title="maintained candidate")
    profile_data = _recommendation_profile(
        [source.id],
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "house_condition",
                "source_property_id": source.id,
                "source_profile_id": profile.id,
                "scope": "profile",
                "polarity": "avoid",
                "strength": "soft",
                "values": ["major_renovation"],
            }
        ],
    )

    with patch.object(
        taste_recommendation.taste_descriptors,
        "build_descriptor",
        return_value={
            "aspects": {
                "house_condition": [{"value": "well_maintained", "status": "supported"}]
            }
        },
    ):
        reading = taste_recommendation.build_context(
            [candidate], profile_data, {"state": "current"}
        ).readings[candidate.id]

    assert reading["matches"] == []
    assert reading["conflicts"] == []
    assert reading["coverage"] == {"supported": 1, "relevant": 1}
    assert reading["rank_value"] == 2000.0


def test_compiled_sea_preference_matches_supported_candidate_evidence(app):
    profile = _profile()
    _property(
        profile,
        title="sea preference",
        is_favorite=True,
        owner_verdict="interested",
        owner_verdict_reason="Нравится: вид на море.",
    )
    candidate = _property(
        profile,
        title="measured sea candidate",
        enrichment={"environment": {"sea_view": "yes"}},
    )

    reading = taste_recommendation.build_context(
        [candidate],
        _compiled_profile_from_current_signals(),
        {"state": "current"},
    ).readings[candidate.id]

    assert [(facet["aspect_id"], facet["status"]) for facet in reading["matches"]] == [
        ("sea_view", "supported")
    ]
    assert reading["group"] == "confident"
    assert reading["coverage"] == {"supported": 1, "relevant": 1}


def test_compiled_stone_preference_matches_photo_descriptor(app):
    profile = _profile()
    _property(
        profile,
        title="stone preference",
        is_favorite=True,
        owner_verdict="interested",
        owner_verdict_reason="Нравится: каменный дом.",
    )
    candidate = _property(profile, title="stone candidate")
    candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "stone_house",
                    "status": "supported",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:stone",
                        "image_sha256": "d" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.8,
                    "limitation": "Only the visible facade is in frame.",
                }
            ],
        )
    }
    db.session.commit()

    reading = taste_recommendation.build_context(
        [candidate],
        _compiled_profile_from_current_signals(),
        {"state": "current"},
    ).readings[candidate.id]

    assert [(facet["aspect_id"], facet["status"]) for facet in reading["matches"]] == [
        ("house_character", "supported")
    ]
    assert reading["group"] == "confident"


def test_compiled_global_land_avoidance_excludes_supported_land(app):
    profile = _profile()
    _property(
        profile,
        title="land avoidance",
        owner_verdict="rejected",
        owner_verdict_reason="Никогда не хочу участок, а не дом.",
    )
    candidate = _property(
        profile,
        title="bare land candidate",
        property_category="land",
        area_type="built",
    )

    reading = taste_recommendation.build_context(
        [candidate],
        _compiled_profile_from_current_signals(),
        {"state": "current"},
    ).readings[candidate.id]

    assert reading["eligibility"] == "excluded"
    assert reading["hard_exclusions"][0]["aspect_id"] == "property_kind"
    assert reading["hard_exclusions"][0]["status"] == "supported"


def test_compiled_mixed_shape_reason_does_not_conflict_with_regular_candidate(app):
    profile = _profile()
    _property(
        profile,
        title="mixed shape reason",
        owner_verdict="rejected",
        owner_verdict_reason=(
            "ИСКЛЮЧЕНО: L-образная форма никогда не подходит; "
            "ровный вытянутый прямоугольник подходит."
        ),
    )
    candidate = _property(
        profile,
        title="regular plot candidate",
        description="Участок правильной формы.",
    )

    reading = taste_recommendation.build_context(
        [candidate],
        _compiled_profile_from_current_signals(),
        {"state": "current"},
    ).readings[candidate.id]

    assert reading["conflicts"] == []
    assert [facet["aspect_id"] for facet in reading["matches"]] == ["plot_outline"]
    assert reading["eligibility"] == "eligible"


def test_visual_winner_keeps_the_independent_numeric_rank_channel(app):
    profile = _profile()
    numeric_reference = _property(
        profile, title="numeric reference", is_favorite=True, owner_verdict="interested"
    )
    visual_reference = _property(
        profile, title="visual reference", is_favorite=True, owner_verdict="interested"
    )
    candidate = _property(profile, title="candidate")
    candidate.taste = {
        "visual_descriptor": _visual_descriptor(
            candidate,
            [
                {
                    "aspect_id": "house_character",
                    "value": "stone_house",
                    "status": "claimed",
                    "evidence": {
                        "source_kind": "photo",
                        "source_id": "candidate:stone",
                        "image_sha256": "9" * 64,
                        "image_index": 0,
                    },
                    "confidence": 0.7,
                    "limitation": "Facade only.",
                }
            ],
        )
    }
    db.session.commit()
    context = taste_recommendation.build_context(
        [numeric_reference, visual_reference, candidate],
        _recommendation_profile(
            [numeric_reference.id, visual_reference.id],
            descriptors={
                str(numeric_reference.id): {
                    "aspects": {"house_character": [_photo_aspect("modern_house")]}
                },
                str(visual_reference.id): {
                    "aspects": {"house_character": [_photo_aspect("stone_house")]}
                },
            },
        ),
        {"state": "current"},
        similarity_ctx=_SimilarityReadings(
            {
                candidate.id: {
                    "reference_id": numeric_reference.id,
                    "score": 80.0,
                    "compared": ["area", "geography"],
                }
            }
        ),
    )

    reading = context.readings[candidate.id]
    assert reading["nearest_positive_reference"]["id"] == visual_reference.id
    assert reading["rank_value"] == 2001.8


def test_conflicting_candidate_measurement_is_a_conflict_not_unknown(app):
    profile = _profile()
    reference = _property(
        profile, title="reference", is_favorite=True, owner_verdict="interested"
    )
    candidate = _property(
        profile,
        title="candidate",
        plot_area=1000,
        attributes={"plot_area_cadastre_m2": 1800},
    )
    descriptor = taste_descriptors.build_descriptor(reference)
    context = taste_recommendation.build_context(
        [reference, candidate],
        _recommendation_profile(
            [reference.id],
            clauses=[
                {
                    "mapping_state": "executable",
                    "aspect_id": "plot_area_m2",
                    "source_property_id": reference.id,
                    "source_profile_id": profile.id,
                    "scope": "profile",
                    "polarity": "prefer",
                    "strength": "soft",
                }
            ],
            descriptors={str(reference.id): descriptor},
        ),
        {"state": "current"},
    )

    reading = context.readings[candidate.id]
    assert reading["group"] == "conflict"
    assert reading["conflicts"][0]["aspect_id"] == "plot_area_m2"
    assert reading["needs_verification"] == []


def test_sea_presence_matches_across_yes_and_likely_states(app):
    profile = _profile()
    reference = _property(
        profile,
        title="reference",
        is_favorite=True,
        owner_verdict="interested",
        enrichment={"environment": {"sea_view": "yes"}},
    )
    candidate = _property(
        profile, title="candidate", enrichment={"environment": {"sea_view": "likely"}}
    )
    context = taste_recommendation.build_context(
        [reference, candidate],
        _recommendation_profile(
            [reference.id],
            clauses=[
                {
                    "mapping_state": "executable",
                    "aspect_id": "sea_view",
                    "source_property_id": reference.id,
                    "source_profile_id": profile.id,
                    "scope": "profile",
                    "polarity": "prefer",
                    "strength": "soft",
                }
            ],
            descriptors={
                str(reference.id): taste_descriptors.build_descriptor(reference)
            },
        ),
        {"state": "current"},
    )

    reading = context.readings[candidate.id]
    assert reading["group"] == "confident"
    assert reading["matches"][0]["aspect_id"] == "sea_view"


def test_supported_hard_violation_is_excluded_while_unknown_is_retained(app):
    profile = _profile()
    reference = _property(
        profile, title="reference", is_favorite=True, owner_verdict="interested"
    )
    violating = _property(profile, title="known notched plot")
    unknown = _property(profile, title="unknown plot")
    descriptors = {
        reference.id: {
            "aspects": {"plot_outline": [{"value": "notched", "status": "claimed"}]}
        },
        violating.id: {
            "aspects": {"plot_outline": [{"value": "notched", "status": "supported"}]}
        },
        unknown.id: {
            "aspects": {"plot_outline": [{"value": None, "status": "unknown"}]}
        },
    }
    profile_data = _recommendation_profile(
        [reference.id],
        clauses=[
            {
                "mapping_state": "executable",
                "aspect_id": "plot_outline",
                "source_property_id": reference.id,
                "source_profile_id": profile.id,
                "scope": "global",
                "polarity": "avoid",
                "strength": "hard",
            }
        ],
        descriptors={str(reference.id): descriptors[reference.id]},
    )

    with patch.object(
        taste_recommendation.taste_descriptors,
        "build_descriptor",
        side_effect=lambda prop, **_kwargs: descriptors[prop.id],
    ):
        context = taste_recommendation.build_context(
            [reference, violating, unknown], profile_data, {"state": "current"}
        )

    assert context.readings[violating.id]["eligibility"] == "excluded"
    assert context.readings[violating.id]["hard_exclusions"][0]["hard"] is True
    assert context.readings[unknown.id]["eligibility"] == "eligible"
    assert context.readings[unknown.id]["needs_verification"][0]["status"] == "unknown"
