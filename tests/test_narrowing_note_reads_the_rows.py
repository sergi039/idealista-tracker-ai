"""The narrowing note asks its question of the rows, never of the parameter.

#508 suppressed "Filters: N of M shown" under `criteria` in {all, fail},
because M is counted under the default reading the clear link restores and
those two modes can put rows on screen that the default hides. The mode is a
proxy for that, and adversarial verification against production found it wrong
in both directions:

* Subscription 6 ("Land at Norte") carries NO criteria, so `criteria=all`
  selects exactly the default's own 144 rows. The note said "15 of 144" —
  true — and #508 silenced it, taking the clear-filters link with it, since
  that link lives inside the note's own span.
* The mirror the string test cannot express at all: `criteria=fail` over a
  scope whose failing rows the owner has already judged. A judged row is
  never hidden by default (the #502 rule), so those rows ARE in M and the
  sentence is true.

Both are one measurement — does anything on screen fall outside what the
clear link restores — which is what `_shows_rows_the_default_hides` asks.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}
NOTE = "filter-bar-narrowing-note"
CLEAR = "clear-filters-link"


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
        source_email_id=f"note:{next(_SEQ)}",
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


class TestASubscriptionWithNoCriteriaNestsUnderEveryMode:
    """The production regression, at its own scale."""

    @pytest.fixture
    def bare(self, app):
        profile = SearchProfile(name="Land at Norte", is_active=True)
        db.session.add(profile)
        db.session.commit()
        # Two rows the search narrows to one. Both are far below the bounds
        # that WOULD apply if this subscription carried any -- the point is
        # that it does not, so no mode can move a row.
        _mk(profile.id, title="Narrow me", area=100, area_type="built")
        _mk(profile.id, title="Other row", area=100, area_type="built")
        return profile

    def test_criteria_all_keeps_a_note_that_was_true(self, client, bare):
        body = client.get(
            f"/properties?profile_id={bare.id}&criteria=all&search=Narrow"
        ).data.decode()
        assert "Narrow me" in body, "the page must have rendered"
        assert NOTE in body, (
            "criteria=all selects the default's own rows here — the subset "
            "claim is true and the note must stand"
        )

    def test_and_keeps_the_clear_link_that_lives_inside_it(self, client, bare):
        body = client.get(
            f"/properties?profile_id={bare.id}&criteria=all&search=Narrow"
        ).data.decode()
        assert CLEAR in body

    def test_the_default_reading_is_unchanged(self, client, bare):
        body = client.get(
            f"/properties?profile_id={bare.id}&search=Narrow"
        ).data.decode()
        assert NOTE in body


class TestTheMeasurementSeesWhatAStringTestCannot:
    @pytest.fixture
    def judged_fails(self, app):
        """A subscription WITH criteria whose failing rows are all judged.

        A judged row is never hidden by default, so under `criteria=fail`
        every row on screen is one the clear link's destination also holds.
        """
        profile = SearchProfile(
            name="Galicia · costa", is_active=True, criteria=CRITERIA
        )
        db.session.add(profile)
        db.session.commit()
        favorited = _mk(
            profile.id,
            title="Judged tiny alpha",
            area=100,
            area_type="built",
            is_favorite=True,
        )
        reviewed = _mk(
            profile.id, title="Judged tiny beta", area=100, area_type="built"
        )
        reviewed.owner_verdict = "interested"
        # Two rows that pass, so the default view is wider than the search.
        _mk(profile.id, title="Roomy one", area=200, area_type="built", plot_area=900)
        _mk(profile.id, title="Roomy two", area=210, area_type="built", plot_area=950)
        db.session.commit()
        return {"profile": profile, "favorited": favorited, "reviewed": reviewed}

    def test_fail_mode_over_judged_rows_keeps_the_note(self, client, judged_fails):
        """No string test can reach this case: the mode is `fail`, and the
        sentence is nonetheless true, because a judged row is in M."""
        body = client.get("/properties?criteria=fail&search=Judged").data.decode()
        assert "Judged tiny alpha" in body
        assert NOTE in body, (
            "every failing row here is judged, so none is hidden by default "
            "and all of them are inside what the clear link restores"
        )

    def test_an_unjudged_fail_on_screen_still_stands_the_note_down(
        self, client, judged_fails
    ):
        """The measurement has to keep #508's own case. One unjudged failing
        row on screen is a row outside M, and the claim breaks."""
        _mk(
            judged_fails["profile"].id,
            title="Judged tiny gamma",
            area=100,
            area_type="built",
        )
        body = client.get("/properties?criteria=fail&search=Judged").data.decode()
        assert "Judged tiny gamma" in body
        assert NOTE not in body
