"""Stored routes and listing kinds must support the facts recommendations use.

Before the fixes a 14-minute car route produced a confident walking-beach
match, and apartments/offices/unknown kinds were all supported houses.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import (
    taste_descriptors,
    taste_preferences,
    taste_recommendation,
    taste_service,
)
from services.property_travel_service import BeachLookup, PropertyTravelService
from tests import setup_test_environment


def _property(**overrides):
    values = {
        "id": 9,
        "source_email_id": "descriptor-route-kind-reference",
        "search_profile_id": 24,
        "title": "Reference",
        "area": 180,
        "plot_area": 900,
        "area_type": "built",
        "property_category": "housing",
        "property_subtype": "house",
        "attributes": {},
        "enrichment": {},
        "travel": {},
        "description": "",
    }
    values.update(overrides)
    return Property(**values)


def _beach_reading(prop):
    clauses = taste_preferences.compile_signals(
        [
            {
                "property_id": 1,
                "profile_id": prop.search_profile_id,
                "verdict": "interested",
                "reason": "Нравится пляж 5 минут пешком",
            }
        ]
    )
    context = taste_recommendation.build_context(
        [prop],
        {"source": {"clauses": clauses}},
        {"state": "current"},
    )
    return context.readings[prop.id]


def test_real_driving_beach_payload_cannot_confirm_a_walking_preference():
    # Use the real stored-payload writer so a fixture cannot silently rename
    # the car route as a walking measurement.
    beaches = PropertyTravelService._beaches_payload(
        BeachLookup(places=[{}], total_found=1),
        {
            "beach:0": {
                "status": "ok",
                "duration_min": 14,
                "distance_m": 12000,
                "distance_km": 12,
                "place": {"name": "Example beach"},
            }
        },
    )
    prop = _property(travel={"beaches": beaches})

    reading = _beach_reading(prop)

    assert reading["group"] == "potential"
    assert reading["matches"] == []
    assert reading["conflicts"] == []
    assert reading["coverage"] == {"supported": 0, "relevant": 1}
    assert [item["aspect_id"] for item in reading["needs_verification"]] == [
        "beach_access"
    ]
    assert prop.travel["beaches"] == beaches
    assert beaches["mode"] == "driving"
    assert beaches["items"][0]["distance_m"] == 12000


@pytest.mark.parametrize("mode", [None, "driving", "cycling", "invalid"])
@pytest.mark.parametrize("duration", [14, 18])
def test_other_route_modes_cannot_prove_either_near_or_far_on_foot(mode, duration):
    prop = _property(
        travel={
            "beaches": {
                "status": "ok",
                "mode": mode,
                "items": [{"duration_min": duration}],
            }
        }
    )

    descriptor = taste_descriptors.build_descriptor(prop)

    assert taste_descriptors.aspect_values(descriptor, "beach_access") == []


@pytest.mark.parametrize(
    ("duration", "expected"), [(14, "walkable"), (15, "walkable"), (16, "far")]
)
def test_explicit_walking_measurement_retains_the_pedestrian_threshold(
    duration, expected
):
    prop = _property(
        travel={
            "beaches": {
                "status": "ok",
                "mode": "walking",
                "items": [{"duration_min": duration}],
            }
        }
    )

    rows = taste_descriptors.aspect_values(
        taste_descriptors.build_descriptor(prop), "beach_access"
    )

    assert [(row["value"], row["status"]) for row in rows] == [(expected, "supported")]


@pytest.mark.parametrize(
    ("category", "subtype", "area_type", "kind", "status"),
    [
        ("housing", "house", "built", "house", "supported"),
        ("housing", "apartment", "built", "apartment", "supported"),
        ("commercial", "office", "built", "office", "supported"),
        ("land", "developed", "built", "land", "supported"),
        ("land", "plot", "plot", "land", "supported"),
        (None, None, "built", None, "unknown"),
        ("housing", None, "built", None, "unknown"),
        (None, None, "plot", None, "unknown"),
    ],
)
def test_descriptor_preserves_the_stated_kind_or_its_absence(
    category, subtype, area_type, kind, status
):
    prop = _property(
        property_category=category,
        property_subtype=subtype,
        area_type=area_type,
    )

    rows = taste_descriptors.aspect_values(
        taste_descriptors.build_descriptor(prop), "property_kind"
    )

    assert [(row["value"], row["status"]) for row in rows] == [(kind, status)]


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


def _snapshot():
    signals = taste_service.collect_signals()
    return {
        "version": 1,
        "signals_fingerprint": taste_service.signals_fingerprint(signals),
        "source": {
            "recommendation_schema_version": taste_service.RECOMMENDATION_SCHEMA_VERSION,
            "preference_compiler_schema_version": taste_preferences.SCHEMA_VERSION,
        },
    }


@pytest.mark.parametrize(
    ("old_subtype", "new_subtype"),
    [("house", "apartment"), ("apartment", "office"), (None, "house")],
)
def test_changed_reference_kind_marks_the_published_snapshot_dirty(
    app, old_subtype, new_subtype
):
    profile = SearchProfile(id=24, name="Houses", is_active=True)
    prop = _property(is_favorite=True, property_subtype=old_subtype)
    db.session.add_all([profile, prop])
    db.session.commit()
    snapshot = _snapshot()
    assert taste_service.recommendation_profile_state(snapshot)["state"] == "current"

    prop.property_subtype = new_subtype
    db.session.commit()

    assert taste_service.recommendation_profile_state(snapshot)["state"] == "dirty"


def test_changing_route_mode_invalidates_the_reference_snapshot(app):
    profile = SearchProfile(id=24, name="Houses", is_active=True)
    prop = _property(
        is_favorite=True,
        travel={
            "beaches": {
                "status": "ok",
                "mode": "walking",
                "items": [{"duration_min": 14}],
            }
        },
    )
    db.session.add_all([profile, prop])
    db.session.commit()
    snapshot = _snapshot()

    prop.travel = {
        "beaches": {
            "status": "ok",
            "mode": "driving",
            "items": [{"duration_min": 14}],
        }
    }
    db.session.commit()

    assert taste_service.recommendation_profile_state(snapshot)["state"] == "dirty"


@pytest.mark.parametrize(
    ("category", "subtype", "area_type", "saved_fingerprint"),
    [
        (
            "housing",
            "house",
            "built",
            "5658835e7f989dad0fe44c7b5eea2403ba2aae40052a1b6b40703a91e75aa94d",
        ),
        (
            "land",
            "plot",
            "plot",
            "7c2c3d5223a13d9a6380083003f1cae92e60cab979bdee85db59b9982797e740",
        ),
    ],
)
def test_unchanged_correct_kinds_keep_existing_photo_evidence_and_profile(
    app, category, subtype, area_type, saved_fingerprint
):
    # These fingerprints were recorded before the fix. The unaffected
    # house/land rows must not lose previously extracted photos or force a
    # profile rebuild merely because another kind was being read incorrectly.
    profile = SearchProfile(id=24, name="Reference", is_active=True)
    prop = _property(
        is_favorite=True,
        property_category=category,
        property_subtype=subtype,
        area_type=area_type,
        taste={
            "visual_descriptor": {
                "schema_version": 1,
                "input_fingerprint": "a" * 64,
                "property_fingerprint": saved_fingerprint,
                "visual_observations": [
                    {
                        "aspect_id": "house_condition",
                        "value": "weathered",
                        "status": "claimed",
                        "evidence": {
                            "source_kind": "photo",
                            "source_id": "property:9:photo:0",
                            "image_sha256": "b" * 64,
                            "image_index": 0,
                        },
                        "confidence": 0.8,
                        "limitation": "Only the visible facade is in frame.",
                    }
                ],
            }
        },
    )
    db.session.add_all([profile, prop])
    db.session.commit()
    snapshot = _snapshot()

    descriptor = taste_descriptors.build_descriptor(
        prop, visual_input=prop.taste["visual_descriptor"]
    )

    assert [
        (row["value"], row["source_kind"])
        for row in taste_descriptors.aspect_values(descriptor, "house_condition")
    ] == [("weathered", "photo")]
    assert taste_service.recommendation_profile_state(snapshot)["state"] == "current"


# Computed by the baseline 4f51a0d descriptor on this exact property. Its
# driving route used to hash as walking access, although the photos did not
# change when that interpretation was corrected.
_DRIVING_PHOTO_FINGERPRINT = (
    "02e7ca2c46d8c51bb40587978077b1413d4e43cacfaa5c0413ff2ac6ea9781a5"
)


def _legacy_driving_photo_property():
    return _property(
        is_favorite=True,
        travel={
            "beaches": {
                "status": "ok",
                "mode": "driving",
                "items": [{"duration_min": 14}],
            }
        },
        taste={
            "visual_descriptor": {
                "schema_version": 1,
                "input_fingerprint": "a" * 64,
                "property_fingerprint": _DRIVING_PHOTO_FINGERPRINT,
                "visual_observations": [
                    {
                        "aspect_id": "house_condition",
                        "value": "weathered",
                        "status": "claimed",
                        "evidence": {
                            "source_kind": "photo",
                            "source_id": "property:9:photo:0",
                            "image_sha256": "b" * 64,
                            "image_index": 0,
                        },
                        "confidence": 0.8,
                        "limitation": "Only the visible facade is in frame.",
                    }
                ],
            }
        },
    )


def test_legacy_driving_projection_keeps_photos_but_dirties_recommendations(app):
    profile = SearchProfile(id=24, name="Reference", is_active=True)
    prop = _legacy_driving_photo_property()
    db.session.add_all([profile, prop])
    db.session.commit()
    signals = taste_service.collect_signals()
    signals[0]["descriptor"]["input_fingerprint"] = _DRIVING_PHOTO_FINGERPRINT
    saved_profile = _snapshot()
    saved_profile["signals_fingerprint"] = taste_service.signals_fingerprint(signals)

    descriptor = taste_descriptors.build_descriptor(
        prop, visual_input=prop.taste["visual_descriptor"]
    )

    assert [
        (row["value"], row["source_kind"])
        for row in taste_descriptors.aspect_values(descriptor, "house_condition")
    ] == [("weathered", "photo")]
    assert taste_descriptors.aspect_values(descriptor, "beach_access") == []
    assert descriptor["input_fingerprint"] != _DRIVING_PHOTO_FINGERPRINT
    assert taste_service.recommendation_profile_state(saved_profile)["state"] == "dirty"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "Changed listing facts"),
        ("area", 250),
        ("plot_area", 1200),
        ("property_subtype", "apartment"),
        ("property_subtype", None),
    ],
)
def test_legacy_photo_compatibility_does_not_hide_other_changed_facts(field, value):
    prop = _legacy_driving_photo_property()
    setattr(prop, field, value)

    descriptor = taste_descriptors.build_descriptor(
        prop, visual_input=prop.taste["visual_descriptor"]
    )

    assert taste_descriptors.aspect_values(descriptor, "house_condition") == []
    assert "visual_descriptor_fingerprint" not in descriptor


def test_legacy_photo_compatibility_still_refuses_invalid_image_provenance():
    prop = _legacy_driving_photo_property()
    prop.taste["visual_descriptor"]["visual_observations"][0]["evidence"][
        "image_sha256"
    ] = "invalid"

    descriptor = taste_descriptors.build_descriptor(
        prop, visual_input=prop.taste["visual_descriptor"]
    )

    assert taste_descriptors.aspect_values(descriptor, "house_condition") == []


def test_visual_cli_does_not_reextract_valid_legacy_beach_photos(app, monkeypatch):
    import app as app_module
    from services import visual_input
    from tests.test_extract_visual_descriptors import _image, _without_real_marker
    from utils import extract_visual_descriptors

    _without_real_marker(monkeypatch)
    profile = SearchProfile(id=24, name="Reference", is_active=True)
    prop = _legacy_driving_photo_property()
    image = _image()
    envelope = visual_input.build_visual_input([image])
    observation = prop.taste["visual_descriptor"]["visual_observations"][0]
    observation["evidence"].update(
        source_id=image["source_id"], image_sha256=image["content_sha256"]
    )
    prop.taste = {
        "visual_descriptor": {
            **envelope,
            "property_fingerprint": _DRIVING_PHOTO_FINGERPRINT,
            "visual_observations": [observation],
        }
    }
    db.session.add_all([profile, prop])
    db.session.commit()
    monkeypatch.setattr(app_module, "create_app", lambda: app)
    monkeypatch.setattr(
        visual_input, "download_portal_photo_inputs", lambda *_args, **_kwargs: [image]
    )
    monkeypatch.setattr(
        visual_input,
        "extract_visual_observations",
        lambda *_args, **_kwargs: pytest.fail("unchanged photos requested extraction"),
    )

    assert extract_visual_descriptors.run(["--ids", str(prop.id), "--apply"]) == 0
    db.session.refresh(prop)
    assert prop.taste["visual_descriptor"]["property_fingerprint"] == (
        _DRIVING_PHOTO_FINGERPRINT
    )
