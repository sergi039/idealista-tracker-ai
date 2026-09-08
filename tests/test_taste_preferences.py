"""Preference polarity stays attached to the owner's local wording."""

from services import taste_preferences


def _clauses(reason, verdict):
    return taste_preferences.compile_signal(
        {"property_id": 88, "profile_id": 7, "verdict": verdict, "reason": reason}
    )


def test_rejected_mixed_sentence_keeps_positive_house_and_visible_unmeasured_plot():
    clauses = _clauses("Нравится каменный дом, но участок маленький", "rejected")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["house_character"]["polarity"] == "prefer"
    assert by_aspect["house_character"]["mapping_state"] == "executable"
    assert by_aspect["plot_area_m2"]["polarity"] == "avoid"
    assert by_aspect["plot_area_m2"]["mapping_state"] == "unmapped"
    assert by_aspect["plot_area_m2"]["reason"] == "no comparable canonical value"
    assert all(clause["scope"] == "profile" for clause in clauses)


def test_tolerated_negative_on_a_positive_listing_is_a_local_tradeoff():
    clauses = _clauses("сырость — минус, но терпимо", "interested")

    assert len(clauses) == 1
    clause = clauses[0]
    assert clause["aspect_id"] == "house_condition"
    assert clause["polarity"] == "tradeoff"
    assert clause["mapping_state"] == "executable"
    assert clause["scope"] == "profile"


def test_negated_desire_is_a_global_hard_avoidance():
    clauses = _clauses("Никогда не хочу оптика 1 Гбит", "rejected")

    assert len(clauses) == 1
    clause = clauses[0]
    assert (
        clause["aspect_id"],
        clause["polarity"],
        clause["strength"],
        clause["scope"],
    ) == (
        "fiber",
        "avoid",
        "hard",
        "global",
    )
    assert clause["mapping_state"] == "executable"


def test_negated_desires_with_modifier_chains_compile_as_avoidance():
    cases = (
        ("не очень нравится каменный дом", "house_character"),
        ("участок правильной формы не особо подходит", "plot_outline"),
        ("не совсем нравится вид на море", "sea_view"),
        ("каменный дом — не то, что хочу", "house_character"),
        ("участок правильной формы не так уж и нравится", "plot_outline"),
        ("не очень-то нравится вид на море", "sea_view"),
    )

    for reason, aspect_id in cases:
        clause = next(
            item
            for item in _clauses(reason, "interested")
            if item["aspect_id"] == aspect_id
        )
        assert clause["polarity"] == "avoid"
        assert clause["mapping_state"] == "executable"


def test_noun_negation_before_desire_is_visible_but_never_inherits_prefer_heading():
    clauses = _clauses("Нравится: не каменный дом, который хочу", "interested")

    assert len(clauses) == 1
    clause = clauses[0]
    assert clause["aspect_id"] == "house_character"
    assert clause["polarity"] == "unresolved"
    assert clause["mapping_state"] == "unmapped"
    assert clause["reason"] == "desire negation is ambiguous"


def test_post_verb_object_negation_is_unresolved_not_a_preference_for_first_value():
    clause = next(
        item
        for item in _clauses("нравится не каменный дом, а деревянный", "interested")
        if item["aspect_id"] == "house_character"
    )

    assert clause["polarity"] == "unresolved"
    assert clause["mapping_state"] == "unmapped"
    assert clause["reason"] == "desire negation is ambiguous"


def test_clause_values_bind_each_shape_to_its_own_clause():
    clauses = _clauses(
        "L-образная форма никогда не подходит; ровный вытянутый прямоугольник подходит",
        "rejected",
    )
    by_text = {clause["text"]: clause for clause in clauses}

    assert by_text["L-образная форма никогда не подходит"]["values"] == ["notched"]
    assert by_text["ровный вытянутый прямоугольник подходит"]["values"] == ["regular"]


def test_explicit_preferences_without_a_canonical_value_remain_unapplied():
    clause = next(
        item
        for item in _clauses("Нравится: скважина", "interested")
        if item["aspect_id"] == "utilities"
    )

    assert clause["polarity"] == "prefer"
    assert clause["values"] == []
    assert clause["mapping_state"] == "unmapped"
    assert clause["reason"] == "no comparable canonical value"


def test_comparable_clause_values_remain_executable_for_common_aspects():
    clauses = _clauses(
        "Нравится: вид на море; каменный крестьянский дом; оптика 1 Гбит. "
        "Никогда не хочу участок, а не дом.",
        "interested",
    )
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["sea_view"]["values"] == ["present"]
    assert by_aspect["house_character"]["values"] == [
        "old_farmhouse",
        "stone_house",
    ]
    assert by_aspect["fiber"]["values"] == ["present"]
    assert by_aspect["property_kind"]["values"] == ["land"]
    assert by_aspect["house_character"]["mapping_state"] == "unmapped"
    assert by_aspect["house_character"]["reason"] == (
        "multiple canonical values need an explicit relationship"
    )
    assert all(
        by_aspect[aspect]["mapping_state"] == "executable"
        for aspect in ("sea_view", "fiber", "property_kind")
    )


def test_heading_polarity_stops_at_sentence_and_factual_observation_stays_unresolved():
    clauses = _clauses("Нравится: каменный дом. Рядом сельхоз постройки.", "interested")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["house_character"]["polarity"] == "prefer"
    assert by_aspect["agricultural_context"]["polarity"] == "unresolved"
    assert by_aspect["agricultural_context"]["mapping_state"] == "unmapped"


def test_intrinsic_avoid_outranks_inherited_heading_polarity():
    clauses = _clauses("Нравится: каменный дом; рядом с дорогой.", "interested")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["road_proximity"]["polarity"] == "avoid"


def test_heading_precedence_is_aspect_local_across_separators():
    cases = (
        (
            "Нравится: вид на море, не нравится рядом с дорогой",
            "sea_view",
            "road_proximity",
            "prefer",
            "avoid",
        ),
        (
            "Нравится: каменный дом; рядом с дорогой",
            "house_character",
            "road_proximity",
            "prefer",
            "avoid",
        ),
        (
            "Нравится: каменный дом, сырость — минус",
            "house_character",
            "house_condition",
            "prefer",
            "avoid",
        ),
        (
            "Нравится: каменный дом. Рядом с дорогой.",
            "house_character",
            "road_proximity",
            "prefer",
            "avoid",
        ),
    )

    for reason, positive_aspect, local_aspect, positive, local in cases:
        by_aspect = {
            clause["aspect_id"]: clause for clause in _clauses(reason, "interested")
        }
        assert by_aspect[positive_aspect]["polarity"] == positive
        assert by_aspect[local_aspect]["polarity"] == local


def test_liked_listing_negative_heading_keeps_tradeoff_across_comma_items():
    clauses = _clauses("Минусы: сырость, скважина", "interested")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["house_condition"]["polarity"] == "tradeoff"
    assert by_aspect["utilities"]["polarity"] == "tradeoff"


def test_only_exact_isthmus_term_maps_plot_outline_clause():
    inherited = _clauses("дом перешёл по наследству", "interested")
    isthmus = _clauses("не нравится перешеек участка", "interested")

    assert not any(clause["aspect_id"] == "plot_outline" for clause in inherited)
    assert (
        next(clause for clause in isthmus if clause["aspect_id"] == "plot_outline")[
            "polarity"
        ]
        == "avoid"
    )


def test_heading_keeps_semicolon_items_but_leaves_following_ambiguous_fact_unresolved():
    clauses = _clauses(
        "Нравится: каменный дом; вид на море. Участок правильной формы.",
        "interested",
    )
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["house_character"]["polarity"] == "prefer"
    assert by_aspect["sea_view"]["polarity"] == "prefer"
    assert by_aspect["plot_outline"]["polarity"] == "unresolved"
    assert by_aspect["plot_outline"]["mapping_state"] == "unmapped"


def test_ambiguous_mapped_fact_is_visible_but_not_executable():
    clauses = _clauses("каменный дом", "interested")

    assert len(clauses) == 1
    assert clauses[0]["aspect_id"] == "house_character"
    assert clauses[0]["polarity"] == "unresolved"
    assert clauses[0]["mapping_state"] == "unmapped"
    assert "not explicit" in clauses[0]["reason"]


def test_explicit_visual_negative_stays_visible_without_inventing_a_comparable_value():
    clauses = _clauses("сельхоз постройки рядом, не красиво", "rejected")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["visual_appeal"]["polarity"] == "avoid"
    assert by_aspect["visual_appeal"]["mapping_state"] == "unmapped"
    assert by_aspect["visual_appeal"]["reason"] == "no comparable canonical value"


def test_observed_comment_sections_keep_decisive_clauses_executable():
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
            "reason": "Нравится: участок правильной формы; Терпимые минусы: моря не видно, сырость наверху.",
        },
        {
            "property_id": 1282,
            "profile_id": 24,
            "verdict": "interested",
            "reason": "Нравится: вид на море; Минусы: скважина и септик; оптики на парцеле нет.",
        },
        {
            "property_id": 970,
            "profile_id": 24,
            "verdict": "rejected",
            "reason": "чужие дома близко",
        },
        {
            "property_id": 1742,
            "profile_id": 24,
            "verdict": "rejected",
            "reason": "это участок, а не дом",
        },
        {
            "property_id": 786,
            "profile_id": 24,
            "verdict": "interested",
            "reason": "форма компактная, Núcleo Rural. Открыты канализация и certificado urbanístico",
        },
    ]
    clauses = taste_preferences.compile_signals(signals)

    def clause(source_id, aspect):
        return next(
            item
            for item in clauses
            if item["source_property_id"] == source_id and item["aspect_id"] == aspect
        )

    global_shape = clause(774, "plot_outline")
    assert (
        global_shape["polarity"],
        global_shape["strength"],
        global_shape["scope"],
    ) == (
        "avoid",
        "hard",
        "global",
    )
    assert clause(969, "plot_outline")["polarity"] == "prefer"
    assert clause(969, "sea_view")["polarity"] == "tradeoff"
    assert clause(1282, "sea_view")["polarity"] == "prefer"
    assert clause(1282, "utilities")["mapping_state"] == "unmapped"
    assert clause(970, "neighbor_privacy")["polarity"] == "avoid"
    kind = clause(1742, "property_kind")
    assert (kind["polarity"], kind["scope"]) == ("avoid", "profile")
    assert clause(786, "plot_outline")["mapping_state"] == "unmapped"
    assert clause(786, "utilities")["mapping_state"] == "unmapped"
