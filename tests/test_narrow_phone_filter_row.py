"""The filter bar on the narrowest phones (owner, 2026-11-16).

Measured on production at 320px, in Spanish: the two switches were two 136px
halves of a 288px line, and "Ocultar retirados" needs 154px with its padding,
so its icon sat on the button's border; beside the Filters trigger and Apply
the Decision select was left 93px of the 189px its label needs and read
"Decisió…" (101px of 171px in English), which does not say what is selected.

Under 360px the switches now share their line by content and the select takes
a line of its own. From 360px nothing changes: the halves hold both labels
there, and docs/design/README.md#phone-layout allows the extra line only for
"Spanish labels and a 320px screen".

In Spanish the label stayed cut from 360px ("Decisión: sin rec…": 133px of
189px at 360px, 163px at 390px) and fits beside the two only from 416px, so
under 420px the Spanish page gives the select its own line too. English is not
touched there: its label fits at the 390px the acceptance is measured at.

The layout is proven by the browser measurement in the PR
(docs/rules/testing.md). What a test can pin is what that measurement stood
on: the rules as the cascade leaves them on both sides of the breakpoint, read
with the resolver `tests/test_row_density.py` already owns, and the wrapping
container the own-line rule relies on.
"""

import re

import pytest

from app import create_app, db
from tests import setup_test_environment
from tests.test_row_density import effective

SWITCH = ".app-workspace #filters-card .scope-switch"
SELECT = ".properties-queue-switch"
BAR = ".app-workspace #filters-card > .card-body"
SPANISH_SELECT = 'html[lang="es"] .properties-queue-switch'

NARROW = (320, 340, 359)
PHONE = (360, 375, 390, 767)
SPANISH_OWN_LINE = (320, 360, 375, 390, 414, 419)
SPANISH_IN_LINE = (420, 430, 767, 768, 1440)


class TestUnder360TheSwitchesShareTheLineByContent:
    @pytest.mark.parametrize("width", NARROW)
    def test_a_switch_is_as_wide_as_its_label_needs(self, width):
        assert effective(SWITCH, "flex", width) == "1 1 auto"

    @pytest.mark.parametrize("width", PHONE)
    def test_from_360_they_are_two_halves_again(self, width):
        assert effective(SWITCH, "flex", width) == ("1 1 calc(50% - var(--ui-space-1))")


class TestUnder360TheDecisionSelectTakesItsOwnLine:
    @pytest.mark.parametrize("width", NARROW)
    def test_the_select_asks_for_the_whole_line(self, width):
        assert effective(SELECT, "flex", width) == "1 1 100%"

    @pytest.mark.parametrize("width", NARROW)
    def test_the_bar_wraps_so_the_trigger_and_apply_follow_it(self, width):
        """A full-line basis moves nothing in a bar that does not wrap: it
        would only squeeze the select back between its neighbours."""
        assert effective(BAR, "flex-wrap", width) == "wrap"

    @pytest.mark.parametrize("width", PHONE)
    def test_from_360_it_keeps_its_place_beside_the_trigger_and_apply(self, width):
        """The acceptance in docs/design/README.md#phone-layout is measured at
        390px: an extra line there moves the first listing past 480px."""
        assert effective(SELECT, "flex", width) == "1 1 0"

    @pytest.mark.parametrize("width", (768, 1440))
    def test_neither_rule_reaches_a_wider_screen(self, width):
        assert effective(SELECT, "flex", width) != "1 1 100%"
        assert effective(SWITCH, "flex", width) != "1 1 auto"


class TestUnder420TheSpanishSelectTakesItsOwnLine:
    @pytest.mark.parametrize("width", SPANISH_OWN_LINE)
    def test_the_spanish_select_asks_for_the_whole_line(self, width):
        assert effective(SPANISH_SELECT, "flex", width) == "1 1 100%"

    @pytest.mark.parametrize("width", SPANISH_OWN_LINE)
    def test_the_bar_wraps_there_too(self, width):
        assert effective(BAR, "flex-wrap", width) == "wrap"

    @pytest.mark.parametrize("width", SPANISH_IN_LINE)
    def test_from_420_the_label_fits_and_no_spanish_rule_applies(self, width):
        assert effective(SPANISH_SELECT, "flex", width) is None

    @pytest.mark.parametrize("width", (360, 375, 390, 414, 419))
    def test_the_rule_every_language_reads_is_unchanged_there(self, width):
        """The Spanish rule is a second selector, not a wider breakpoint on
        the first: an English page keeps the select in the line from 360px."""
        assert effective(SELECT, "flex", width) == "1 1 0"


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.drop_all()


class TestThePageSaysItsLanguageWhereTheRuleLooks:
    @pytest.mark.parametrize("language", ("en", "es"))
    def test_the_html_element_carries_the_language_in_use(self, client, language):
        """`html[lang="es"]` matches nothing unless the page writes the
        language it was rendered in on its root element, and it must not
        match a page rendered in English."""
        answer = client.post("/api/set-language", json={"language": language})
        assert answer.status_code == 200, "the language did not switch"
        body = client.get("/properties").get_data(as_text=True)
        root = re.search(r"<html\b[^>]*>", body)
        assert root, "the page drew no html element"
        assert f'lang="{language}"' in root.group(0)
