"""The line under the tabs on /properties (owner report, 2026-11-14).

The owner read two lines of 10.5px grey text there -- "Filters: 98 of 1871
shown", "Criteria: 681 failing hidden", "Rejected: 25 hidden", four fragments
about one recommendation state, "38 preference(s)" -- and asked for what is not
needed to go. What stays is pinned here:

* everything a standing rule keeps back is ONE "N hidden" summary whose rows add
  up to N, and each row's link brings exactly those rows back (the way back is
  the rule, not the sentence);
* nothing in the line is small text, and the retired sentences are gone;
* the recommendation state is one element, and says whether previous
  recommendations are still shown;
* on the Recommended tab the coverage counts are about the rows drawn.

Every assertion reads a rendered page: the template's static text proves nothing
about what a reader sees.
"""

import re
from html import unescape
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.taste_recommendation import RecommendationContext
from tests import setup_test_environment
from utils import i18n

ROOT = Path(__file__).resolve().parents[1]
CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}
_SEQ = iter(range(1, 100_000))


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


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def world(app):
    """One subscription holding a passing row, a measured fail, a rejected row
    and a row nobody gave a subscription."""
    profile = SearchProfile(name="Costa", is_active=True, criteria=CRITERIA)
    db.session.add(profile)
    db.session.commit()

    def mk(title, profile_id=profile.id, **extra):
        values = dict(
            source_email_id=f"line:{next(_SEQ)}",
            title=title,
            price=200000,
            area=220,
            area_type="built",
            plot_area=1200,
            search_profile_id=profile_id,
            listing_status="active",
            listing_status_source="check",
        )
        values.update(extra)
        row = Property(**values)
        db.session.add(row)
        db.session.commit()
        return row

    return {
        "profile": profile,
        "pass": mk("Pass house"),
        "pass2": mk("Pass cottage"),
        "fail": mk("Fail tiny", area=80),
        "rejected": mk("Rejected villa", owner_verdict="rejected"),
        "orphan": mk("Orphan plot", profile_id=None),
    }


def _body(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _status(body):
    """The status block, from its opening tag to the entry help that follows."""
    start = body.index('<div class="workspace-result-status">')
    return body[start : body.index('id="recommendation-entry-help"', start)]


def _href(markup, element_id):
    found = re.search(r'id="%s"\s+href="([^"]+)"' % re.escape(element_id), markup)
    assert found, f"#{element_id} is not in the status line"
    return unescape(found.group(1))


def _text(markup):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _collapsing_ancestors(markup, element_id):
    """Tags enclosing `element_id` that carry `collapse` or the phone class."""
    from html.parser import HTMLParser

    void = {"input", "img", "br", "hr", "meta", "link"}
    found = []

    class Walk(HTMLParser):
        stack = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") == element_id:
                found.extend(
                    t
                    for t, c in self.stack
                    if {"collapse", "workspace-status-more"} & set(c.split())
                )
            if tag not in void:
                self.stack.append((tag, attrs.get("class") or ""))

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    return

    Walk().feed(markup)
    return found


def _found(body):
    return int(re.search(r"<strong>(\d+) properties found</strong>", body).group(1))


class TestWhatTheLineKeepsBackIsOneControl:
    def test_the_summary_adds_up_its_reasons(self, client, world):
        status = _status(_body(client, "/properties?profile_id=all"))
        summary = re.search(r"<summary>(\d+) hidden</summary>", status)
        assert summary, "kept-back rows must be one summary"
        listed = re.search(r'id="hidden-rows".*?</details>', status, re.S).group(0)
        counts = [
            int(n) for n in re.findall(r'<span id="[a-z-]+"[^>]*>\s*(\d+) ', listed)
        ]
        assert sum(counts) == int(summary.group(1)) == 3, (
            "criteria + rejected + no-subscription, each counted once"
        )

    def test_the_summary_also_adds_folded_publications_and_hard_exclusions(
        self, client, world
    ):
        """Five reasons, one number: the two the fixture cannot produce on its
        own are supplied the way the route supplies them."""

        def selection(query, *_args, **_kwargs):
            return (
                query,
                RecommendationContext(profile=_state("current"), readings={}),
                4,
            )

        with (
            patch("routes.main_routes._recommendation_selection", selection),
            patch("routes.main_routes.duplicate_fold.folded_count", return_value=7),
        ):
            status = _status(
                _body(
                    client,
                    "/properties?profile_id=all&mode=recommendation&view_type=list",
                )
            )
        total = int(re.search(r"<summary>(\d+) hidden</summary>", status).group(1))
        listed = re.search(r'id="hidden-rows".*?</details>', status, re.S).group(0)
        rows = dict(re.findall(r'<span id="([a-z-]+)"[^>]*>\s*(\d+) ', listed))
        assert {"duplicates-folded-coverage", "recommendation-hard-exclusions"} <= set(
            rows
        )
        assert rows["duplicates-folded-coverage"] == "7"
        assert rows["recommendation-hard-exclusions"] == "4"
        assert total == sum(int(n) for n in rows.values()) == 7 + 4 + 1 + 1 + 1

    def test_nothing_kept_back_means_no_summary(self, client, app):
        db.session.add(SearchProfile(name="Empty", is_active=True))
        db.session.commit()
        status = _status(_body(client, "/properties"))
        assert "hidden-rows" not in status

    @pytest.mark.parametrize(
        "link, title",
        [
            ("criteria-hidden-show-link", "Fail tiny"),
            ("rejected-hidden-show-link", "Rejected villa"),
            ("unassigned-count-link", "Orphan plot"),
        ],
    )
    def test_each_row_has_the_link_that_brings_its_rows_back(
        self, client, world, link, title
    ):
        before = _body(client, "/properties?profile_id=all")
        assert title not in before, "the fixture row must start out hidden"
        href = _href(_status(before), link)
        assert title in _body(client, href), f"#{link} says it shows them and does not"

    def test_the_summary_is_never_behind_the_phone_trigger(self, client, world):
        status = _status(_body(client, "/properties?profile_id=all"))
        details = re.search(r'<details id="hidden-rows"[^>]*>', status).group(0)
        assert "collapse" not in details and "workspace-status-more" not in details
        assert status.index('id="hidden-rows"') < status.index("workspace-status-more")
        assert not _collapsing_ancestors(status, "hidden-rows"), (
            "a wrapper that collapses on a phone would hide the way back"
        )

    def test_a_selection_widened_to_the_orphans_hides_nothing_there(
        self, client, world
    ):
        body = _body(client, "/properties?profile_id=unassigned")
        line = _status(body).split('id="properties-coverage"')[0]
        assert 'id="unassigned-count-note"' not in line, (
            "nothing is kept back, so it is not in the kept-back list"
        )
        panel = body[body.index('id="properties-coverage"') :]
        assert "includes 1 listing with no subscription" in _text(panel)


class TestTheRetiredSentencesAreGone:
    def test_the_status_line_is_body_text(self, client, world):
        status = _status(_body(client, "/properties?profile_id=all&search=pass"))
        assert not re.search(r'class="[^"]*\bsmall\b', status), (
            "`.small` inside the line compounds to 10.5px (owner report)"
        )

    def test_the_stylesheet_does_not_set_the_line_below_body_size(self):
        css = (ROOT / "static" / "css" / "workspace.css").read_text(encoding="utf-8")
        rule = re.search(r"\n\.workspace-result-status \{([^}]*)\}", css).group(1)
        size = float(re.search(r"font-size:\s*([\d.]+)rem", rule).group(1))
        assert size >= 0.875

    def test_no_rule_shrinks_the_line_or_what_it_discloses(self):
        css = (ROOT / "static" / "css" / "workspace.css").read_text(encoding="utf-8")
        checked = 0
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if not re.search(
                r"workspace-result-status|workspace-hidden|properties-coverage",
                selector,
            ):
                continue
            for size in re.findall(r"font-size:\s*([\d.]+)rem", body):
                checked += 1
                assert float(size) >= 0.8125, f"{selector.strip()}: {size}rem"
        assert checked >= 4, "the rules this guards were not found"

    def test_a_narrowing_filter_says_out_of_and_not_a_second_filter_line(
        self, client, world
    ):
        status = _status(_body(client, "/properties?profile_id=all&search=Pass+house"))
        text = _text(status)
        assert "out of 2" in text and "Filters:" not in text
        assert _href(status, "clear-filters-link")

    def test_old_wording_is_in_neither_language(self):
        for language in ("en", "es"):
            catalog = i18n.TRANSLATIONS[language]
            for key in (
                "recommendation_profile_unapplied_one",
                "recommendation_profile_unapplied_other",
                "recommendation_profile_current",
                "hidden_rows_summary_one",
                "hidden_rows_summary_other",
                "hidden_rows_recommendation_one",
                "hidden_rows_recommendation_other",
            ):
                assert key in catalog, (language, key)
            ours = [
                value
                for key, value in catalog.items()
                if key.startswith(("hidden_rows_", "recommendation_profile_"))
            ]
            assert ours and not any("(s)" in value for value in ours), language
        for gone in (
            "workspace_previous",
            "recommendation_hard_exclusions",
            "filter_bar_narrowing",
        ):
            assert gone not in i18n.TRANSLATIONS["en"]
            assert gone not in i18n.TRANSLATIONS["es"]

    def test_both_languages_carry_the_same_keys(self):
        en = {
            k
            for k in i18n.TRANSLATIONS["en"]
            if k.startswith(
                (
                    "hidden_rows_",
                    "recommendation_profile_",
                    "recommendation_liked",
                    "recommendation_preferences",
                    "filter_bar_out_of",
                )
            )
        }
        es = {
            k
            for k in i18n.TRANSLATIONS["es"]
            if k.startswith(
                (
                    "hidden_rows_",
                    "recommendation_profile_",
                    "recommendation_liked",
                    "recommendation_preferences",
                    "filter_bar_out_of",
                )
            )
        }
        assert en == es and len(en) >= 20


def _state(kind, version=3, unapplied=0):
    return {
        "state": kind,
        "version": version,
        "usable_signal_count": 4,
        "positive_reference_count": 2,
        "mapped_clause_count": 5,
        "unapplied_clauses": [
            {"text": f"Clause {i}", "source_property_id": None, "reason": None}
            for i in range(unapplied)
        ],
    }


def _with_state(client, url, state):
    with patch(
        "routes.main_routes.taste_service.recommendation_profile_state",
        return_value=state,
    ):
        return _body(client, url)


class TestTheRecommendationStateIsOneSentenceAndOneButton:
    @pytest.mark.parametrize(
        "kind, version, refresh, previous",
        [
            ("dirty", 3, True, "true"),
            ("dirty", None, True, "false"),
            ("pending", 3, False, "true"),
            ("failed", 3, True, "true"),
            ("failed", None, True, "false"),
        ],
    )
    def test_one_state_says_whether_previous_ones_are_shown(
        self, client, world, kind, version, refresh, previous
    ):
        body = _with_state(
            client,
            "/properties?profile_id=all&mode=recommendation&view_type=list",
            _state(kind, version),
        )
        status = _status(body)
        assert status.count('id="recommendation-profile-status"') == 1
        assert f'data-recommendation-previous="{previous}"' in status
        assert status.count('id="recommendation-refresh-button"') == int(refresh)
        assert "remain available" not in status

    @pytest.mark.parametrize("kind", ["current", "dirty"])
    def test_a_stale_taste_score_joins_the_same_sentence(self, client, world, kind):
        """`model-profile-dirty` was a second message beside the state. It is
        part of it now -- even where a current profile is otherwise silent."""
        body = _with_state(
            client,
            "/properties?profile_id=all&view_type=list",
            dict(_state(kind), model_profile_dirty=True),
        )
        status = _status(body)
        line = status[: status.index('id="properties-coverage"')]
        state = re.search(
            r'<span id="recommendation-profile-status".*?</span>\s*</span>|'
            r'<span id="recommendation-profile-status".*?</span>',
            line,
            re.S,
        )
        assert state and 'id="model-profile-dirty"' in state.group(0), (
            "the stale-score sentence must sit inside the one state element"
        )
        assert line.count('id="model-profile-dirty"') == 1
        assert "behind your latest reviews" in _text(state.group(0))

    def test_a_current_profile_is_silent_on_the_all_tab_and_read_in_the_panel(
        self, client, world
    ):
        body = _with_state(
            client, "/properties?profile_id=all&view_type=list", _state("current")
        )
        status = _status(body)
        line = status[: status.index('id="properties-coverage"')]
        assert 'id="recommendation-profile-status"' not in line
        panel = status[status.index('id="properties-coverage"') :]
        assert "Recommendations are based on 2 listings you liked and 5" in _text(panel)

    def test_a_current_profile_is_shown_on_the_recommended_tab(self, client, world):
        body = _with_state(
            client,
            "/properties?profile_id=all&mode=recommendation&view_type=list",
            _state("current"),
        )
        status = _status(body)
        line = status[: status.index('id="properties-coverage"')]
        assert 'id="recommendation-profile-status"' in line

    def test_unapplied_preferences_are_counted_in_words(self, client, world):
        body = _with_state(
            client,
            "/properties?profile_id=all&view_type=list",
            _state("current", unapplied=3),
        )
        assert "3 of your preferences are not applied yet" in _text(_status(body))
        one = _with_state(
            client,
            "/properties?profile_id=all&view_type=list",
            _state("current", unapplied=1),
        )
        assert "1 of your preferences is not applied yet" in _text(_status(one))


class TestRecommendedTabCoverageIsAboutTheRowsDrawn:
    def test_the_coverage_denominator_is_the_collection_not_the_selection(
        self, client, world
    ):
        """The Recommended tab said "278 of 98 verified": the four counts were
        taken before the collection was cut to the rows it draws."""
        keep = {world["pass"].id, world["pass2"].id}
        # Three more live, verified rows the collection does not select: they
        # are in the selection (so a count taken before the cut includes them)
        # and not on the page.
        for number in range(3):
            db.session.add(
                Property(
                    source_email_id=f"line-extra:{number}",
                    title=f"Not recommended {number}",
                    price=200000,
                    area=220,
                    area_type="built",
                    plot_area=1200,
                    search_profile_id=world["profile"].id,
                    listing_status="active",
                    listing_status_source="check",
                )
            )
        db.session.commit()
        reading = {
            "state": "candidate",
            "nearest_positive_reference": None,
            "matches": [],
            "conflicts": [],
            "needs_verification": [],
            "recommended": True,
            "rank_value": 1.0,
        }

        def selection(query, *_args, **_kwargs):
            return (
                query.filter(Property.id.in_(keep)),
                RecommendationContext(
                    profile=_state("current"), readings={i: reading for i in keep}
                ),
                0,
            )

        with patch("routes.main_routes._recommendation_selection", selection):
            body = _body(
                client,
                "/properties?profile_id=all&mode=recommendation&view_type=list",
            )
        assert _found(body) == 2
        panel = _text(body[body.index('id="properties-coverage"') :])
        for label in ("Listing status", "Industrial neighbours", "Score"):
            counted = re.search(label + r": (\d+) of (\d+)", panel)
            assert counted, label
            assert int(counted.group(2)) == 2, (
                f"{label}: {counted.group(0)} is not about the 2 rows drawn"
            )
            assert int(counted.group(1)) <= 2
        # Every row is verified, so a count taken over the whole selection
        # would read 5 where the two rows drawn give 2.
        assert "Listing status: 2 of 2" in panel

    def test_the_hard_exclusions_row_carries_its_way_back(self, client, world):
        def selection(query, *_args, **_kwargs):
            return (
                query,
                RecommendationContext(profile=_state("current"), readings={}),
                4,
            )

        with patch("routes.main_routes._recommendation_selection", selection):
            body = _body(
                client,
                "/properties?profile_id=all&mode=recommendation&view_type=list",
            )
        status = _status(body)
        assert "4 ruled out by your answers or requirements" in _text(status)
        back = parse_qs_href(_href(status, "recommendation-hard-exclusions-link"))
        assert back["mode"] == ["combined"] and "recommendation_scope" not in back
        assert "hidden-rows" in status
        # And it is a page that opens, on the catalog, not on the collection
        # that left the rows out.
        landed = _body(client, _href(status, "recommendation-hard-exclusions-link"))
        assert re.search(r'id="mode-all-btn"[^>]*aria-current="true"', landed, re.S)


def parse_qs_href(href):
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(href).query)


class TestSpanish:
    def test_the_line_is_translated(self, client, world):
        with client.session_transaction() as session:
            session["language"] = "es"
        status = _status(_body(client, "/properties?profile_id=all"))
        text = _text(status)
        assert "3 ocultos" in text
        assert "fuera de sus criterios" in text and "rechazado" in text
        assert "hidden" not in text and "rejected" not in text
