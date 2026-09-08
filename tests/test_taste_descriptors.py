"""Freshness tests for evidence-shaped taste descriptors."""

from types import SimpleNamespace

from services import taste_descriptors


def _property():
    return SimpleNamespace(
        id=9,
        area=120,
        plot_area=900,
        area_type="built",
        property_category="house",
        property_subtype="detached_house",
        description="Traditional stone house.",
        attributes={"dossier": {"plot_area_m2": 910}},
        enrichment={
            "cadastre": {
                "geometry": {"area_m2": 920},
                "measured_at": "2026-09-08T10:00:00Z",
            },
            "advertiser": {"checked_at": "2026-09-08T10:00:00Z"},
        },
        environment={
            "sea_view": "likely",
            "sea_view_detail": {
                "source": "geometry",
                "measured_at": "2026-09-08T10:00:00Z",
            },
            "other_checked_at": "2026-09-08T10:00:00Z",
        },
        travel={
            "beaches": {
                "status": "ok",
                "items": [{"duration_min": 12}],
                "measured_at": "2026-09-08T10:00:00Z",
                "route_checked_at": "2026-09-08T10:00:00Z",
            }
        },
    )


def test_input_fingerprint_ignores_unconsumed_operational_timestamps():
    prop = _property()
    original = taste_descriptors.input_fingerprint(prop)

    prop.enrichment["advertiser"]["checked_at"] = "2026-09-09T10:00:00Z"
    prop.environment["other_checked_at"] = "2026-09-09T10:00:00Z"
    prop.travel["beaches"]["route_checked_at"] = "2026-09-09T10:00:00Z"

    assert taste_descriptors.input_fingerprint(prop) == original


def test_input_fingerprint_tracks_consumed_measurement_value_and_provenance():
    prop = _property()
    original = taste_descriptors.input_fingerprint(prop)

    prop.enrichment["cadastre"]["geometry"]["area_m2"] = 950
    assert taste_descriptors.input_fingerprint(prop) != original

    prop = _property()
    original = taste_descriptors.input_fingerprint(prop)
    prop.environment["sea_view_detail"]["source"] = "manual"
    assert taste_descriptors.input_fingerprint(prop) != original


def test_visual_descriptor_fingerprint_tracks_validated_observation_content():
    prop = _property()
    fingerprint = taste_descriptors.input_fingerprint(prop)
    visual = {
        "schema_version": 1,
        "input_fingerprint": "a" * 64,
        "property_fingerprint": fingerprint,
        "visual_observations": [
            {
                "aspect_id": "house_condition",
                "value": "weathered",
                "status": "supported",
                "evidence": {
                    "source_kind": "photo",
                    "source_id": "property:9:photo:0",
                    "image_sha256": "b" * 64,
                    "image_index": 0,
                },
                "confidence": 0.8,
                "limitation": "Only the visible exterior is in frame.",
            }
        ],
    }
    first = taste_descriptors.build_descriptor(prop, visual_input=visual)
    visual["visual_observations"][0]["limitation"] = "Only one facade is visible."
    second = taste_descriptors.build_descriptor(prop, visual_input=visual)

    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert (
        first["visual_descriptor_fingerprint"]
        != second["visual_descriptor_fingerprint"]
    )


def test_owner_clause_values_bind_only_canonical_descriptor_values():
    assert taste_descriptors.text_claim_values("Нравится вид на море") == {
        "sea_view": ["present"]
    }
    assert taste_descriptors.text_claim_values("Моря не видно") == {"sea_view": ["no"]}
    assert taste_descriptors.text_claim_values("это участок, а не дом") == {
        "property_kind": ["land"]
    }
    assert taste_descriptors.text_claim_values(
        "каменный крестьянский дом, рядом много построек"
    ) == {
        "house_character": ["old_farmhouse", "stone_house"],
        "nearby_buildings": ["dense_visible"],
        "neighbor_privacy": ["low"],
    }
    assert taste_descriptors.text_claim_values("оптики на парцеле нет") == {
        "fiber": ["absent"]
    }
    for absence in ("оптики нет", "оптика отсутствует", "fibra no disponible"):
        assert taste_descriptors.text_claim_values(absence) == {"fiber": ["absent"]}
    assert taste_descriptors.text_claim_values("Есть ли оптика?") == {}
    assert taste_descriptors.text_claim_values("Не хочу оптику") == {
        "fiber": ["present"]
    }


def test_text_claims_reject_unrelated_walks_and_russian_word_prefixes():
    inherited = list(
        taste_descriptors._text_claims(
            "Дом перешел по наследству; пешком до магазина пять минут.",
            source_kind="listing_claim",
            source_id="test",
        )
    )
    assert inherited == []

    beach = list(
        taste_descriptors._text_claims(
            "До пляжа пешком десять минут.",
            source_kind="listing_claim",
            source_id="test",
        )
    )
    assert [(aspect, row["value"]) for aspect, row in beach] == [
        ("beach_access", "walkable")
    ]

    for unsupported_access in (
        "До пляжа пешком 40 минут.",
        "До пляжа пешком не дойти.",
        "Пляж 488 м.",
    ):
        assert (
            list(
                taste_descriptors._text_claims(
                    unsupported_access,
                    source_kind="listing_claim",
                    source_id="test",
                )
            )
            == []
        )
    explicit_access = list(
        taste_descriptors._text_claims(
            "Пляж в пешей доступности.",
            source_kind="listing_claim",
            source_id="test",
        )
    )
    assert [(aspect, row["value"]) for aspect, row in explicit_access] == [
        ("beach_access", "walkable")
    ]

    mixed_sentences = list(
        taste_descriptors._text_claims(
            "Пешком до магазина. Далеко от пляжа.",
            source_kind="listing_claim",
            source_id="test",
        )
    )
    assert [(aspect, row["value"]) for aspect, row in mixed_sentences] == [
        ("beach_access", "far")
    ]
