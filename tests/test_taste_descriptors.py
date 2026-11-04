"""Freshness tests for evidence-shaped taste descriptors."""

from types import SimpleNamespace

from services import taste_descriptors
import pytest


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "La segunda vivienda está completamente para rehabilitar.",
            "major_renovation",
        ),
        ("Aunque requiere algunas reformas menores.", "renovation_needed"),
        ("Se vende chalet forrado en piedra para terminar.", "unfinished"),
        ("La vivienda se encuentra para reforma integral.", "major_renovation"),
        ("La vivienda esta para restaurar.", "renovation_needed"),
        ("La casa necesita reformas.", "renovation_needed"),
    ],
)
def test_explicit_current_spanish_works_are_claimed_not_ignored(text, expected):
    prop = _property()
    prop.description = text
    rows = taste_descriptors.aspect_values(
        taste_descriptors.build_descriptor(prop), "house_condition"
    )
    assert any(row["value"] == expected and row["status"] == "claimed" for row in rows)


@pytest.mark.parametrize(
    "text",
    [
        "No major renovation required.",
        "Does not need major renovation.",
        "Sin reforma integral.",
        "No needs renovation.",
        "Recién rehabilitada en 2015, con reforma integral terminada.",
        "La casa está terminada; el bajo cubierta está sin terminar.",
        "La casa no está para reformar: está completamente reformada y lista para entrar a vivir.",
        "No es una casa para reformar.",
        "La casa no está en buen estado.",
    ],
)
def test_negated_renovation_is_not_a_candidate_drawback(text):
    prop = _property()
    prop.description = text
    assert (
        taste_descriptors.aspect_values(
            taste_descriptors.build_descriptor(prop), "house_condition"
        )
        == []
    )


def test_new_claim_reader_preserves_existing_photo_evidence():
    prop = _property()
    prop.attributes["condition"] = "para reformar"
    # Captured from the pre-fix descriptor against this unchanged fixture.
    old_hash = "8a978ec928b640911dda3f44fe852d0273727529c57f94091cc3525475e40023"
    assert taste_descriptors.visual_property_fingerprint_matches(prop, old_hash)
    before = taste_descriptors.input_fingerprint(prop)
    prop.attributes["condition"] = "buen estado"
    assert taste_descriptors.input_fingerprint(prop) != before


def test_a_street_named_pareada_is_not_evidence_of_attached_neighbours():
    prop = _property()
    prop.title = "Casa en venta en calle Pareada, Bergondo"
    assert (
        taste_descriptors.aspect_values(
            taste_descriptors.build_descriptor(prop), "neighbor_privacy"
        )
        == []
    )


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
