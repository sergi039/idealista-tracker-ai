"""Two counts that carried a link to a page stating something else (#513).

The subscription criteria (`services/subscription_criteria.py`) are a fifth
scope axis, and two surfaces knew nothing about them:

* **`/municipalities`** applied no criteria filter at all, so every count and
  every median was taken over rows the page its own link opens does not show.
  Measured on production 2026-08-31: 16 of 142 rows printed a Listings count
  larger than their link opened, 59 listings in all, Cedeira printing 35
  against a page reading "28 properties found · Criteria: 7 failing hidden".
  The medians are what that page is *for*, and the excluded rows sit at the
  cheap end — Camariñas ran 18 rows at €127,500 / €865 per m² against 11 rows
  at €200,000 / €584 under the default reading, which is the difference
  between the cheapest municipality on the page and an ordinary one. So there
  the counts move, and the page discloses what the reading left out.
* **`/profiles`** counts every stored listing a subscription holds — a fact
  about the subscription, not a basis for comparing anything — so there the
  number stays and the LINK states the scope: `criteria=all`, the remedy
  `utils/listing_filters.CLEARED_NOT_ABSENT` already records for the one
  filter whose absence still filters. Measured the same day: 443 in the
  column, "384 properties found" behind it.

What is pinned here is the acceptance condition rather than the shape of a
URL: *follow the link the page actually rendered and count what comes back*,
the way `tests/test_municipality_drilldown_scope.py` does for #417's four
axes. A test asserting "the href contains criteria=all" would stay green
through a `/properties` that read the parameter differently.
"""

import html
import json
import re
from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}

TOTAL_RE = re.compile(r"<strong>\s*(\d+)\s+properties found\s*</strong>")
DRILLDOWN_RE = re.compile(r'<a href="([^"]+)"\s+data-drilldown="([^"]+)"')
# The header line: "<n municipalities> · <n listings> listings".
HEADING_RE = re.compile(r"(\d+)\s*·\s*(\d+)\s*listings")
# Cedeira's INE code, so the "facts do not move" case reads its expected
# values out of the committed reference file instead of hard-coding a figure
# that a re-import would age out.
CEDEIRA_INE = "15022"


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


def _mk(profile_id, municipality, **overrides):
    n = next(_SEQ)
    values = dict(
        source_email_id=f"counts:{n}",
        title=f"Listing {n}",
        municipality=municipality,
        listing_status="active",
        property_category="housing",
        search_profile_id=profile_id,
        price=120000,
        area=200.0,
        area_type="built",
        plot_area=1000.0,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _fails(profile_id, municipality, **overrides):
    """A row measurably below both bounds: 100 m² of house on a 300 m² plot."""
    values = dict(area=100.0, plot_area=300.0)
    values.update(overrides)
    return _mk(profile_id, municipality, **values)


@pytest.fixture
def world(app):
    """Cedeira's own shape, small enough that every number is arithmetic.

    Cedeira: three rows that pass (one of them with no price, so the €/m²
    coverage count has something to say) and three that fail at €1,200/m² —
    the cheap-end concentration that inverts the comparison. Ares is carried
    only by failing rows, so it disappears from the table entirely. Sada's one
    failing row is favorited, which the criteria hide never overrules. Ferrol
    belongs to a subscription with no criteria at all, so nothing may touch it
    whatever shape its listing has.
    """
    galicia = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    plain = SearchProfile(name="Asturias", is_active=True)
    db.session.add_all([galicia, plain])
    db.session.commit()

    _mk(galicia.id, "Cedeira")  # 120000 / 200 m² = 600 €/m²
    _mk(galicia.id, "Cedeira", plot_area=900.0)  # 600 €/m²
    _mk(galicia.id, "Cedeira", plot_area=900.0, price=None)  # no €/m²
    for _ in range(3):
        _fails(galicia.id, "Cedeira")  # 120000 / 100 m² = 1200 €/m²

    _fails(galicia.id, "Ares")
    _fails(galicia.id, "Ares")

    _fails(galicia.id, "Sada", is_favorite=True)

    # The same shape under a subscription that states no bounds: its reading
    # is `no_criteria`, and no clause here may reach it.
    _fails(plain.id, "Ferrol")
    return {"galicia": galicia, "plain": plain}


def _links(body):
    """{row key: the URL the page put behind that municipality's name}."""
    return {key: html.unescape(href) for href, key in DRILLDOWN_RE.findall(body)}


def _total(client, url):
    """The unpaginated result count `/properties` reports for `url`."""
    body = client.get(url).get_data(as_text=True)
    match = TOTAL_RE.search(body)
    assert match, f"no result count rendered for {url}"
    return int(match.group(1))


def _cells(body, key):
    """The <td> cells of one municipality row, tags stripped."""
    for chunk in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        if f'data-drilldown="{key}"' not in chunk:
            continue
        return [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)
        ]
    return None


def _text(body):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _page(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.get_data(as_text=True)


class TestTheMunicipalityCountOpensItself:
    """Follow the rendered link and count what returns — every mode."""

    def test_every_row_opens_exactly_its_own_listings(self, client, world):
        body = _page(client, "/municipalities")
        links = _links(body)
        for key, link in links.items():
            listings = int(_cells(body, key)[5])
            assert _total(client, link) == listings, key

    def test_cedeira_counts_the_rows_its_link_opens_and_not_the_others(
        self, client, world
    ):
        """The defect itself, by value: 3 on the page, 3 behind the link.

        Six listings carry Cedeira. Three of them are measured below the
        subscription's bounds and the page the link opens has never shown
        them, which is what made 35 and 28 two statements about one place.
        """
        body = _page(client, "/municipalities")
        assert int(_cells(body, "cedeira")[5]) == 3
        assert _total(client, _links(body)["cedeira"]) == 3
        assert _total(client, "/properties?municipality=Cedeira&criteria=all") == 6, (
            "the fixture's own arithmetic: three of the six are measured fails"
        )

    def test_show_everything_restores_both_halves_together(self, client, world):
        body = _page(client, "/municipalities?criteria=all")
        assert int(_cells(body, "cedeira")[5]) == 6
        assert _total(client, _links(body)["cedeira"]) == 6

    def test_a_one_verdict_mode_travels_to_the_link_too(self, client, world):
        body = _page(client, "/municipalities?criteria=fail")
        assert int(_cells(body, "cedeira")[5]) == 3
        assert _total(client, _links(body)["cedeira"]) == 3
        assert "criteria=fail" in _links(body)["cedeira"]

    def test_the_default_reading_needs_no_parameter_on_the_link(self, client, world):
        """Absence IS the default hide on every surface — stating `default`
        would put a value in the URL the /properties control has no option
        for. The end-to-end case above is what proves the two readings agree;
        this pins that the link does not say something the page did not."""
        assert "criteria=" not in _links(_page(client, "/municipalities"))["cedeira"]


class TestTheMediansAreTakenOverTheSameRows:
    """The reason the counts move here rather than the link."""

    def test_the_price_per_m2_median_is_the_kept_rows_median(self, client, world):
        """600, not 1200: the three excluded rows are the expensive-per-m²
        half, and a median over them describes listings the page cannot open."""
        assert _cells(_page(client, "/municipalities"), "cedeira")[6] == "600 (2)"

    def test_showing_everything_moves_it_back(self, client, world):
        cell = _cells(_page(client, "/municipalities?criteria=all"), "cedeira")[6]
        assert cell == "1200 (5)"

    def test_the_coverage_count_is_over_the_kept_rows(self, client, world):
        """`(2)` against `(5)`: a median over 2 of 3 is a different claim from
        one over 5 of 6, and the denominator is the page's own row count."""
        default = _cells(_page(client, "/municipalities"), "cedeira")
        every = _cells(_page(client, "/municipalities?criteria=all"), "cedeira")
        assert default[5] == "3" and default[6] == "600 (2)"
        assert every[5] == "6" and every[6] == "1200 (5)"

    def test_the_heading_counts_the_rows_and_the_municipalities_shown(
        self, client, world
    ):
        assert HEADING_RE.search(_text(_page(client, "/municipalities"))).groups() == (
            "3",
            "5",
        )
        assert HEADING_RE.search(
            _text(_page(client, "/municipalities?criteria=all"))
        ).groups() == ("4", "10")


class TestMunicipalityFactsDoNotMove:
    """INE and SEPE describe the municipality, with no listing involved."""

    def test_renta_and_population_read_the_same_under_every_mode(self, client, world):
        reference = json.loads(
            Path("data/ine_municipal.json").read_text(encoding="utf-8")
        )["municipalities"][CEDEIRA_INE]
        default = _cells(_page(client, "/municipalities"), "cedeira")
        every = _cells(_page(client, "/municipalities?criteria=all"), "cedeira")
        # Cells 1..4 are the facts block: renta, index, población, unemployment.
        assert default[1:5] == every[1:5]
        assert default[1] == f"€{reference['renta_media_persona']:,.0f}"
        assert default[3].startswith(f"{reference['population']:,.0f}")


class TestWhatTheReadingLeavesOutIsSaidOutLoud:
    """A page that prints 28 where it printed 35 and says nothing is #98."""

    def test_the_note_counts_the_listings_and_the_lost_municipalities(
        self, client, world
    ):
        note = _text(_page(client, "/municipalities"))
        assert "5 listings are outside every count and median on this page" in note
        assert "1 municipality is therefore not shown at all" in note

    def test_a_municipality_carried_only_by_failing_rows_is_the_lost_one(
        self, client, world
    ):
        """Ares is absent from the table, and absent reads as empty unless
        the page says otherwise."""
        default = _page(client, "/municipalities")
        assert "ares" not in _links(default)
        assert "ares" in _links(_page(client, "/municipalities?criteria=all"))

    def test_nothing_excluded_says_nothing(self, client, world):
        body = _page(client, "/municipalities?criteria=all")
        assert 'id="municipalities-criteria-excluded"' not in body

    def test_the_note_explains_the_default_hide_only_where_it_is_the_reading(
        self, client, world
    ):
        """The tooltip describes what the DEFAULT hide never hides. Under an
        explicitly chosen verdict the rows are gone by the reader's own
        choice, and reusing that explanation would describe a narrowing that
        did not happen."""
        note = re.compile(r'id="municipalities-criteria-excluded"(.*?)>', re.S)
        default = note.search(_page(client, "/municipalities")).group(1)
        chosen = note.search(_page(client, "/municipalities?criteria=fail")).group(1)
        assert "A favorited or reviewed listing is never hidden" in default
        assert "title=" not in chosen

    def test_an_unreadable_mode_marks_the_reading_it_actually_applied(
        self, client, world
    ):
        """A stale bookmark asking for a mode that does not exist gets the
        default reading — and the control has to say so. Every option
        un-highlighted over a narrowed page reads as "nothing is on" (#104's
        shape), and the page's own links would go on carrying the word."""
        body = _page(client, "/municipalities?criteria=bogus")
        assert int(_cells(body, "cedeira")[5]) == 3
        assert re.search(
            r'class="dropdown-item active"\s+data-criteria="default"', body
        )
        assert "criteria=bogus" not in body

    def test_the_reader_can_switch_the_reading_from_the_page(self, client, world):
        body = _page(client, "/municipalities")
        assert 'id="municipalities-criteria-control"' in body
        assert 'data-criteria="all"' in body

    def test_the_control_is_absent_when_no_subscription_carries_criteria(
        self, client, world
    ):
        world["galicia"].criteria = None
        db.session.commit()
        body = _page(client, "/municipalities")
        assert 'id="municipalities-criteria-control"' not in body
        assert 'id="municipalities-criteria-excluded"' not in body
        assert int(_cells(body, "cedeira")[5]) == 6, (
            "with no criteria set the reading may narrow nothing"
        )


class TestTheExemptionsAndTheUntouchedRows:
    """The hide is `subscription_criteria`'s, imported and not re-stated."""

    def test_a_favorited_failing_row_is_still_counted(self, client, world):
        body = _page(client, "/municipalities")
        assert int(_cells(body, "sada")[5]) == 1
        assert _total(client, _links(body)["sada"]) == 1

    def test_a_subscription_with_no_criteria_is_never_narrowed(self, client, world):
        body = _page(client, "/municipalities")
        assert int(_cells(body, "ferrol")[5]) == 1
        assert _total(client, _links(body)["ferrol"]) == 1


class TestTheSubscriptionMenuCountsWhatPickingItShows:
    """The other number on this page that carries a link."""

    def test_the_menu_count_is_the_count_under_the_current_reading(self, client, world):
        body = _page(client, "/municipalities")
        galicia = world["galicia"].id
        menu = re.search(
            rf'href="[^"]*profile_id={galicia}[^"]*"[^>]*>(.*?)</a>', body, re.S
        )
        assert menu, "the subscription menu did not offer the criteria subscription"
        assert re.search(r"\b4\b", _text(menu.group(1))), (
            "the menu promised a count the picked page does not show"
        )
        picked = _text(_page(client, f"/municipalities?profile_id={galicia}"))
        assert HEADING_RE.search(picked).group(2) == "4"

    def test_showing_everything_offers_the_whole_subscription(self, client, world):
        galicia = world["galicia"].id
        body = _page(client, "/municipalities?criteria=all")
        menu = re.search(
            rf'href="[^"]*profile_id={galicia}[^"]*"[^>]*>(.*?)</a>', body, re.S
        )
        assert re.search(r"\b9\b", _text(menu.group(1)))
        picked = _text(
            _page(client, f"/municipalities?profile_id={galicia}&criteria=all")
        )
        assert HEADING_RE.search(picked).group(2) == "9"


class TestTheProfilesColumnOpensItsOwnNumber:
    """There the number is a fact about the subscription, so the link moves."""

    def _column(self, body, profile_id):
        match = re.search(
            rf'<a class="small text-decoration-none" href="([^"]*profile_id={profile_id}[^"]*)">\s*(\d+)\s*</a>',
            body,
        )
        assert match, f"no listings count rendered for profile {profile_id}"
        return html.unescape(match.group(1)), int(match.group(2))

    def test_the_count_still_counts_every_stored_listing(self, client, world):
        _, count = self._column(_page(client, "/profiles"), world["galicia"].id)
        assert count == 9, "the column answers how many listings the search holds"

    def test_the_link_opens_exactly_that_number(self, client, world):
        link, count = self._column(_page(client, "/profiles"), world["galicia"].id)
        assert _total(client, link) == count

    def test_the_unassigned_row_states_the_same_scope(self, client, app, world):
        """An unassigned row is outside every subscription's criteria clause,
        so this one cannot be proved end-to-end today — the link is pinned by
        value instead, because what makes it honest is that it names the scope
        the number was counted under, not that the two happen to agree."""
        _mk(None, "Cudillero", area=100.0, plot_area=300.0)
        body = _page(client, "/profiles")
        match = re.search(
            r'href="([^"]*profile_id=unassigned[^"]*)">\s*(\d+)\s*</a>', body
        )
        assert match, "the unassigned bucket lost its count"
        link = html.unescape(match.group(1))
        assert "criteria=all" in link and "hide_removed=off" in link
        assert _total(client, link) == int(match.group(2)) == 1


class TestTheControlOffersWhatTheCodeApplies:
    """The reviewer's finding: CRITERIA_MODES' docstring claimed to be the one
    home "so a surface DRAWING the control and the code APPLYING it cannot
    disagree", while templates/municipalities.html carried a literal copy of
    the list. A constant that claims to be a single home and is not is worse
    than no constant, because the next reader acts on the claim."""

    def test_the_dropdown_offers_exactly_the_modes_that_are_applied(
        self, client, world
    ):
        import re

        from routes.main_routes import CRITERIA_MODES

        html = client.get("/municipalities").data.decode()
        offered = set(re.findall(r'data-criteria="([^"]+)"', html))
        assert offered, "the criteria control must render on /municipalities"
        assert offered == {"default"} | set(CRITERIA_MODES), (
            f"the control offers {sorted(offered)} while the code applies "
            f"{sorted(CRITERIA_MODES)} plus the default"
        )
