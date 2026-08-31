"""Two links that claimed something false about the criteria filter.

Both were found by adversarial verification against production on
2026-08-31, and both are the same defect the #445 family keeps producing: a
recovery control that re-issues the very narrowing it offers to lift.

`criteria` is the first filter on this page whose ABSENCE still filters —
unset means "hide the measured fails" — so the inverted clear-list in
`utils/listing_filters.NON_FILTERS`, which is correct for every ordinary
filter, silently dropped it and landed the reader back where they started.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}


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


_SEQ = iter(range(1, 10_000))


def _mk(profile_id, **overrides):
    values = dict(
        source_email_id=f"recov:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=100000,
        search_profile_id=profile_id,
        listing_status="active",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


@pytest.fixture
def failing_row(app):
    """One listing hidden by the criteria default, with coordinates so the
    map can plot it once the hide is actually lifted."""
    profile = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(profile)
    db.session.commit()
    hidden = _mk(
        profile.id,
        title="Hidden by criteria",
        area=100,
        area_type="built",
        location_lat=43.3,
        location_lon=-8.8,
    )
    _mk(
        profile.id,
        title="Ordinary row",
        area=200,
        area_type="built",
        location_lat=43.4,
        location_lon=-8.9,
    )
    # A second passing row, so the DEFAULT view holds two and a search for
    # one of them is a real narrowing — without it the control test below
    # asserts against a page that had nothing to disclose.
    _mk(
        profile.id,
        title="Another passing row",
        area=250,
        area_type="built",
        location_lat=43.5,
        location_lon=-8.7,
    )
    return hidden


class TestTheMapRecoveryLinkActuallyRecovers:
    def test_clearing_shows_the_criteria_hidden_listing(self, client, failing_row):
        """The production reproduction: the notice offered "Clear the filters
        and show it" and the cleared link rendered the identical notice with
        the identical link — a closed loop, because dropping `criteria`
        re-issues the default hide."""
        import re

        first = client.get(f"/map?focus={failing_row.id}").data.decode()
        match = re.search(r'id="map-focus-notice".{0,400}?href="([^"]+)"', first, re.S)
        assert match, "the notice must render for a criteria-hidden listing"
        link = match.group(1).replace("&amp;", "&")

        # The whole promise of that link: following it shows the listing.
        cleared = client.get(link).data.decode()
        assert str(failing_row.id) in cleared
        assert "map-focus-notice" not in cleared, (
            f"following {link!r} returned the notice again — the loop is back"
        )

    def test_the_cleared_link_says_criteria_out_loud(self, client, failing_row):
        """Not an implementation detail: `criteria` absent means "default",
        so the link has to carry `criteria=all` rather than omit it."""
        import re

        body = client.get(f"/map?focus={failing_row.id}").data.decode()
        match = re.search(r'id="map-focus-notice".{0,400}?href="([^"]+)"', body, re.S)
        link = match.group(1).replace("&amp;", "&")
        assert "criteria=all" in link, link


class TestTheNarrowingNoteDoesNotClaimASubsetItHasNot:
    def test_no_subset_claim_under_an_explicit_criteria_mode(self, client, failing_row):
        """`?criteria=fail&search=…` showed "54 of 377" on production while
        none of the 54 was among the 377. The line asserts N ⊆ M, so where
        the sets are disjoint it must not be drawn."""
        body = client.get(
            f"/properties?criteria=fail&search={failing_row.title[:6]}"
        ).data.decode()
        assert "Hidden by criteria" in body, "the fail mode must show the row"
        assert "filter-bar-narrowing-note" not in body

    def test_a_subset_mode_keeps_the_line(self, client, failing_row):
        """The guard is narrow on purpose: a `pass`/`unknown` row DOES
        survive the default reading the clear link restores, so there the
        sentence is true and suppressing it would lose a real disclosure.
        Only `fail` and `all` put rows on screen that the default hides."""
        body = client.get("/properties?criteria=unknown&search=Ordinary").data.decode()
        assert "Ordinary row" in body
        assert "filter-bar-narrowing-note" in body

    def test_the_note_still_renders_under_the_default_reading(
        self, client, failing_row
    ):
        """It is suppressed only where it cannot back its arithmetic: with
        criteria at its default the on-screen rows really are a subset."""
        body = client.get("/properties?search=Ordinary").data.decode()
        assert "Ordinary row" in body
        assert "filter-bar-narrowing-note" in body
