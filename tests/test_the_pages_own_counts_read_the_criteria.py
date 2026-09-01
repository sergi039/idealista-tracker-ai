"""Every count on the listing page is the size of the page its own link opens.

Closing-audit findings 2, 3 and 5 (2026-09-01), all measured on production,
and the independent reviewer's two second-order cases. Five surfaces learned
the subscription criteria in #508–#520 and the page everything was fixed FOR
kept three of its own numbers on the old reading:

* the subscription chip said "Galicia · costa 543" while its own link opened
  478 — the 65 criteria-hidden rows exactly, the overstatement #518 names as
  the defect (finding 2);
* every counted dropdown option overstated by the same rows — Cedeira "(25)"
  over a page finding 18 (#518's own worked example, fixed on
  /municipalities and alive here), "Not decided yet (540)" over 475 (finding
  3);
* `criteria=FAIL` APPLIED the fail mode — 65 rows — while the select, handed
  the raw string, rendered nothing selected and every chip link carried the
  raw spelling (finding 5).

**The rule is #518's, stated as one relation and tested as one: a count and
the page its own link opens are one statement.** Not "the default reading"
(the first cut of this change, rejected in review: under `criteria=all` it
made the chip UNDERSTATE by the same 65, a second understating control on the
page the whole feature is about), and not "the bare subscription": a chip
that counted the subscription alone over a page with `search=` typed promised
more than clicking it showed, because the href keeps the page's filters
(`base_args`). So every count follows what its own link keeps — the criteria
mode (spelled by absence for the default, `criteria=all`/`fail` otherwise)
and the other filters — through the one function the page narrows with
(`_apply_filter_bar`), with the option's own dimension left open.

One exception, and it is #470's owner decision, not this file's: the chip
never counts the delisted rows a `hide_removed=off` page shows; the switch
explains that gap and `tests/test_chip_badge_counts_live_listings.py` pins
it. Every page swept here keeps Hide removed on.

Scope, not a defect: the acceptance condition is the audit's — follow the
link the page actually rendered and count what comes back.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}

TOTAL_RE = re.compile(r"<strong>\s*(\d+)\s+properties found\s*</strong>")

# A subscription chip: its href, its name, its badge count — in the order the
# template emits them, so the count read is the one beside that name.
CHIP_RE = re.compile(
    r'<a\b[^>]*href="([^"]+)"[^>]*>\s*'
    r'<span class="properties-subscription-name">([^<]+)</span>\s*'
    r'<span class="badge[^"]*"[^>]*>(\d+)</span>',
    re.S,
)

COUNTED_SELECTS = ("municipality", "source", "advertiser", "verdict", "action")


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


_SEQ = iter(range(1, 100_000))


def _mk(profile_id, municipality="Cedeira", **overrides):
    n = next(_SEQ)
    values = dict(
        source_email_id=f"own_counts:{n}",
        title=f"Listing {n}",
        municipality=municipality,
        listing_status="active",
        property_category="housing",
        search_profile_id=profile_id,
        price=120000,
        area=200.0,
        area_type="built",
        plot_area=1000.0,
        url=f"https://www.idealista.com/inmueble/{7000 + n}/",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _fails(profile_id, **overrides):
    """A row measurably below both bounds: 100 m² of house on a 300 m² plot."""
    values = dict(area=100.0, plot_area=300.0)
    values.update(overrides)
    return _mk(profile_id, **values)


@pytest.fixture
def world(app):
    """Galicia carries criteria; Asturias none. Every exemption present.

    Galicia, municipality Cedeira unless said otherwise:
      3 passing rows (one fotocasa, for the source counts), 1 unknown
      (no plot stated), 3 unjudged fails (one of them in Camariñas, one
      fotocasa), and one fail under each exemption — favorited, reviewed
      (rejected), carrying an open action. Ares holds ONLY unjudged fails.
    Asturias: one fail-shaped row — no criteria, so nothing may touch it.

    Deliberately NO delisted row: Hide removed is #470's business (see the
    module docstring), and one here would make these assertions measure two
    rules at once.
    """
    galicia = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    asturias = SearchProfile(name="Asturias", is_active=True)
    db.session.add_all([galicia, asturias])
    db.session.commit()

    rows = {
        # The campaign token makes this row `owner`, so the advertiser
        # dropdown has two states and is actually rendered.
        "pass_1": _mk(
            galicia.id,
            url="https://www.idealista.com/inmueble/91523456/"
            "?utm_campaign=express_newAd_sale_particular",
        ),
        "pass_2": _mk(galicia.id),
        "pass_fotocasa": _mk(
            galicia.id,
            url=f"https://www.fotocasa.es/es/comprar/casa/cedeira/{next(_SEQ)}/d",
        ),
        "unknown": _mk(galicia.id, plot_area=None),
        "fail_hidden_1": _fails(galicia.id),
        "fail_hidden_2": _fails(galicia.id, municipality="Camariñas"),
        "fail_hidden_fotocasa": _fails(
            galicia.id,
            url=f"https://www.fotocasa.es/es/comprar/casa/cedeira/{next(_SEQ)}/d",
        ),
        "fail_ares_1": _fails(galicia.id, municipality="Ares"),
        "fail_ares_2": _fails(galicia.id, municipality="Ares"),
        "fail_favorited": _fails(galicia.id, is_favorite=True),
        "fail_reviewed": _fails(galicia.id, owner_verdict="rejected"),
        "fail_actioned": _fails(galicia.id, next_action="Call the architect"),
        "asturias_fail_shape": _fails(asturias.id, municipality="Gijon"),
    }
    return {
        "galicia": galicia.id,
        "asturias": asturias.id,
        "ids": {name: row.id for name, row in rows.items()},
    }


def _page(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.get_data(as_text=True)


def _total(client, url):
    body = _page(client, url)
    match = TOTAL_RE.search(body)
    assert match, f"no result count rendered for {url}"
    return int(match.group(1))


def _chips(body):
    """{name: (count, href)} for every subscription chip on the page."""
    return {
        name.strip(): (int(count), html.unescape(href))
        for href, name, count in CHIP_RE.findall(body)
    }


def _options(body, select_name):
    """{value: count-or-None} for one filter dropdown, {} when not drawn."""
    select = re.search(rf'<select[^>]*name="{select_name}".*?</select>', body, re.S)
    if not select:
        return {}
    out = {}
    for value, label in re.findall(
        r'<option value="([^"]*)"[^>]*>(.*?)</option>', select.group(0), re.S
    ):
        count = re.search(r"\((\d+)\)", label)
        out[html.unescape(value)] = int(count.group(1)) if count else None
    return out


def _with_param(url, name, value):
    """`url` with `name` set to `value` — what submitting the filter form with
    that option picked sends, since the form carries every other field."""
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name
    ]
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _listed_ids(client, url):
    body = _page(client, _with_param(url, "per_page", "100"))
    return {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}


PAGES = [
    "/properties",
    "/properties?criteria=all",
    "/properties?criteria=fail",
    "/properties?criteria=unknown",
    "/properties?profile_id={G}",
    "/properties?profile_id={G}&criteria=all",
    "/properties?profile_id={G}&criteria=pass",
    # The reviewer's second-order cases: something typed in the filter bar.
    "/properties?profile_id={G}&search=Cedeira",
    "/properties?search=Ares",
    "/properties?profile_id={G}&municipality=Cedeira",
    "/properties?profile_id={G}&municipality=Ares&criteria=all",
    "/properties?profile_id={G}&source=fotocasa&criteria=all",
    "/properties?profile_id={G}&verdict=undecided",
    "/properties?profile_id={G}&favorites=on&criteria=all",
    "/properties?profile_id={G}&action=pending&criteria=fail",
]


class TestEveryCountIsItsLinksOwnPromise:
    """The rule, as one relation, over pages in every mode with and without
    something in the filter bar: for every chip, and for every counted
    option, count == |the id set of the page its own link opens|."""

    @pytest.mark.parametrize("page_query", PAGES)
    def test_chips_and_counted_options_open_exactly_their_count(
        self, client, world, page_query
    ):
        url = page_query.format(G=world["galicia"])
        body = _page(client, url)
        chips = _chips(body)
        assert chips, "no chips rendered"
        for name, (count, href) in chips.items():
            assert len(_listed_ids(client, href)) == count, (name, href)
        counted = 0
        for select_name in COUNTED_SELECTS:
            for value, count in _options(body, select_name).items():
                if not value or count is None:
                    continue
                counted += 1
                landing = _with_param(url, select_name, value)
                assert len(_listed_ids(client, landing)) == count, (
                    select_name,
                    value,
                    landing,
                )
        assert counted or _total(client, url) == 0, (
            "no counted option rendered on a page that has rows"
        )

    def test_the_sweep_is_not_trivially_true(self, client, world):
        """The relation must be able to fail: the pages above carry counts
        that differ from each other and from zero, so a count that read a
        constant would be caught. (A sweep over pages that all render the
        same number proves nothing about following the mode.)"""
        seen = set()
        for page_query in PAGES:
            url = page_query.format(G=world["galicia"])
            seen.add(_chips(_page(client, url))["Galicia · costa"][0])
        assert len(seen) >= 4, seen


class TestTheChipFollowsWhatItsLinkKeeps:
    """The defect by value, and the mode followed rather than defended."""

    def test_the_defect_by_value(self, client, world):
        """12 live Galicia rows, 5 of them unjudged fails: the chip says 7.

        The three exempt fails — favorited, reviewed, actioned — stay
        counted: a row the owner judged is never hidden, so the page its
        link opens still shows it.
        """
        chips = _chips(_page(client, "/properties"))
        assert chips["Galicia · costa"][0] == 7
        assert chips["Asturias"][0] == 1, (
            "a subscription without criteria keeps its raw live count"
        )

    def test_the_default_is_spelled_by_absence(self, client, world):
        """The URL vocabulary says default by silence (criteria_mode()'s one
        translation), so the bare page's chip carries no `criteria=` — and
        opens the default reading, which is what it counted."""
        count, href = _chips(_page(client, "/properties"))["Galicia · costa"]
        assert "criteria=" not in href
        assert count == 7 == _total(client, href)

    def test_under_criteria_all_the_chip_is_exact_not_defended(self, client, world):
        """The rejected first cut kept the chip at 7 here, with the select to
        explain the gap. The rule has no gap: 12 == 12, and the href states
        the mode because `criteria` filters when absent."""
        count, href = _chips(_page(client, "/properties?criteria=all"))[
            "Galicia · costa"
        ]
        assert count == 12
        assert "criteria=all" in href
        assert _total(client, href) == 12

    def test_under_criteria_fail_the_chip_counts_the_fails(self, client, world):
        """Every measured fail, judged or not — `fail` selects a verdict, and
        the exemptions belong to the default hide alone."""
        count, href = _chips(_page(client, "/properties?criteria=fail"))[
            "Galicia · costa"
        ]
        assert count == 8
        assert "criteria=fail" in href
        assert _total(client, href) == 8

    def test_the_chip_follows_the_filter_bar_too(self, client, world):
        """The reviewer's second-order case: with `search=` typed, the chip
        counts what clicking it shows — the href keeps the search."""
        base = f"/properties?profile_id={world['galicia']}"
        count, href = _chips(_page(client, f"{base}&search=Ares"))["Galicia · costa"]
        assert count == 0, "both Ares rows are unjudged fails, hidden by default"
        assert "search=Ares" in href
        count, href = _chips(_page(client, f"{base}&search=Ares&criteria=all"))[
            "Galicia · costa"
        ]
        assert count == 2 == _total(client, href)
        count, _ = _chips(_page(client, f"{base}&source=fotocasa"))["Galicia · costa"]
        assert count == 1, "pass_fotocasa; the fotocasa fail is hidden by default"


class TestTheCountedOptionsFollowTheSameRule:
    def test_cedeira_is_fixed_on_the_page_it_was_measured_on(self, client, world):
        """#518's own worked example: the option count IS the page count."""
        base = f"/properties?profile_id={world['galicia']}"
        municipalities = _options(_page(client, base), "municipality")
        picked = f"{base}&municipality=Cedeira"
        assert municipalities["Cedeira"] == 7 == len(_listed_ids(client, picked))

    def test_an_option_the_current_view_empties_is_not_offered(self, client, world):
        """Ares holds only unjudged fails: under the default reading picking
        it can only return an empty page, so it is not offered — and under
        `criteria=all` it is, with the number that page shows."""
        base = f"/properties?profile_id={world['galicia']}"
        assert "Ares" not in _options(_page(client, base), "municipality")
        everything = _options(_page(client, f"{base}&criteria=all"), "municipality")
        assert everything["Ares"] == 2

    def test_undecided_follows_the_mode(self, client, world):
        base = f"/properties?profile_id={world['galicia']}"
        assert _options(_page(client, base), "verdict")["undecided"] == 6
        assert (
            _options(_page(client, f"{base}&criteria=all"), "verdict")["undecided"]
            == 11
        )
        assert _options(_page(client, base), "verdict")["rejected"] == 1

    def test_the_action_counts_follow_the_mode_like_the_others(self, client, world):
        """The first cut left the action dropdown alone because under the
        default reading its narrowing is a no-op (an open action exempts the
        row). It is not a no-op under `criteria=pass`: the actioned row is a
        fail, and the page its option opens is empty."""
        base = f"/properties?profile_id={world['galicia']}"
        assert _options(_page(client, base), "action").get("pending") == 1
        assert _total(client, f"{base}&action=pending") == 1
        under_pass = _options(_page(client, f"{base}&criteria=pass"), "action")
        assert under_pass.get("pending") in (None, 0)
        assert _total(client, f"{base}&criteria=pass&action=pending") == 0

    def test_options_follow_the_filter_bar_too(self, client, world):
        """With `source=fotocasa` picked, the municipality counts describe the
        fotocasa rows — the page picking a municipality then opens."""
        base = f"/properties?profile_id={world['galicia']}&source=fotocasa&criteria=all"
        municipalities = _options(_page(client, base), "municipality")
        assert municipalities == {"": None, "Cedeira": 2}, municipalities

    def test_an_options_own_dimension_is_left_open(self, client, world):
        """Picking a municipality must not collapse the municipality dropdown
        to that one entry: the count for every OTHER municipality is the page
        that re-picking it opens, so the dimension is skipped when its own
        options are counted. The relation test alone cannot see this — a
        collapsed dropdown is exact about the one option it still offers."""
        base = f"/properties?profile_id={world['galicia']}&criteria=all"
        picked = _options(_page(client, f"{base}&municipality=Cedeira"), "municipality")
        assert picked == {"": None, "Ares": 2, "Camariñas": 1, "Cedeira": 9}, picked
        assert _total(client, f"{base}&municipality=Ares") == 2


class TestTheHiddenSubscriptionNoteCountsWhatShowingWouldShow:
    """Finding 2's sibling: the withheld-listings number is what revealing
    the subscription would add to THIS page — its mode and its filters."""

    def _note(self, app, client, url):
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            assert client.get(url).status_code == 200
        finally:
            template_rendered.disconnect(record, app)
        return seen[-1]["hidden_subscription_note"]

    def test_the_note_follows_the_page(self, app, client, world):
        profile = db.session.get(SearchProfile, world["galicia"])
        profile.is_hidden = True
        db.session.commit()
        assert self._note(app, client, "/properties") == {"profiles": 1, "listings": 7}
        assert self._note(app, client, "/properties?criteria=all") == {
            "profiles": 1,
            "listings": 12,
        }
        assert self._note(app, client, "/properties?search=Ares") == {
            "profiles": 1,
            "listings": 0,
        }, "revealing the subscription would add nothing to a page searching Ares"


class TestTheDisclosureIsAbsentAtZero:
    """A line saying nothing was hidden is noise on a request where nothing
    was (the independent reviewer's note on #529's API twin, which still
    renders "0 listing(s) ... hidden" from routes/api_routes.py — outside
    this PR's files). The page's rule, pinned: absent at zero, present with
    the count above zero."""

    def test_absent_at_zero_present_above(self, client, world):
        asturias = _page(client, f"/properties?profile_id={world['asturias']}")
        assert 'id="criteria-hidden-coverage"' not in asturias
        assert "failing hidden" not in asturias
        galicia = _page(client, f"/properties?profile_id={world['galicia']}")
        assert re.search(r"Criteria:\s*5\s+failing hidden", galicia)
        favorites = _page(
            client, f"/properties?profile_id={world['galicia']}&favorites=on"
        )
        assert 'id="criteria-hidden-coverage"' not in favorites, (
            "the only favorited row is exempt, so nothing is hidden and the "
            "line stays off"
        )


class TestTheCriteriaParameterIsCanonicalised:
    """Finding 5: the applied mode and the rendered control are one value."""

    def test_criteria_FAIL_applies_and_renders_the_fail_mode(self, client, world):
        raw = _listed_ids(client, "/properties?criteria=FAIL")
        canonical = _listed_ids(client, "/properties?criteria=fail")
        assert raw == canonical and raw, "one spelling, one set"
        body = _page(client, "/properties?criteria=FAIL")
        assert re.search(r'<option value="fail"\s+selected', body), (
            "the control renders the mode the query applied"
        )
        # And the links the page rebuilds carry the canonical spelling.
        chips = _chips(body)
        assert "criteria=fail" in chips["Galicia · costa"][1]
        assert "criteria=FAIL" not in body
        # And the disclosure agrees with the applied mode: under `fail`
        # nothing is hidden by default, so the line is absent.
        assert 'id="criteria-hidden-coverage"' not in body

    def test_an_unrecognised_spelling_renders_the_default_it_applies(
        self, client, world
    ):
        assert _listed_ids(client, "/properties?criteria=banana") == _listed_ids(
            client, "/properties?profile_id=all"
        )
        body = _page(client, "/properties?criteria=banana")
        assert re.search(r'<option value=""\s+selected', body), (
            "banana falls back to the default reading, and the control says "
            "the default rather than nothing"
        )
        # The disclosure agrees with the mode that ran: the default reading
        # hid Galicia's five unjudged fails, and the line says so.
        assert re.search(r"Criteria:\s*5\s+failing hidden", body), (
            "the default hide ran for banana, so the disclosure must name it"
        )
