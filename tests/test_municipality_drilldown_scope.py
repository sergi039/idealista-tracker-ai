"""A municipality row opens the listings it counted (#417, MUNIC-001).

`/municipalities` draws a listing count on every row and links that row to
`/properties`. The two used to answer different questions: the aggregate
counts **every** stored listing, while the link said `profile_id=all`, which
this codebase defines as *active and not hidden*. Measured on production
2026-08-19 at both pages' defaults, 49 of 87 drill-downs agreed with the row
above them; 25 undercounted by 259 listings and 13 opened on zero while
claiming 52. Gijón said 79 and showed 42.

Four axes have to travel for the link to mean the row, and they are pinned
here one at a time as well as end to end:

1. the exact contributing subscriptions, retired and hidden ones included --
   there is no other spelling that reaches them;
2. the unassigned rows, and **only** when such rows really contributed:
   `all` never implies them and neither does this;
3. the favorites mode the aggregate was computed under;
4. the listing-status scope, whose two names are opposites --
   `/municipalities?archived=on` includes the delisted rows and
   `/properties?hide_removed=on` excludes them -- so this is the one axis
   where copying the parameter across would be exactly wrong.

The end-to-end assertion is the contract itself: fetch the page, read the
number the row claims and the link it offers, follow the link, and compare
with the unpaginated total /properties prints. Reading both numbers off
rendered HTML is deliberate -- a test that called the service directly would
pass over a template that never used what the service returned.
"""

import html as htmlmod
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# One municipality row: its name, the count it claims, the link it offers.
_ROW = re.compile(
    r"<tr>(?P<body>.*?)</tr>",
    re.S,
)
_LINK = re.compile(
    r'<a href="(?P<href>/properties\?[^"]*)"\s+class="text-decoration-none fw-semibold">'
    r"(?P<name>[^<]+)</a>",
    re.S,
)
_END_CELL = re.compile(r'<td class="text-end">(.*?)</td>', re.S)
# "79 properties found" -- the unpaginated total, not the page size.
_FOUND = re.compile(r"<strong>\s*(\d+)\s+[^<]*</strong>")


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


@pytest.fixture
def inventory(app):
    """Two municipalities spread across every kind of subscription.

    Gijón is spelled three ways on purpose: the aggregate groups them under
    one key, so the link has to reach all of them -- and the third spelling
    sits on a delisted row, where the aggregate cannot see it but the link's
    spelling lookup can. Avilés is the control -- its
    only unassigned listing is delisted, so that row is outside the default
    aggregate and inside the archived one, which is what makes "carry
    `unassigned` only when it contributed" a claim with two sides.
    """
    with app.app_context():
        live = SearchProfile(name="Land at Norte", is_active=True)
        retired = SearchProfile(name="Legacy Lands", is_active=False)
        hidden = SearchProfile(name="Solares Norte", is_active=True, is_hidden=True)
        db.session.add_all([live, retired, hidden])
        db.session.commit()
        ids = {"live": live.id, "retired": retired.id, "hidden": hidden.id}

        def add(slug, municipality, profile_id, **kwargs):
            db.session.add(
                Property(
                    source_email_id=f"drilldown_{slug}",
                    title=f"{slug} listing",
                    municipality=municipality,
                    search_profile_id=profile_id,
                    listing_status=kwargs.pop("listing_status", "active"),
                    price=200000,
                    area=1000,
                    **kwargs,
                )
            )

        add("gijon_live_a", "Gijón", ids["live"])
        add("gijon_live_b", "Gijón", ids["live"], is_favorite=True)
        # The other spelling of the same municipality, in a retired
        # subscription -- the two halves of the production defect at once.
        add("gijon_retired_a", "Gijon", ids["retired"])
        add("gijon_retired_b", "Gijon", ids["retired"], is_favorite=True)
        add("gijon_retired_c", "Gijon", ids["retired"])
        add("gijon_hidden", "Gijón", ids["hidden"])
        add("gijon_orphan", "Gijón", None)
        # A third spelling that exists only on a delisted row. At the default
        # the aggregate never sees it, but `stored_spellings_of` walks the
        # whole table, so the link's IN-clause is a strict superset of the
        # spellings the row was built from -- which is safe only because the
        # status axis really travels. Drop `hide_removed` and this row walks
        # straight into the drill-down.
        add("gijon_removed", "GIJON", ids["live"], listing_status="removed")

        add("aviles_live", "Avilés", ids["live"])
        add("aviles_live_fav", "Avilés", ids["live"], is_favorite=True)
        add("aviles_orphan_gone", "Avilés", None, listing_status="sold")

        db.session.commit()
        return ids


def _municipality_rows(client, query=""):
    """{name: (claimed listings, drill-down href)} off the rendered page."""
    response = client.get(f"/municipalities{query}", follow_redirects=False)
    # A template error is a redirect with a flash, and the page it lands on
    # also has no municipality rows -- so the status is asserted before
    # anything is parsed out of the body.
    assert response.status_code == 200, f"/municipalities{query} did not render"
    body = response.get_data(as_text=True)

    rows = {}
    for match in _ROW.finditer(body):
        link = _LINK.search(match.group("body"))
        if not link:
            continue
        cells = _END_CELL.findall(match.group("body"))
        # Four municipality-fact columns, then the listing count.
        listings = re.sub(r"<[^>]+>", "", cells[4]).strip()
        rows[link.group("name").strip()] = (
            int(listings),
            htmlmod.unescape(link.group("href")),
        )
    assert rows, f"/municipalities{query} rendered no municipality rows"
    return rows


def _properties_total(client, href):
    """The unpaginated total /properties prints for `href`."""
    response = client.get(href, follow_redirects=False)
    assert response.status_code == 200, f"{href} did not render"
    body = response.get_data(as_text=True)
    found = _FOUND.search(body)
    assert found, f"{href} printed no result count"
    return int(found.group(1))


@pytest.mark.parametrize(
    "query",
    ["", "?archived=on", "?favorites=on", "?archived=on&favorites=on"],
)
def test_every_drill_down_opens_the_rows_its_row_counted(client, inventory, query):
    """The contract itself, in all four states the page can be read in."""
    rows = _municipality_rows(client, query)
    for name, (claimed, href) in rows.items():
        assert _properties_total(client, href) == claimed, (
            f"{name} at /municipalities{query}: row claims {claimed}, "
            f"{href} shows something else"
        )


def test_the_link_names_the_retired_and_hidden_subscriptions(client, inventory):
    """`profile_id=all` cannot reach either, and both carry listings here."""
    _, href = _municipality_rows(client)["Gijón"]
    for kind in ("live", "retired", "hidden"):
        assert f"profile_id={inventory[kind]}" in href, (
            f"the {kind} subscription is missing from {href}"
        )
    assert "profile_id=all" not in href


def test_the_link_names_only_the_subscriptions_that_contributed(client, inventory):
    """The other direction, and the reason the scope is read off the rows.

    A second query -- "which subscriptions exist", "which hold listings" --
    would name subscriptions this municipality has nothing in. It would not
    change the count here, which is exactly why it has to be asserted
    separately: a link naming five subscriptions for a municipality carried by
    one ticks four boxes the owner never chose, and the next Apply would
    widen the view. Issue #417 is what a second query costs.
    """
    _, href = _municipality_rows(client)["Avilés"]
    assert f"profile_id={inventory['live']}" in href
    for absent in ("retired", "hidden"):
        assert f"profile_id={inventory[absent]}" not in href, (
            f"the {absent} subscription holds nothing in Avilés but {href} names it"
        )


def test_unassigned_travels_only_when_it_contributed(client, inventory):
    """Two sides, because either one alone is satisfied by a constant.

    Gijón holds a live unassigned listing, so the sentinel has to be there.
    Avilés holds one too, but it is delisted and therefore outside the default
    aggregate -- so at the default the sentinel must be absent, and under
    `archived=on`, where that row is counted, it must appear.
    """
    default_rows = _municipality_rows(client)
    assert "profile_id=unassigned" in default_rows["Gijón"][1]
    assert "profile_id=unassigned" not in default_rows["Avilés"][1]

    archived_rows = _municipality_rows(client, "?archived=on")
    assert "profile_id=unassigned" in archived_rows["Avilés"][1]


def test_the_archive_axis_is_inverted_rather_than_copied(client, inventory):
    """`archived` on one page is `hide_removed` off on the other."""
    default_claim, default_href = _municipality_rows(client)["Gijón"]
    archived_claim, archived_href = _municipality_rows(client, "?archived=on")["Gijón"]

    assert "hide_removed=on" in default_href
    assert "hide_removed" not in archived_href
    # And the difference is a real row, so the two links cannot be the same
    # link wearing two names.
    assert archived_claim == default_claim + 1


def test_favorites_travels_with_the_link(client, inventory):
    favorite_claim, favorite_href = _municipality_rows(client, "?favorites=on")["Gijón"]
    assert "favorites=on" in favorite_href
    assert favorite_claim == 2


def test_drill_down_args_omits_the_switches_it_does_not_set():
    """The absent-means-off spelling /properties itself serialises.

    `base_args` in templates/properties.html drops `hide_removed` and
    `favorites` when they are off, so a link that spelled them `off` would be
    a second spelling of a state the page already round-trips.
    """
    from services.municipality_comparison_service import drill_down_args

    row = {"name": "Gijón", "profile_ids": (6, 8), "unassigned": 0}

    on = drill_down_args(row, favorites_only=True, hide_removed=True)
    assert on == {
        "municipality": "Gijón",
        "profile_id": [6, 8],
        "favorites": "on",
        "hide_removed": "on",
    }

    off = drill_down_args(row, favorites_only=False, hide_removed=False)
    assert off["favorites"] is None
    assert off["hide_removed"] is None
