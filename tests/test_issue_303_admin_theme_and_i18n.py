"""Issue #303: admin pages follow the theme, and the navbar translates fully.

Two small promises, pinned the same way tests/test_tablet_list_layout.py pins
the tablet layout: the templates must carry the theme-aware classes, and the
stylesheet must actually define them.

*Theme.* `/profiles` rendered Bootstrap's `.table-light` on its header and on
the "No subscription" row. That class pins `#f8f9fa` whatever `data-bs-theme`
says, so in dark mode the page showed white slabs under a dark navbar -- and
the muted row's `.text-body-secondary` text was near-white on that white
(measured: background rgb(248,249,250), text rgba(222,226,230,0.75)).
`/profiles/<id>/edit` carried the same thead, and `/criteria` hid four
`bg-light` card headers in its market-settings section. The fix routes them
all through classes whose colors are theme variables: the existing
`.lands-table-head`, a new `.table-row-muted`, and Bootstrap's own
`bg-body-tertiary`. `table-light` has no legitimate use left in this app, so
it is banned from every template rather than from the two that carried it.

*i18n.* The navbar labelled Settings (and Map) with literal English while
every sibling went through `t()`. With the UI in Spanish, "Settings" stayed
English -- the QA finding -- so the labels now come from `utils/i18n.py` like
the rest of the navbar.
"""

import re
from pathlib import Path

import pytest

from app import create_app, db
from tests import setup_test_environment
from utils.i18n import TRANSLATIONS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
STYLESHEET = ROOT / "static" / "css" / "style.css"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class TestAdminPagesFollowTheTheme:
    def test_no_template_uses_table_light(self):
        """`.table-light` is hard-coded `#f8f9fa` -- a white slab in dark mode."""
        offenders = [
            template.name
            for template in sorted(TEMPLATES.glob("*.html"))
            if "table-light" in template.read_text(encoding="utf-8")
        ]
        assert not offenders, f"table-light is back in: {offenders}"

    def test_profiles_table_carries_the_themed_classes(self, client):
        body = client.get("/profiles").get_data(as_text=True)
        assert '<thead class="lands-table-head">' in body
        assert 'id="profiles-unassigned-row" class="table-row-muted"' in body

    def test_the_muted_row_class_is_defined_with_theme_variables(self):
        """A class the template renders with no themed rule behind it would be
        the white slab in disguise. The rule must set Bootstrap's table
        variables from theme variables, not from literal colors."""
        css = STYLESHEET.read_text(encoding="utf-8")
        start = css.index(".table-row-muted")
        rule = css[start : css.index("}", start)]
        assert "--bs-table-bg: var(--bs-tertiary-bg)" in rule
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rule), (
            f"literal color in .table-row-muted: {rule!r}"
        )

    def test_criteria_no_longer_hardcodes_bg_light(self):
        """The market-settings card headers were `bg-light`: invisible in the
        default view, white slabs once that section opens in dark mode.
        (`bg-light` on a *badge* next to `text-dark` is a deliberate look and
        stays legal elsewhere -- this pin is scoped to criteria.html.)"""
        text = (TEMPLATES / "criteria.html").read_text(encoding="utf-8")
        assert "bg-light" not in text


class TestNavbarTranslatesFully:
    def test_settings_and_map_have_translations_in_both_languages(self):
        for lang in ("en", "es"):
            for key in ("settings", "map"):
                assert TRANSLATIONS[lang].get(key), f"missing {lang}:{key}"
        assert TRANSLATIONS["es"]["settings"] != TRANSLATIONS["en"]["settings"], (
            "the Spanish navbar still shows English 'Settings'"
        )

    def test_navbar_renders_spanish_settings_label(self, client):
        with client.session_transaction() as session:
            session["language"] = "es"
        body = client.get("/profiles").get_data(as_text=True)
        assert TRANSLATIONS["es"]["settings"] in body
        assert TRANSLATIONS["es"]["map"] in body
        # The literal English words must not survive as nav labels: they only
        # ever appeared as `icon</i>Label`, so that shape is what is banned.
        assert not re.search(r"</i>Settings\s*<", body)
        assert not re.search(r"</i>Map\s*<", body)

    def test_navbar_still_english_by_default(self, client):
        body = client.get("/profiles").get_data(as_text=True)
        assert re.search(r"</i>Settings\s*<", body)
