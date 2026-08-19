"""The number on a /municipalities row and the page its link opens (#417).

They are two statements about one municipality and they were taken under two
different scopes. The aggregate counts every stored listing; the link said
`profile_id=all`, which this codebase defines as *active and not hidden*, and
carried no listing-status parameter at all -- so `/properties` read the
presence of `municipality` as a submitted form with the "hide removed" box
unticked. Measured against production on 2026-08-19: 38 of 87 rows disagreed
with the page they linked to and 13 opened on zero, because 311 of 773
listings sit in retired subscriptions.

What is pinned here is the acceptance condition itself -- *follow the link the
page actually rendered and count what comes back* -- rather than the shape of
the URL. A test that only asserted "the href contains profile_id=6" would stay
green through a `/properties` that read the parameter differently, which is
the half of this defect that lived on the other page.

Four axes, each with its own case, because production exercises only two of
them today (0 unassigned rows, 0 hidden profiles) and a contract nobody tests
is a contract that breaks the first time the data reaches it.
"""

import html
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.municipality_comparison_service import (
    MunicipalityComparisonService,
    drilldown_args,
    drilldown_truncates,
)
from services.profile_selection import (
    MAX_SELECTED_PROFILE_IDS,
    parse_profile_selection,
)
from tests import setup_test_environment

TOTAL_RE = re.compile(r"<strong>\s*(\d+)\s+properties found\s*</strong>")
DRILLDOWN_RE = re.compile(r'<a href="([^"]+)"\s+data-drilldown="([^"]+)"')


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


_counter = {"n": 0}


def _listing(municipality, profile=None, **overrides):
    _counter["n"] += 1
    fields = dict(
        source_email_id=f"drilldown-{_counter['n']}",
        title=f"L{_counter['n']}",
        municipality=municipality,
        listing_status="active",
        property_category="housing",
        search_profile_id=(profile.id if profile is not None else None),
    )
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


@pytest.fixture
def world(app):
    """Production in miniature: a live search, a retired one, a hidden one.

    Gijón is carried by all four kinds of row at once, in two spellings, so
    one municipality exercises the grouping and every scope axis together.
    Nava is the issue's own case -- a municipality whose every listing sits in
    a subscription `/properties` does not offer, which is what "13 rows open
    on zero" was.
    """
    live = SearchProfile(name="Land at Norte", is_active=True)
    retired = SearchProfile(name="Quesada", is_active=False)
    hidden = SearchProfile(name="Solares Norte", is_active=True, is_hidden=True)
    db.session.add_all([live, retired, hidden])
    db.session.commit()

    _listing("Gijón", live, is_favorite=True)
    _listing("Gijon", retired)
    _listing("Gijón", hidden)
    _listing("Gijón", None)
    _listing("Gijón", live, listing_status="removed")

    _listing("Nava", retired)
    _listing("Nava", retired)

    # Navia is one subscription's alone, and holds a favorite beside a
    # non-favorite: without that pair the favorites axis is invisible to the
    # end-to-end check, because every other municipality's favorite is the
    # only row its profile scope reaches anyway. (Measured -- dropping
    # `favorites` from the link left the whole drill-down suite green.)
    _listing("Navia", live)
    _listing("Navia", live, is_favorite=True)
    return {"live": live, "retired": retired, "hidden": hidden}


def _links(body):
    """{row key: the URL the page put behind that municipality's name}."""
    return {key: html.unescape(href) for href, key in DRILLDOWN_RE.findall(body)}


def _total(client, url):
    """The unpaginated result count `/properties` reports for `url`."""
    body = client.get(url).get_data(as_text=True)
    match = TOTAL_RE.search(body)
    assert match, f"no result count rendered for {url}"
    return int(match.group(1))


def _ids(client, url):
    """The listing ids `/properties` shows for `url`, unpaginated."""
    joiner = "&" if "?" in url else "?"
    body = client.get(f"{url}{joiner}per_page=100").get_data(as_text=True)
    return {int(value) for value in re.findall(r"/properties/(\d+)", body)}


def _rows(properties):
    service = MunicipalityComparisonService()
    return {row["key"]: row for row in service.build_rows(properties)}


class TestTheScopeTravelsWithTheRow:
    """`build_rows` records what it counted, off the rows it counted."""

    def test_the_row_names_every_contributing_subscription(self, app, world):
        rows = _rows(Property.query.filter(Property.listing_status != "removed").all())
        scope = rows["gijon"]["scope"]
        assert scope["profile_counts"] == {
            world["live"].id: 1,
            world["retired"].id: 1,
            world["hidden"].id: 1,
        }, "a retired or hidden subscription still carried listings into the median"
        assert scope["unassigned"] == 1

    def test_the_ids_are_ordered_so_one_municipality_is_one_url(self, app, world):
        # Fed in *reverse* id order on purpose. The fixture builds its profiles
        # and its listings in ascending order, so `Property.query.all()` hands
        # `build_rows` a dict that is already sorted -- and this assertion then
        # passed with the `sorted()` call removed, which is a test that cannot
        # fail dressed as one that can.
        rows = _rows(list(reversed(Property.query.order_by(Property.id).all())))
        ids = list(rows["gijon"]["scope"]["profile_counts"])
        assert ids == sorted(ids)

    def test_a_municipality_in_one_subscription_records_only_that_one(self, app, world):
        rows = _rows(Property.query.all())
        assert rows["navia"]["scope"] == {
            "profile_counts": {world["live"].id: 2},
            "unassigned": 0,
        }


class TestTheLinkCarriesFourAxes:
    """What `drilldown_args` puts in the URL, and why each part is there."""

    def test_it_names_the_contributing_ids_instead_of_the_all_sentinel(
        self, app, world
    ):
        rows = _rows(Property.query.all())
        args = drilldown_args(rows["nava"])
        assert args["profile_id"] == [world["retired"].id], (
            "`all` means active-and-not-hidden, which is not what was counted"
        )
        assert "all" not in args["profile_id"]

    def test_unassigned_rows_get_their_own_token(self, app, world):
        rows = _rows(Property.query.all())
        assert "unassigned" in drilldown_args(rows["gijon"])["profile_id"], (
            "`search_profile_id IS NULL` is never part of `all`"
        )

    def test_the_listing_status_scope_is_pinned_even_at_its_default(self, app, world):
        rows = _rows(Property.query.all())
        assert drilldown_args(rows["nava"])["hide_removed"] == "on", (
            "/properties reads an absent hide_removed beside a municipality "
            "as an unticked box, not as its own default"
        )
        assert (
            drilldown_args(rows["nava"], include_archived=True)["hide_removed"] == "off"
        )

    def test_the_favorites_mode_travels(self, app, world):
        rows = _rows(Property.query.all())
        assert drilldown_args(rows["nava"])["favorites"] is None
        assert drilldown_args(rows["nava"], favorites_only=True)["favorites"] == "on"

    def test_a_row_with_no_subscription_at_all_never_widens_to_everything(self):
        """An empty scope must not fall back to `all` or to no filter.

        Unreachable from `build_rows` (a group exists because rows made it),
        and pinned anyway: the failure mode is silent -- a link that shows
        more than it counted looks like a working link.
        """
        args = drilldown_args({"name": "Nowhere", "scope": {}})
        assert args["profile_id"] == ["0"]


class TestTheDrillDownReturnsWhatWasCounted:
    """The acceptance condition: follow the rendered link, count what returns."""

    def test_every_row_opens_exactly_its_own_listings(self, app, client, world):
        body = client.get("/municipalities").get_data(as_text=True)
        links = _links(body)
        expected = _rows(
            Property.query.filter(
                Property.listing_status.notin_(("removed", "sold"))
            ).all()
        )
        assert set(links) == set(expected), "a row with no link, or a link with no row"
        for key, row in expected.items():
            assert _total(client, links[key]) == row["listings"], key

    def test_the_drill_down_holds_the_same_listings_the_median_used(
        self, app, client, world
    ):
        """Equal totals could still be two different sets of rows."""
        scoped = Property.query.filter(
            Property.listing_status.notin_(("removed", "sold"))
        ).all()
        counted = {
            prop.id
            for prop in scoped
            if (prop.municipality or "").strip().lower().startswith("gij")
        }
        links = _links(client.get("/municipalities").get_data(as_text=True))
        assert _ids(client, links["gijon"]) == counted

    def test_a_municipality_carried_only_by_a_retired_search_is_not_zero(
        self, app, client, world
    ):
        links = _links(client.get("/municipalities").get_data(as_text=True))
        assert _total(client, links["nava"]) == 2, (
            "the issue's own case: 13 of 87 rows opened on an empty page"
        )

    def test_the_archived_view_opens_the_removed_listings_too(self, app, client, world):
        body = client.get("/municipalities?archived=on").get_data(as_text=True)
        links = _links(body)
        expected = _rows(Property.query.all())
        for key, row in expected.items():
            assert _total(client, links[key]) == row["listings"], key
        assert expected["gijon"]["listings"] == 5, "the removed listing is counted"

    def test_the_favorites_view_opens_only_the_favorites(self, app, client, world):
        body = client.get("/municipalities?favorites=on").get_data(as_text=True)
        links = _links(body)
        expected = _rows(
            Property.query.filter(
                Property.is_favorite.is_(True),
                Property.listing_status.notin_(("removed", "sold")),
            ).all()
        )
        assert set(links) == set(expected)
        for key, row in expected.items():
            assert _total(client, links[key]) == row["listings"], key

    def test_sorting_does_not_change_what_a_link_opens(self, app, client, world):
        body = client.get("/municipalities?sort=score&order=asc").get_data(as_text=True)
        links = _links(body)
        expected = _rows(
            Property.query.filter(
                Property.listing_status.notin_(("removed", "sold"))
            ).all()
        )
        for key, row in expected.items():
            assert _total(client, links[key]) == row["listings"], key


class TestMoreSubscriptionsThanOneLinkCanName:
    """Past `MAX_SELECTED_PROFILE_IDS` the link undercounts, and says so here.

    `profile_id` accepts 50 ids because the parsed list goes into a SQL
    `IN (...)` and a hand-written URL is not obliged to be reasonable. The
    aggregate has no such bound, so a municipality carried by more than 50
    subscriptions is this ticket's own defect one regime further out --
    /properties discloses the truncation only after the click.

    Unreachable in production (15 subscriptions in all), which is why it is
    pinned rather than left to be rediscovered.
    """

    def _row(self, count):
        return {
            "name": "Gijón",
            "scope": {
                "profile_counts": {n: 1 for n in range(1, count + 1)},
                "unassigned": 0,
            },
        }

    def test_the_cap_is_read_from_the_module_that_owns_it(self):
        assert not drilldown_truncates(self._row(MAX_SELECTED_PROFILE_IDS))
        assert drilldown_truncates(self._row(MAX_SELECTED_PROFILE_IDS + 1))

    def test_the_link_still_names_every_id_rather_than_dropping_some_here(self):
        """Truncating in the builder would be the silent half of the same act:
        the parser drops the tail either way, and only it tells the page."""
        args = drilldown_args(self._row(MAX_SELECTED_PROFILE_IDS + 1))
        assert len(args["profile_id"]) == MAX_SELECTED_PROFILE_IDS + 1
        parsed = parse_profile_selection({"profile_id": args["profile_id"]})
        assert parsed.truncated
        assert len(parsed.ids) == MAX_SELECTED_PROFILE_IDS

    def test_the_page_warns_before_the_click(self, app, client):
        profiles = [
            SearchProfile(name=f"S{n}", is_active=True)
            for n in range(MAX_SELECTED_PROFILE_IDS + 1)
        ]
        db.session.add_all(profiles)
        db.session.commit()
        for profile in profiles:
            _listing("Gijón", profile)
        body = client.get("/municipalities").get_data(as_text=True)
        assert 'data-drilldown-truncated="gijon"' in body
        assert "shows fewer listings than the count here" in body

    def test_a_row_within_the_cap_carries_no_warning(self, app, client, world):
        body = client.get("/municipalities").get_data(as_text=True)
        assert "data-drilldown-truncated" not in body


class TestThePageSaysWhatItCovers:
    """One line, because the page has no subscription control of its own."""

    def _note(self, body):
        match = re.search(r'id="municipalities-scope-note".*?</div>', body, re.S)
        return match.group(0) if match else None

    def test_it_counts_the_subscriptions_properties_does_not_offer(
        self, app, client, world
    ):
        note = self._note(client.get("/municipalities").get_data(as_text=True))
        assert note is not None
        # The retired one and the hidden one; the live subscription is offered
        # and is not withheld. Four listings: Gijon 1 + Nava 2 in the retired
        # search, Gijón 1 in the hidden one.
        assert "2 retired or hidden subscriptions" in note
        assert "4 listings" in note
        # And the unassigned row separately -- `profile_id=all` never covers
        # it either, but it is not a subscription.
        assert "1 listing with no subscription" in note

    def test_it_speaks_spanish_on_a_spanish_page(self, app, client, world):
        with client.session_transaction() as session:
            session["language"] = "es"
        note = self._note(client.get("/municipalities").get_data(as_text=True))
        assert note is not None
        assert "2 suscripciones retiradas u ocultas" in note
        assert "4 anuncios" in note
        assert "1 anuncio sin suscripción" in note

    def test_one_subscription_is_written_in_the_singular(self, app, client):
        retired = SearchProfile(name="Quesada", is_active=False)
        db.session.add(retired)
        db.session.commit()
        _listing("Nava", retired)
        note = self._note(client.get("/municipalities").get_data(as_text=True))
        assert "1 retired or hidden subscription" in note
        assert "1 listing" in note and "1 listings" not in note

    def test_nothing_is_said_when_every_subscription_is_live(self, app, client):
        live = SearchProfile(name="Land at Norte", is_active=True)
        db.session.add(live)
        db.session.commit()
        _listing("Navia", live)
        body = client.get("/municipalities").get_data(as_text=True)
        assert self._note(body) is None, "a disclosure with nothing to disclose"

    def test_a_municipality_of_unassigned_listings_alone_still_says_so(
        self, app, client
    ):
        """The whole row is absent from a bare /properties, and nothing else
        on the page would have mentioned it: there is no subscription to
        count, so a note keyed on profiles alone rendered nothing at all."""
        live = SearchProfile(name="Land at Norte", is_active=True)
        db.session.add(live)
        db.session.commit()
        _listing("Navia", live)
        _listing("Pravia", None)
        _listing("Pravia", None)
        note = self._note(client.get("/municipalities").get_data(as_text=True))
        assert note is not None
        assert "2 listings with no subscription" in note
        assert "retired or hidden" not in note, (
            "an unassigned listing is not a subscription"
        )
