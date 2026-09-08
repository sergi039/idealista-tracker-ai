"""Preference polarity stays attached to the owner's local wording."""

from services import taste_preferences


def _clauses(reason, verdict):
    return taste_preferences.compile_signal(
        {"property_id": 88, "profile_id": 7, "verdict": verdict, "reason": reason}
    )


def test_rejected_mixed_sentence_keeps_its_positive_house_and_negative_plot():
    clauses = _clauses("Нравится каменный дом, но участок маленький", "rejected")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["house_character"]["polarity"] == "prefer"
    assert by_aspect["house_character"]["mapping_state"] == "executable"
    assert by_aspect["plot_area_m2"]["polarity"] == "avoid"
    assert by_aspect["plot_area_m2"]["mapping_state"] == "executable"
    assert all(clause["scope"] == "profile" for clause in clauses)


def test_tolerated_negative_on_a_positive_listing_is_a_local_tradeoff():
    clauses = _clauses("сырость — минус, но терпимо", "interested")

    assert len(clauses) == 1
    clause = clauses[0]
    assert clause["aspect_id"] == "house_condition"
    assert clause["polarity"] == "tradeoff"
    assert clause["mapping_state"] == "executable"
    assert clause["scope"] == "profile"


def test_ambiguous_mapped_fact_is_visible_but_not_executable():
    clauses = _clauses("каменный дом", "interested")

    assert len(clauses) == 1
    assert clauses[0]["aspect_id"] == "house_character"
    assert clauses[0]["polarity"] == "unresolved"
    assert clauses[0]["mapping_state"] == "unmapped"
    assert "not explicit" in clauses[0]["reason"]


def test_explicit_visual_negative_does_not_inherit_the_listing_verdict():
    clauses = _clauses("сельхоз постройки рядом, не красиво", "rejected")
    by_aspect = {clause["aspect_id"]: clause for clause in clauses}

    assert by_aspect["visual_appeal"]["polarity"] == "avoid"
    assert by_aspect["visual_appeal"]["mapping_state"] == "executable"


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
    assert clause(1282, "utilities")["polarity"] == "tradeoff"
    assert clause(970, "neighbor_privacy")["polarity"] == "avoid"
    kind = clause(1742, "property_kind")
    assert (kind["polarity"], kind["scope"]) == ("avoid", "profile")
    assert clause(786, "plot_outline")["mapping_state"] == "unmapped"
    assert clause(786, "utilities")["mapping_state"] == "unmapped"
