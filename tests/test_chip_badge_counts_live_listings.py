"""The subscription chip badge counts live listings, not everything (#470).

#469 disclosed the filter bar's contribution to "the chip says one number,
the page says another"; what survived its adversarial review, reproduced, was
the narrower residual gap: the badge came from a bare group-by that counted removed
and sold listings, while the count line's baseline -- Hide removed is on by
default -- did not, so a chip said 4 over a page honestly saying 3. Owner
decision 2026-08-21: the badge does not count the delisted rows.

The exclusion is the same expression the Hide removed switch applies, and it
is deliberately unconditional -- the badge answers "how many live listings
does this subscription hold", the way the portal's own saved search would.
Pinned here, numbers by value (a badge asserting only its presence is
satisfied by any number -- the cadastre-card lesson):

* the chip badge and the menu count, which render the same `option.count`
  twice and must agree;
* the hidden-subscription note, which reads the same helper;
* the #470 reproduction itself, under the rule that followed it (closing
  audit 2026-09-01): the chip's link keeps the page's filters, so the badge
  is the narrowing note's NUMERATOR -- the page its own link opens -- and
  the baseline is the denominator. Chip 4 over "2 of 3 shown" became 3, and
  is now the 2.
"""

import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        norte = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        ghost = SearchProfile(
            name="Ghost",
            is_active=True,
            is_hidden=True,
            travel_targets={"presets": {}, "custom": []},
        )
        # Active, and every listing delisted: the case the #472 review
        # reproduced -- its rows still render with Hide removed off, so its
        # controls must not vanish with its live count.
        allsold = SearchProfile(
            name="AllSold",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([norte, ghost, allsold])
        db.session.commit()
        rows = []
        for slug, status, sea_view in [
            ("live-sea", "active", "yes"),
            ("live-plain", "active", None),
            ("live-likely", "active", "likely"),
            ("gone", "removed", "yes"),
        ]:
            rows.append(
                Property(
                    source_email_id=slug,
                    title=f"Plot {slug}",
                    municipality="Cudillero",
                    search_profile_id=norte.id,
                    listing_status=status,
                    enrichment=(
                        {"environment": {"sea_view": sea_view}} if sea_view else None
                    ),
                )
            )
        for slug, status in [
            ("allsold-1", "sold"),
            ("allsold-2", "removed"),
        ]:
            rows.append(
                Property(
                    source_email_id=slug,
                    title=f"Plot {slug}",
                    municipality="Cudillero",
                    search_profile_id=allsold.id,
                    listing_status=status,
                )
            )
        for slug, status in [
            ("ghost-live-1", "active"),
            ("ghost-live-2", "active"),
            ("ghost-gone", "sold"),
        ]:
            rows.append(
                Property(
                    source_email_id=slug,
                    title=f"Plot {slug}",
                    municipality="Cudillero",
                    search_profile_id=ghost.id,
                    listing_status=status,
                )
            )
        db.session.add_all(rows)
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _rendered(response):
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "An error occurred while loading properties" not in body
    return body


def _chip_badge(body, name):
    match = re.search(
        r'<span class="properties-subscription-name">'
        + re.escape(name)
        + r'</span>\s*<span class="badge[^"]*">(\d+)</span>',
        body,
    )
    assert match, f"no chip badge found for {name}"
    return int(match.group(1))


class TestTheBadgeIsALiveCount:
    def test_the_chip_badge_leaves_the_removed_row_out(self, client):
        body = _rendered(client.get("/properties", query_string={"profile_id": "all"}))
        # 4 rows in the subscription, one of them removed.
        assert _chip_badge(body, "Land at Norte") == 3

    def test_the_menu_count_agrees_with_the_chip(self, client, app):
        with app.app_context():
            norte_id = SearchProfile.query.filter_by(name="Land at Norte").one().id
        body = _rendered(client.get("/properties", query_string={"profile_id": "all"}))
        # The same option.count, rendered a second time as the menu label.
        label = re.search(
            rf'for="profile-option-{norte_id}">\s*Land at Norte\s*'
            r'<span class="text-body-secondary">\((\d+)\)</span>',
            body,
        )
        assert label, "no menu label for the subscription"
        assert int(label.group(1)) == _chip_badge(body, "Land at Norte")

    def test_the_hidden_note_counts_live_listings_only(self, client):
        body = _rendered(client.get("/properties", query_string={"profile_id": "all"}))
        note = re.search(
            r'id="hidden-subscriptions-note".*?</div>', body, flags=re.DOTALL
        )
        assert note, "the hidden-subscription note is missing"
        # Ghost holds 3 rows, one of them sold.
        assert "1 hidden subscription" in note.group(0)
        assert "2 listings" in note.group(0)


class TestAnAllDelistedSubscriptionKeepsItsControls:
    """The #472 review's own reproduction, inverted into a guard.

    `profile_id=all` renders an active subscription's rows whatever their
    status the moment Hide removed is off, so the chip and the checkbox are
    offered on "holds anything at all" and display the live count -- a badge
    of 0 under the default switch is the honest reading, not a reason to
    drop the control that reaches those rows.
    """

    def test_the_chip_stays_with_an_honest_zero_badge(self, client):
        body = _rendered(client.get("/properties", query_string={"profile_id": "all"}))
        assert _chip_badge(body, "AllSold") == 0

    def test_the_menu_checkbox_stays_too(self, client, app):
        with app.app_context():
            allsold_id = SearchProfile.query.filter_by(name="AllSold").one().id
        body = _rendered(client.get("/properties", query_string={"profile_id": "all"}))
        assert f'id="profile-option-{allsold_id}"' in body

    def test_its_rows_and_its_chip_share_the_widened_page(self, client):
        # `mode` marks the request as the filter form's, so an absent
        # hide_removed reads as the box unticked and the delisted rows render.
        body = _rendered(
            client.get(
                "/properties",
                query_string={"profile_id": "all", "mode": "combined"},
            )
        )
        assert "Plot allsold-1" in body
        assert _chip_badge(body, "AllSold") == 0


class TestTheSeventyReproductionIsGone:
    def test_the_badge_is_the_numerator_and_the_baseline_the_denominator(
        self, client, app
    ):
        """#470's own scenario: chip 4 over 'Filters: 2 of 3 shown'. The
        delisted row is still left out (3, not 4); and since the chip's link
        keeps `sea_view=likely`, the badge is what that link opens: 2."""
        with app.app_context():
            norte_id = SearchProfile.query.filter_by(name="Land at Norte").one().id
        body = _rendered(
            client.get(
                "/properties",
                query_string={"profile_id": norte_id, "sea_view": "likely"},
            )
        )
        badge = _chip_badge(body, "Land at Norte")
        note = re.search(r"Filters: (\d+) of (\d+) shown", body)
        assert note, "the narrowing note is missing"
        assert int(note.group(1)) == badge == 2, (
            "the badge is the page its own link opens, filters kept"
        )
        assert int(note.group(2)) == 3, "the baseline: live rows before the bar"
