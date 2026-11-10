"""A full stop inside a token is not a sentence boundary (#638)."""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import taste_preferences, taste_service
from tests import setup_test_environment


def _texts(reason):
    return [text for text, _ in taste_preferences._segments(reason)]


@pytest.mark.parametrize(
    "reason",
    [
        "Писать на owner@example.com по этому дому",
        "Смотрел на example.com в субботу",
        "Объявление https://www.example.com/path/to.html хорошее",
        "Звонил 08.11.2026 агенту насчет цены",
        "Пляж в 1.5 km от дома",
    ],
)
def test_one_sentence_with_a_dotted_token_stays_one_clause(reason):
    texts = _texts(reason)
    assert len(texts) == 1
    assert texts[0].startswith(reason.split()[0])


def test_the_token_survives_inside_the_clause():
    assert "owner@example.com" in _texts("Писать на owner@example.com по дому")[0]
    assert "08.11.2026" in _texts("Звонил 08.11.2026 агенту")[0]
    assert "1.5 km" in _texts("Пляж в 1.5 km от дома")[0]
    assert (
        "https://www.example.com/a.html"
        in _texts("Ссылка https://www.example.com/a.html вот")[0]
    )


def test_ellipsis_ends_the_clause_once_without_leaving_dots_behind():
    assert _texts("Дорога рядом... Участок маленький") == [
        "Дорога рядом",
        "Участок маленький",
    ]


def test_two_real_sentences_still_split():
    assert _texts("Дом нравится. Участок маленький!") == [
        "Дом нравится",
        "Участок маленький",
    ]
    assert _texts("Casa bonita. Parcela pequeña? Sin jardín; sí") == [
        "Casa bonita",
        "Parcela pequeña",
        "Sin jardín",
        "sí",
    ]


def test_semicolon_still_marks_a_heading_carry_over():
    assert taste_preferences._segments("Нравится: каменный дом; пляж рядом") == [
        ("Нравится: каменный дом", True),
        ("пляж рядом", False),
    ]


def test_cyrillic_and_spanish_letters_count_as_letters():
    assert taste_preferences.is_meaningful_clause("дом")
    assert taste_preferences.is_meaningful_clause("дома")
    assert taste_preferences.is_meaningful_clause("jardín")
    assert taste_preferences.is_meaningful_clause("за 300")
    assert not taste_preferences.is_meaningful_clause("08.11")
    assert not taste_preferences.is_meaningful_clause("com)")
    assert not taste_preferences.is_meaningful_clause(None)


def test_compiled_clauses_hold_no_fragment_without_letters():
    clauses = taste_preferences.compile_signal(
        {
            "property_id": 5,
            "profile_id": 1,
            "verdict": "rejected",
            "reason": "Агент owner@example.com, звонить 08.11.2026. 1.5. Участок маленький.",
        }
    )
    assert clauses
    for clause in clauses:
        assert taste_preferences.is_meaningful_clause(clause["text"])
        assert clause["text"] not in {"com", "08", "11", "2026", "5"}
    assert any("owner@example.com" in clause["text"] for clause in clauses)


def test_stored_fragments_are_not_listed_as_unapplied():
    clauses = [
        {"text": "com)", "mapping_state": "unmapped"},
        {"text": "08", "mapping_state": "unmapped"},
        {"text": "Участок маленький", "mapping_state": "unmapped"},
        {"text": "Каменный дом", "mapping_state": "executable"},
    ]
    assert [c["text"] for c in taste_service._unapplied_clauses(clauses)] == [
        "Участок маленький"
    ]


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _page(app, reason):
    profile = SearchProfile(name="Galicia", is_active=True)
    db.session.add(profile)
    db.session.commit()
    db.session.add(
        Property(
            source_email_id="t638:1",
            title="Casa",
            price=100000,
            area=100,
            municipality="Malpica",
            search_profile_id=profile.id,
            owner_verdict="rejected",
            owner_verdict_reason=reason,
        )
    )
    db.session.commit()
    return app.test_client().get("/properties?profile_id=all")


def test_page_lists_the_real_preference_and_no_fragments(app):
    page = _page(app, "Писать на agent@example.com 08.11.2026. Фантазийное пожелание.")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'id="recommendation-unapplied-preferences"' in body
    assert "Фантазийное пожелание" in body
    assert "agent@example.com 08.11.2026" in body
    assert "2 preference(s) are not used yet" in body
    assert body.count("<li>") >= 2
    summary = body.split('id="recommendation-unapplied-preferences"')[1]
    items = summary.split("</ol>")[0].count("<li>")
    assert items == 2


def test_block_is_absent_when_nothing_real_is_left(app):
    page = _page(app, "08. 11. 1.")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'id="recommendation-unapplied-preferences"' not in body


@pytest.mark.parametrize("word", ["mar", "luz", "сад", "вид", "jardín", "well-kept"])
def test_a_short_real_word_is_still_a_preference(word):
    assert taste_preferences.is_meaningful_clause(word)


@pytest.mark.parametrize("fragment", ["com)", "08", "example50", "(", "a", "x.y"])
def test_a_stray_fragment_is_not_a_preference(fragment):
    assert not taste_preferences.is_meaningful_clause(fragment)


def test_short_items_of_a_semicolon_list_all_survive():
    clauses = [text for text, _ in taste_preferences._segments("jardín; mar; luz.")]
    assert clauses == ["jardín", "mar", "luz"]


def test_a_long_run_of_dots_is_scanned_in_linear_time():
    import time

    text = "." * 200_000 + "x"
    started = time.perf_counter()
    taste_preferences._segments(text)
    assert time.perf_counter() - started < 2.0


def test_spanish_opening_marks_do_not_cost_a_preference():
    clauses = [text for text, _ in taste_preferences._segments("Jardín; ¡luz! ¿Mar?")]
    assert clauses == ["Jardín", "luz", "Mar"]
