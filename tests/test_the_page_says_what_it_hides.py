"""Three places where /properties said something false about its own filtering.

All three are #98 in copy rather than in data: an absence of a *match* is
rendered as an absence of the *row*, and the remedy offered is not the one
that works.

Reproduced against production (commit 9c890d7) on 2026-08-31:

* `/properties?search=35241157` printed "Read as Idealista listing 35241157 —
  nothing here carries that id." for property 1458, four lines above the same
  page's own "Criteria: 1 failing hidden". It is NOT criteria-specific:
  `?search=112408790&municipality=Gijón` printed the identical sentence for
  property 1537, an ordinary visible row that no criteria touch.
* `?profile_id=24&search=Brantuas` — a phrase out of hidden row 995's own
  title — offered "run a manual sync to fetch new listings" as the only
  remedy for a listing that was ingested weeks ago.
* `/properties/995` said nothing whatever about the subscription criteria
  that hide it; its only "criteria" was the scoring-weights card.

**Every assertion here also proves the page RENDERED.** `routes/
main_routes.py` turns a template error into a flash and a second render with
no rows, which shows "0 properties found" and none of these lines — so a test
that only checks for the absence of the old sentence stays green through
exactly that failure (CLAUDE.md records the earlier version of
`tests/test_listing_search_by_url.py` doing it).
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}

# The flash the error path renders instead of the page. Every helper below
# asserts it is absent, so "the line is missing" can never read as a pass.
RENDER_FAILED = "An error occurred while loading properties"


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
def galicia(app):
    row = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def asturias(app):
    row = SearchProfile(name="Asturias", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _mk(profile_id, **overrides):
    n = next(_SEQ)
    values = dict(
        source_email_id=f"hides:{n}",
        title=f"Listing {n}",
        price=100000,
        search_profile_id=profile_id,
        area=200,
        area_type="built",
        municipality="Ponteceso",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _page(client, url):
    """GET a listing page and refuse to read a failed render as an answer."""
    response = client.get(url)
    assert response.status_code == 200, url
    html = response.data.decode()
    assert RENDER_FAILED not in html, f"{url} rendered the error path, not the page"
    assert "properties_found" not in html  # the i18n key, never the string
    return html


def _href(html, element_id):
    """The href of one link on the page, unescaped for the test client."""
    marker = f'id="{element_id}" href="'
    assert marker in html, f"no {element_id} on the page"
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)].replace("&amp;", "&")


class TestASearchThatNamesAListingTheFiltersHide:
    """Defect 1, the general case: any narrowing, not only the criteria."""

    def test_a_criteria_hidden_row_is_not_reported_as_absent(self, client, galicia):
        # Property 1458's shape: bare land 150 m², so the plot bound (700) is
        # a MEASURED shortfall and the default view hides it.
        hidden = _mk(
            galicia.id,
            title="Casa en Cervo",
            area=150,
            area_type="plot",
            idealista_property_id=35241157,
        )

        html = _page(client, "/properties?search=35241157")

        assert "0 properties found" in html
        assert "nothing here carries that id" not in html
        assert "Read as Idealista listing 35241157." in html
        assert f"It is here as #{hidden.id}, and the filters on this page hide it." in (
            html
        )

    def test_an_ordinary_row_under_an_ordinary_filter_is_not_reported_as_absent(
        self, client, galicia
    ):
        # Property 1537's shape: 150 m² built, so its house bound passes and
        # its plot is unmeasured — `unknown`, which no criteria hide. The
        # municipality filter is what removes it, which is why this defect
        # was never a criteria defect.
        row = _mk(
            galicia.id,
            title="Casa en Carballo",
            area=150,
            municipality="Carballo",
            idealista_property_id=112408790,
        )

        html = _page(client, "/properties?search=112408790&municipality=Gij%C3%B3n")

        assert "nothing here carries that id" not in html
        assert f"It is here as #{row.id}, and the filters on this page hide it." in html

    def test_a_listing_nothing_carries_still_says_so(self, client, galicia):
        _mk(galicia.id, idealista_property_id=11111111)

        html = _page(client, "/properties?search=99999999")

        assert "Read as Idealista listing 99999999 — nothing here carries that id." in (
            html
        )
        assert "It is here as #" not in html

    def test_a_row_in_another_subscription_says_which_fact_it_is(
        self, client, galicia, asturias
    ):
        row = _mk(asturias.id, title="Casa en Gijón", idealista_property_id=22222222)

        html = _page(client, f"/properties?profile_id={galicia.id}&search=22222222")

        assert (
            f"It is here as #{row.id}, in a subscription this page is not showing."
        ) in html
        assert "and the filters on this page hide it" not in html

    def test_a_pasted_url_gets_the_same_answer(self, client, galicia):
        row = _mk(
            galicia.id,
            title="Chalet",
            area=150,
            area_type="plot",
            url="https://www.fotocasa.es/es/comprar/casa/cervo/tal-cual/187654321/d",
        )

        html = _page(
            client,
            "/properties?search=https%3A%2F%2Fwww.fotocasa.es%2Fes%2Fcomprar"
            "%2Fcasa%2Fcervo%2Ftal-cual%2F187654321%2Fd",
        )

        assert "nothing here carries that link" not in html
        assert f"It is here as #{row.id}, and the filters on this page hide it." in html


class TestTheRevealLinkActuallyReveals:
    """The link is the whole point: #445's defect was a recovery link that
    re-issued the filter it promised to clear."""

    def _href(self, html):
        return _href(html, "search-reveal-link")

    def test_it_clears_the_criteria_hide_rather_than_re_issuing_it(
        self, client, galicia
    ):
        hidden = _mk(
            galicia.id,
            title="Casa en Cervo",
            area=150,
            area_type="plot",
            idealista_property_id=35241157,
        )

        href = self._href(_page(client, "/properties?search=35241157"))
        # Stated out loud, not merely dropped: `criteria`'s absent state IS
        # the hide (utils/listing_filters.CLEARED_NOT_ABSENT).
        assert "criteria=all" in href
        assert "Casa en Cervo" in _page(client, href)
        assert "1 properties found" in _page(client, href)
        assert f"/properties/{hidden.id}" in _page(client, href)

    def test_it_clears_an_ordinary_filter_too(self, client, galicia):
        _mk(
            galicia.id,
            title="Casa en Carballo",
            area=150,
            municipality="Carballo",
            idealista_property_id=112408790,
        )

        href = self._href(
            _page(client, "/properties?search=112408790&municipality=Gij%C3%B3n")
        )

        assert "municipality=Gij" not in href
        assert "Casa en Carballo" in _page(client, href)

    def test_it_reaches_a_row_in_another_subscription(self, client, galicia, asturias):
        _mk(asturias.id, title="Casa en Gijón", idealista_property_id=22222222)

        href = self._href(
            _page(client, f"/properties?profile_id={galicia.id}&search=22222222")
        )

        assert "Casa en Gijón" in _page(client, href)

    def test_it_reaches_a_withdrawn_row(self, client, galicia):
        # `hide_removed` defaults ON, and `mode`/`view_type` ride across in
        # NON_FILTERS — which is exactly what `resolve_hide_removed` reads as
        # "this came from the filter form". Leaving it unsaid makes the
        # promise depend on which link the reader arrived by.
        _mk(
            galicia.id,
            title="Casa retirada",
            listing_status="removed",
            idealista_property_id=33333333,
        )

        html = _page(client, "/properties?search=33333333")
        href = self._href(html)

        assert "hide_removed=off" in href
        assert "Casa retirada" in _page(client, href)


class TestTheEmptyStateNamesARemedyThatWorks:
    """Defect 2: the only remedy offered was to ingest more listings."""

    def test_it_counts_what_clearing_would_show_and_links_to_it(self, client, galicia):
        _mk(galicia.id, title="Casa en Brantuas", area=137)
        _mk(galicia.id, title="Otra casa", area=137)

        html = _page(client, f"/properties?profile_id={galicia.id}&search=Brantuas")

        assert "0 properties found" in html
        assert (
            "Nothing here matches the filters on this page. "
            "Your subscription selection holds 2 listings."
        ) in html
        cleared = _page(client, _href(html, "empty-state-clear-link"))
        assert "Casa en Brantuas" in cleared
        assert "2 properties found" in cleared

    def test_the_count_is_the_number_its_own_link_lands_on(self, client, galicia):
        # The disclosed number has to be the number that page shows -- the
        # rule `unassigned_count` already states. Both switches count as
        # filters here, so a withdrawn row is in the count AND on the page:
        # `hide_removed` defaults ON and has to be stated, exactly as
        # `criteria` does.
        _mk(galicia.id, title="Casa viva", area=137)
        _mk(galicia.id, title="Casa retirada", area=137, listing_status="removed")

        html = _page(client, f"/properties?profile_id={galicia.id}&search=Brantuas")

        assert "Your subscription selection holds 2 listings." in html
        cleared = _page(client, _href(html, "empty-state-clear-link"))
        assert "2 properties found" in cleared
        assert "Casa retirada" in cleared

    def test_a_genuinely_empty_scope_offers_no_such_line(self, client, galicia):
        html = _page(client, f"/properties?profile_id={galicia.id}")

        assert "0 properties found" in html
        assert "Your subscription selection holds" not in html

    def test_the_sync_sentence_is_no_longer_the_only_remedy(self, client, galicia):
        _mk(galicia.id, title="Casa en Brantuas", area=137)

        html = _page(client, f"/properties?profile_id={galicia.id}&search=Brantuas")

        assert "Try adjusting your filters, or run a manual sync" not in html


class TestAHiddenRowsOwnPageSaysWhy:
    """Defect 3, asserted BY VALUE — a card full of Nones must fail."""

    def test_it_names_the_bound_that_is_missed(self, client, galicia):
        # Property 995's own figures: 137 m² built against a 150 m² minimum,
        # plot never stated.
        row = _mk(galicia.id, title="Casa en Ponteceso", area=137)

        html = client.get(f"/properties/{row.id}").data.decode()

        assert "This listing does not meet its subscription&#39;s criteria" in html
        assert "so the listing page hides it by default" in html
        assert "Galicia · costa" in html
        assert "House at least 150 m²:" in html
        assert "137 m² — below the minimum" in html
        assert "Plot at least 700 m²:" in html
        assert "not stated, so this bound cannot be checked" in html

    def test_a_judged_row_is_told_it_is_shown_anyway(self, client, galicia):
        row = _mk(galicia.id, title="Casa favorita", area=137, is_favorite=True)

        html = client.get(f"/properties/{row.id}").data.decode()

        assert "It is shown anyway, because you have already judged it." in html
        assert "so the listing page hides it by default" not in html
        # And the list really does draw it, so the page is not lying.
        assert "Casa favorita" in _page(client, f"/properties?profile_id={galicia.id}")

    def test_a_passing_row_says_so_with_its_figures(self, client, galicia):
        row = _mk(galicia.id, title="Casa grande", area=200, plot_area=900)

        html = client.get(f"/properties/{row.id}").data.decode()

        assert "This listing meets its subscription&#39;s criteria." in html
        assert "200 m² — meets the minimum" in html
        assert "900 m² — meets the minimum" in html

    def test_an_unmeasured_row_is_never_reported_as_failing(self, client, galicia):
        row = _mk(galicia.id, title="Casa sin parcela", area=200)

        html = client.get(f"/properties/{row.id}").data.decode()

        assert "has not been measured against its subscription&#39;s criteria" in html
        assert "does not meet its subscription&#39;s criteria" not in html

    def test_a_subscription_without_criteria_draws_no_card(self, client, asturias):
        row = _mk(asturias.id, title="Casa cualquiera", area=137)

        html = client.get(f"/properties/{row.id}").data.decode()

        assert 'id="subscription-criteria-card"' not in html


class TestTheTwoReadingsOfTheHideAgree:
    """`hidden_by_default` is the Python twin of
    `hidden_by_default_expression`, so the row's own page and the list's
    "N failing hidden" cannot become a third wrong number."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"is_favorite": True},
            {"owner_verdict": "interested"},
            {"next_action": "Call the agency"},
            {"area": 200, "plot_area": 900},
            {"area": 200},
        ],
    )
    def test_python_and_sql_answer_alike(self, app, galicia, overrides):
        from services import subscription_criteria

        values = dict(area=137, area_type="built")
        values.update(overrides)
        prop = _mk(galicia.id, **values)

        hidden_sql = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.hidden_by_default_expression(Property, CRITERIA)
            )
        }
        assert subscription_criteria.hidden_by_default(prop, CRITERIA) == (
            prop.id in hidden_sql
        ), f"the two languages disagree for {overrides}"

    def test_no_criteria_hides_nothing(self, app, asturias):
        from services import subscription_criteria

        prop = _mk(asturias.id, area=10)

        assert subscription_criteria.hidden_by_default(prop, None) is False


class TestTheRevealLinkSurvivesANullStatus:
    """The reviewer's finding: `None not in ("removed", "sold")` is True in
    Python, so the link called `hide_removed=on` safe — while the page filters
    with `notin_(...)`, and under SQL's three-valued logic `NULL NOT IN (...)`
    is NULL, which drops the row. The link promised to reveal a listing and
    landed on a page that still hid it.

    Production holds no NULL status today (1536 active, 1 removed, measured
    2026-08-31), so this is latent — but direct SQL is a supported workflow in
    this repository and the row shape is reachable.
    """

    def test_a_null_status_row_is_really_revealed(self, client, app):
        import re

        from sqlalchemy import text

        prop = _mk(
            None,
            title="Null status row",
            area=100,
            area_type="built",
            idealista_property_id=99887766,
        )
        # Only direct SQL can produce it: the column carries a Python-side
        # default that SQLAlchemy applies to any attribute that is None.
        db.session.execute(
            text("UPDATE properties SET listing_status = NULL WHERE id = :i"),
            {"i": prop.id},
        )
        db.session.commit()
        # The identity map still holds the row as the ORM wrote it, so without
        # this the route re-reads 'active' and the test fails for a reason that
        # is not the defect.
        db.session.expire_all()
        assert (
            db.session.execute(
                text("SELECT listing_status FROM properties WHERE id = :i"),
                {"i": prop.id},
            ).scalar()
            is None
        ), "the fixture must really store NULL, or this test proves nothing"

        # Search by the LISTING id, which is what the box reads — the row's
        # internal id names nothing, and searching by it drew no reveal line
        # at all in the first version of this test.
        body = client.get("/properties?search=99887766").data.decode()
        # Anchor on the reveal link itself. A bare href regex matches the
        # clear-filters link first, which carries profile_id=all and is a
        # different promise -- the first version of this test did exactly that
        # and reported the wrong element's parameters.
        match = re.search(r'id="search-reveal-link" href="([^"]+)"', body)
        if match is None:
            # No reveal line means the row was not hidden at all; then there is
            # nothing to promise and nothing to break.
            assert "Null status row" in body
            return
        link = match.group(1).replace("&amp;", "&")
        assert "hide_removed=off" in link, (
            f"a NULL-status row is dropped by hide_removed=on, so the link that "
            f"promises to reveal it must not ask for it: {link!r}"
        )
        revealed = client.get(link).data.decode()
        assert "Null status row" in revealed, (
            f"following {link!r} did not reveal the listing it named"
        )
