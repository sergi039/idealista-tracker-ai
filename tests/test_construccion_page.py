"""The /construccion page (design review PASS WITH FIXES, 2026-08-21).

The #98 contract, rendered: a topic requiring local confirmation shows a
value node (data-test="value") ONLY for a `present` record; `not_confirmed`
and `not_researched` are different states with different markers; an invalid
?concejo= yields no overlay; a missing identity snapshot refuses the page.

The render expectations are split by state on purpose (the review's last
mandatory fix): missing/empty → "not researched" and no value node;
`not_confirmed` → its own status plus `searched`, and equally no value node.
The check is against the DOM marker, not "no digits in the text" — values can
be categorical, and `searched` legitimately holds article numbers and dates.
"""

import json

import pytest

from app import create_app, db
from services import concejo_legal
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _write_concejo(tmp_path, monkeypatch, code, payload):
    concejos = tmp_path / "concejos"
    concejos.mkdir(exist_ok=True)
    (concejos / f"{code}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(concejo_legal, "CONCEJOS_DIR", concejos)


class TestPage:
    def test_renders_with_all_78_options(self, client):
        resp = client.get("/construccion")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'data-test="coverage-line"' in html
        assert html.count("<option") == 79  # 78 concejos + "not selected"

    def test_invalid_code_shows_no_overlay(self, client):
        for bad in ("33999", "15030", "abc"):
            resp = client.get(f"/construccion?concejo={bad}")
            html = resp.get_data(as_text=True)
            assert resp.status_code == 200
            assert 'data-test="code-rejected"' in html
            assert 'data-test="not-researched-banner"' not in html
            assert 'data-test="value"' not in html


class TestNotResearched:
    def test_a_concejo_without_a_file_shows_states_and_no_values(
        self, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(concejo_legal, "CONCEJOS_DIR", tmp_path / "none")
        resp = client.get("/construccion?concejo=33049")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'data-test="not-researched-banner"' in html
        assert 'data-test="state-not-researched"' in html
        assert 'data-test="value"' not in html
        # The regional statute row is its own subject and still renders.
        assert 'data-test="regional-row"' in html

    def test_an_empty_file_is_still_not_researched(self, client, tmp_path, monkeypatch):
        _write_concejo(
            tmp_path,
            monkeypatch,
            "33049",
            {"ine_code": "33049", "display_name": "Piloña", "facts": []},
        )
        resp = client.get("/construccion?concejo=33049")
        html = resp.get_data(as_text=True)
        assert 'data-test="not-researched-banner"' in html
        assert 'data-test="value"' not in html


class TestStatesAreDistinct:
    def test_present_and_not_confirmed_render_differently(
        self, client, tmp_path, monkeypatch
    ):
        _write_concejo(
            tmp_path,
            monkeypatch,
            "33016",
            {
                "ine_code": "33016",
                "display_name": "Castrillón",
                "facts": [
                    {
                        "topic": "pgo_status",
                        "state": "present",
                        "value": "approved",
                        "confidence": "verified",
                        "source": "BOPA (fixture)",
                        "source_url": "https://example.org/bopa",
                        "checked_at": "2026-08-01",
                    },
                    {
                        "topic": "icio_rate_pct",
                        "state": "not_confirmed",
                        "searched": "ordenanza fiscal art. 12-18",
                        "checked_at": "2026-07-01",
                    },
                ],
            },
        )
        resp = client.get("/construccion?concejo=33016")
        html = resp.get_data(as_text=True)
        assert 'data-test="not-researched-banner"' not in html
        assert 'data-test="value"' in html  # only pgo carries one
        assert html.count('data-test="value"') == 1
        assert 'data-test="state-not-confirmed"' in html
        assert "ordenanza fiscal art. 12-18" in html
        # The other topics stay honestly unresearched.
        assert 'data-test="state-not-researched"' in html


class TestCoverage:
    def test_empty_file_counts_nowhere(self, tmp_path, monkeypatch):
        _write_concejo(
            tmp_path,
            monkeypatch,
            "33049",
            {"ine_code": "33049", "display_name": "Piloña", "facts": []},
        )
        snapshot = concejo_legal.load_snapshot()
        cov = concejo_legal.coverage(snapshot)
        assert cov["searched_any"] == 0
        assert "33049" not in cov["beyond_scope"]

    def test_beyond_scope_is_named_separately(self, tmp_path, monkeypatch):
        # 33044 (Nava) is not in the seeded perimeter.
        _write_concejo(
            tmp_path,
            monkeypatch,
            "33044",
            {
                "ine_code": "33044",
                "display_name": "Nava",
                "facts": [
                    {
                        "topic": "pgo_status",
                        "state": "not_confirmed",
                        "searched": "PGO en el registro de planeamiento",
                        "checked_at": "2026-08-01",
                    }
                ],
            },
        )
        snapshot = concejo_legal.load_snapshot()
        cov = concejo_legal.coverage(snapshot)
        assert cov["beyond_scope"] == ["33044"]
        assert cov["searched_any"] == 0  # outside the perimeter numerator


class TestSnapshotRefusal:
    def test_missing_snapshot_refuses_the_page(self, client, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(
            concejo_legal, "SNAPSHOT_PATH", Path("/nonexistent/snap.json")
        )
        resp = client.get("/construccion")
        assert resp.status_code == 503
        assert 'data-test="coverage-line"' not in resp.get_data(as_text=True)


class TestFullDossier:
    """The full-view contract: a reading page with NO concejo context.

    The round-6 review forbade a local topic's value in prose next to a
    selected concejo; the full document keeps that by having no selector, no
    slots and no overlay -- ?concejo= is ignored outright."""

    def test_renders_the_uncompressed_document(self, client):
        resp = client.get("/construccion?view=full")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'data-test="full-body"' in html
        assert "32 ter" in html  # deep §7 marker the digest does not carry
        assert 'id="full-top"' in html

    def test_carries_no_concejo_machinery_even_when_asked(self, client):
        resp = client.get("/construccion?view=full&concejo=33016")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'data-test="slot-' not in html
        assert 'data-test="not-researched-banner"' not in html
        assert "<option" not in html
        assert 'data-test="full-note"' in html

    def test_digest_page_links_to_it(self, client):
        html = client.get("/construccion").get_data(as_text=True)
        assert 'data-test="full-link"' in html

    def test_missing_file_refuses_rather_than_renders_empty(self, client, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(
            concejo_legal, "FULL_DOSSIER_PATH", Path("/nonexistent/full.html")
        )
        resp = client.get("/construccion?view=full")
        assert resp.status_code == 503
        assert 'data-test="full-missing"' in resp.get_data(as_text=True)


class TestDeepLink:
    def test_chapter_carries_callout_and_states(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(concejo_legal, "CONCEJOS_DIR", tmp_path / "none")
        resp = client.get("/construccion?concejo=33049")
        html = resp.get_data(as_text=True)
        # The silence chapter holds a local topic: its callout names the
        # selected concejo, and its slot shows the not-researched state.
        assert 'data-test="chapter-callout-05-licencia"' in html
        assert "Piloña" in html
        assert 'data-test="slot-silence_period_months"' in html
        assert 'id="silencio"' in html
