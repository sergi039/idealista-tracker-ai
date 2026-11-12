"""On a phone `/properties` is a resource index, and nothing is drawn twice.

Owner report, 2026-11-12, from a 390px phone: a full screen of controls came
before the first listing (it began 902px down) and one listing took a screen
(514px). The phone layout now follows an established index layout
(`docs/design/README.md`, "Phone layout"): the secondary display controls and
the secondary status items sit behind ONE trigger each, and every hide stays in
view.

What is pinned here is what a headless test can know: the markup those CSS
rules need, and that the two files still meet. Positions, heights and what is
actually under a finger are measured in a browser on the built image -- a
template's text cannot prove a layout (`docs/rules/testing.md`).

Three promises:

*One control, one element.* The phone folds controls away by re-placing the
elements that exist, never by drawing a second copy for the narrow screen.

*A disclosure is transparent from the tablet width up.* Each wrapper is a
Bootstrap `collapse`, so it is closed until its trigger is pressed. The trigger
is not drawn at 768px and wider, so the stylesheet has to neutralise the
collapse there or the desktop would lose the controls with no way to open them.

*A hide is never behind the phone's disclosure.* The lines that say what the
page withheld -- failing criteria, rejected listings, folded publications,
hard exclusions -- are the reason the count can be believed.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_CSS = ROOT / "static" / "css" / "workspace.css"
TEMPLATE = ROOT / "templates" / "properties.html"

PHONE = "@media (max-width: 767.98px) {"
TABLET_UP = "@media (min-width: 768px) {"

# Ids of the controls a phone reaches through the display trigger.
DISPLAY_CONTROLS = (
    "mode-combined-btn",
    "mode-investment-btn",
    "mode-lifestyle-btn",
    "mode-taste-btn",
    "view-list-btn",
    "view-cards-btn",
    "view-map-btn",
)

# Ids of the status lines that disclose what the page withheld or why the
# count is what it is. None may carry the phone disclosure's class.
ALWAYS_VISIBLE_STATUS = (
    "filter-bar-narrowing-note",
    "unassigned-count-note",
    "criteria-hidden-coverage",
    "rejected-hidden-coverage",
    "duplicates-folded-coverage",
    "duplicates-fold-skipped",
    "taste-sort-dormant",
    "overdue-count-note",
    "recommendation-profile-status",
    "model-profile-dirty",
    "recommendation-hard-exclusions",
)

VOID = {
    "input",
    "img",
    "br",
    "hr",
    "meta",
    "link",
    "source",
    "wbr",
    "area",
    "base",
    "col",
}


class Tree(HTMLParser):
    """Start tags with the ids of the elements they are nested in."""

    def __init__(self, body):
        super().__init__()
        self.stack = []
        self.nodes = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ancestors = tuple(a.get("id") for _, a in self.stack if a.get("id"))
        self.nodes.append((tag, attrs, ancestors))
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def by_id(self, element_id):
        return [node for node in self.nodes if node[1].get("id") == element_id]

    def one(self, element_id):
        found = self.by_id(element_id)
        assert len(found) == 1, f"#{element_id} is rendered {len(found)} times"
        return found[0]


def _classes(attrs):
    return (attrs.get("class") or "").split()


def _media_block(css, header):
    """The body of one media query, matched by brace depth."""
    start = css.index(header)
    open_brace = css.index("{", start)
    depth = 0
    for index in range(open_brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _rule(block, selector):
    """The declarations of the rule whose selector list ends with `selector`."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", block)
    assert match, f"no rule for {selector!r}"
    return match.group(1)


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Phone index",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for index, verdict in enumerate((None, None, "rejected")):
            db.session.add(
                Property(
                    source_email_id=f"phone_index_{index}",
                    title=f"PhoneIndexListing{index}",
                    municipality="Cudillero",
                    search_profile_id=profile.id,
                    listing_status="active",
                    property_category="housing",
                    property_subtype="house",
                    price=150000 + index,
                    area=200,
                    location_lat=43.5,
                    location_lon=-6.2,
                    location_accuracy="approximate",
                    score_total=60.0,
                    owner_verdict=verdict,
                )
            )
        db.session.commit()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _page(client, view_type="list"):
    response = client.get(f"/properties?mode=combined&view_type={view_type}")
    assert response.status_code == 200
    return Tree(response.get_data(as_text=True))


class TestOneTriggerForTheDisplayControls:
    def test_the_trigger_opens_the_wrapper_and_names_what_is_selected(self, client):
        tree = _page(client)
        _, toggle, _ = tree.one("display-options-toggle")
        assert toggle["data-bs-toggle"] == "collapse"
        assert toggle["data-bs-target"] == "#display-options"
        assert toggle["aria-controls"] == "display-options"
        assert toggle["aria-expanded"] == "false"
        # Not drawn from the tablet width up, where the controls are in the row.
        assert "d-md-none" in _classes(toggle)
        # The closed trigger says which representation and assessment are on.
        assert "Table" in toggle["aria-label"] and "Overall" in toggle["aria-label"]
        _, wrapper, _ = tree.one("display-options")
        assert "collapse" in _classes(wrapper)
        assert "show" not in _classes(wrapper)

    def test_every_display_control_is_inside_the_wrapper_exactly_once(self, client):
        tree = _page(client)
        for control in DISPLAY_CONTROLS:
            _, _, ancestors = tree.one(control)
            assert "display-options" in ancestors, f"#{control} left the wrapper"
        exports = [
            ancestors
            for tag, attrs, ancestors in tree.nodes
            if tag == "a" and "/properties/export" in (attrs.get("href") or "")
        ]
        assert len(exports) == 1 and "display-options" in exports[0]

    def test_the_trigger_names_the_cards_when_the_cards_are_drawn(self, client):
        _, toggle, _ = _page(client, "cards").one("display-options-toggle")
        assert "Cards" in toggle["aria-label"] and "Table" not in toggle["aria-label"]

    def test_sort_and_the_tabs_stay_outside_the_wrapper(self, client):
        """They are the visible half of the bar: a tap must not be needed to
        see how the list is ordered or which collection it is."""
        tree = _page(client)
        for control in ("sort", "mode-all-btn", "mode-recommendation-btn"):
            _, _, ancestors = tree.one(control)
            assert "display-options" not in ancestors


class TestOneTriggerForTheSecondaryStatus:
    def test_the_trigger_is_rendered_once_and_targets_the_class(self, client):
        tree = _page(client)
        _, toggle, _ = tree.one("result-details-toggle")
        assert toggle["data-bs-toggle"] == "collapse"
        assert toggle["data-bs-target"] == ".workspace-status-more"
        assert toggle["aria-expanded"] == "false"
        assert "d-md-none" in _classes(toggle)

    def test_the_coverage_is_behind_it_and_comes_after_it(self, client):
        tree = _page(client)
        _, coverage, _ = tree.one("properties-coverage")
        assert {"collapse", "workspace-status-more"} <= set(_classes(coverage))
        ids = [attrs.get("id") for _, attrs, _ in tree.nodes]
        assert ids.index("result-details-toggle") < ids.index("properties-coverage"), (
            "a trigger drawn after what it discloses jumps away when pressed"
        )

    def test_a_rendered_hide_is_not_behind_it(self, client):
        """One fixture row is rejected, so the page withheld a listing and has
        to say so where a phone shows it without a tap."""
        tree = _page(client)
        _, hide, ancestors = tree.one("rejected-hidden-coverage")
        assert "workspace-status-more" not in _classes(hide)
        assert "collapse" not in _classes(hide)
        assert "properties-coverage" not in ancestors

    def test_no_hide_or_state_line_carries_the_disclosure_class(self):
        """Every branch of the template, not only the ones a fixture reaches."""
        source = TEMPLATE.read_text(encoding="utf-8")
        for element_id in ALWAYS_VISIBLE_STATUS:
            tags = re.findall(
                r"<[a-z]+\b[^>]*\bid=\"%s\"[^>]*>" % re.escape(element_id), source
            )
            assert tags, f"#{element_id} is no longer in the template"
            for tag in tags:
                assert "workspace-status-more" not in tag, (
                    f"#{element_id} would be hidden on a phone until a tap"
                )


class TestDecisionButtonsAreNamedOnAPhone:
    def test_a_table_row_carries_the_text_in_the_compact_class(self, client):
        tree = _page(client)
        labels = [
            attrs
            for tag, attrs, _ in tree.nodes
            if tag == "span" and "owner-decision-text" in _classes(attrs)
        ]
        # Two rows are on screen (the rejected one is withheld), three buttons each.
        assert len(labels) == 6
        for attrs in labels:
            assert "owner-decision-text-compact" in _classes(attrs)
            # `visually-hidden` is an !important utility: with it on the span
            # no phone rule could draw the word.
            assert "visually-hidden" not in _classes(attrs)

    def test_a_card_keeps_the_text_visible_at_every_width(self, client):
        tree = _page(client, "cards")
        labels = [
            attrs
            for tag, attrs, _ in tree.nodes
            if tag == "span" and "owner-decision-text" in _classes(attrs)
        ]
        assert labels
        for attrs in labels:
            assert "owner-decision-text-compact" not in _classes(attrs)


class TestTheStylesheetMeetsTheMarkup:
    def test_the_phone_rules_live_in_one_block(self):
        css = WORKSPACE_CSS.read_text(encoding="utf-8")
        assert css.count(PHONE) == 1, (
            "a second phone block makes the cascade depend on file order again"
        )

    def test_the_wrappers_are_transparent_from_the_tablet_width_up(self):
        """Without these two rules the desktop loses its display controls and
        its coverage line: both are `collapse`, and their triggers are `d-md-none`."""
        block = _media_block(WORKSPACE_CSS.read_text(encoding="utf-8"), TABLET_UP)
        assert "display: contents" in _rule(block, ".workspace-display-options")
        assert "display: block" in _rule(block, ".workspace-status-more.collapse")
        # The same block hides the decision words a phone draws.
        assert "clip: rect(0, 0, 0, 0)" in _rule(block, ".owner-decision-text-compact")
        # And wraps the tabs, now that the markup no longer forces it.
        assert "flex-wrap: wrap" in _rule(block, ".workspace-result-tabs")

    def test_the_phone_block_does_not_draw_the_wrappers_open(self):
        block = _media_block(WORKSPACE_CSS.read_text(encoding="utf-8"), PHONE)
        assert "display: contents" not in _rule(block, ".workspace-display-options")
        assert ".workspace-status-more.collapse" not in block

    def test_the_tabs_scroll_on_one_line_on_a_phone(self):
        block = _media_block(WORKSPACE_CSS.read_text(encoding="utf-8"), PHONE)
        tabs = _rule(block, ".workspace-result-tabs")
        assert "flex-wrap: nowrap" in tabs and "overflow-x: auto" in tabs
        # `.flex-wrap` is an !important utility: on the element it would
        # outrank the rule above and the tabs would stack again.
        source = TEMPLATE.read_text(encoding="utf-8")
        tag = re.search(r'<div class="([^"]*\bworkspace-result-tabs\b[^"]*)"', source)
        assert tag and "flex-wrap" not in tag.group(1).split()

    def test_search_opens_with_the_filters_and_the_decision_stays_out(self):
        """Search joins the other fields behind the one Filters trigger; the
        owner's queue select and Apply never do (#630)."""
        block = _media_block(WORKSPACE_CSS.read_text(encoding="utf-8"), PHONE)
        hidden = [
            selector.strip()
            for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", block)
            if re.search(r"display:\s*none", body)
        ]
        assert any(
            ":not(:has(.workspace-advanced-filters[open]))" in selector
            and selector.endswith(".properties-filter-search")
            for selector in hidden
        ), "search is not folded behind the Filters trigger"
        for selector in hidden:
            assert "properties-queue-switch" not in selector
            assert 'type="submit"' not in selector

    def test_a_row_is_one_stacked_summary(self):
        block = _media_block(WORKSPACE_CSS.read_text(encoding="utf-8"), PHONE)
        row = _rule(block, ".workspace-data-table tr")
        assert "display: grid" in row
        for area in ("summary", "price", "owner", "score", "facts"):
            assert area in row, f"the row grid lost its {area} area"
