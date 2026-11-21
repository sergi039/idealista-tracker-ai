"""On a phone the top bar's Manual Sync button is its icon (owner, 2026-11-16).

Measured on production: with the words drawn, the Spanish label
("Sincronización Manual") made the bar 359px wide, so the page scrolled
sideways on any narrower screen and at 320px the language button was off
screen; in both languages the label wrapped to two lines on every phone and
the bar grew from 53px to 61px.

The words now sit in `.utility-label`, which the phone block of the stylesheet
hides. The layout is proven by the browser measurement in the PR
(docs/rules/testing.md). What a test can pin is what that measurement stood
on: the rule as the cascade leaves it at each width, and the markup the rule
assumes -- every word of the button inside the hidden span, and the button's
name kept in `aria-label` for where the words are not drawn.
"""

import re
from html import unescape

import pytest

from app import create_app, db
from tests import setup_test_environment
from tests.test_row_density import effective

LABELS = {"en": "Manual Sync", "es": "Sincronización Manual"}


class TestTheLabelIsHiddenUnderTheTabletBreakpoint:
    @pytest.mark.parametrize("width", (320, 375, 767))
    def test_a_phone_does_not_draw_the_words(self, width):
        assert (
            effective(".workspace-utilities .utility-label", "display", width) == "none"
        )

    @pytest.mark.parametrize("width", (768, 1440))
    def test_a_wider_screen_draws_them(self, width):
        assert (
            effective(".workspace-utilities .utility-label", "display", width) is None
        )


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    # The control is drawn only on the machine that ingests
    # (services/ingest_policy.py reads this from `app.config`).
    app.config["AUTO_START_SCHEDULER"] = True
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.drop_all()


def _sync_button(client, language):
    answer = client.post("/api/set-language", json={"language": language})
    assert answer.status_code == 200, "the language did not switch"
    body = client.get("/properties").get_data(as_text=True)
    bar = re.search(r'<header class="workspace-topbar">(.*?)</header>', body, re.DOTALL)
    assert bar, "the page drew no top bar"
    button = re.search(
        r'(<button\b[^>]*hx-post="[^"]*ingest/email/run"[^>]*>)(.*?)</button>',
        bar.group(1),
        re.DOTALL,
    )
    assert button, "the top bar drew no Manual Sync button"
    return unescape(button.group(1)), unescape(button.group(2))


class TestTheMarkupTheRuleAssumes:
    @pytest.mark.parametrize("language", sorted(LABELS))
    def test_every_word_of_the_button_is_inside_the_hidden_span(self, client, language):
        _, inner = _sync_button(client, language)
        span = re.search(r'<span class="utility-label">(.*?)</span>', inner, re.DOTALL)
        assert span, "the label is not in .utility-label, so a phone draws it"
        assert span.group(1).strip() == LABELS[language]
        outside = re.sub(r"<i\b[^>]*></i>", "", inner.replace(span.group(0), ""))
        assert outside.strip() == "", f"words drawn outside the span: {outside!r}"

    @pytest.mark.parametrize("language", sorted(LABELS))
    def test_the_button_keeps_its_name_where_the_words_are_not_drawn(
        self, client, language
    ):
        opening, _ = _sync_button(client, language)
        assert f'aria-label="{LABELS[language]}"' in opening
