"""The weight preview counts what the save will store (#453 review).

`confirm_pool_scoring` exists so that raising a weightless criterion above 0
cannot silently re-score the table: it stages the config, rescores in the
session, counts the rows that move, rolls back, and asks. A preview that
under-reports is therefore worse than no preview at all, and CLAUDE.md says so
in as many words.

It under-reported. `Property.score_*` are `Numeric(5, 2)`, so a score is kept
to the cent — and the loop asked `abs(float(new) - float(old)) >= 0.05`.
Reproduced 2026-08-20 with four land listings, one measured hazard and
`hazard_score = 0.0001`: **"0 of 4 listings would change score"**, and the
confirm wrote `33.32` over `33.33` on all four.

The threshold did not even hold at its own boundary. Both sides arrive as
`Decimal`, and `float(Decimal("50.05")) - float(Decimal("50.00"))` is
0.04999999999999716 — *not* `>= 0.05`. So the one value the number named was
the one value it got wrong.
"""

from decimal import Decimal

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.property_scoring_service import PropertyScoringService  # noqa: E402


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


class TestStoredScore:
    """`stored_score` is the whole rule: what the column will keep."""

    def test_a_cent_is_a_difference(self):
        stored = PropertyScoringService.stored_score
        assert stored(Decimal("33.33")) != stored(Decimal("33.32"))

    def test_the_old_threshold_would_have_called_that_no_change(self):
        """The control, so this file states the defect rather than only the
        fix. Without it a reader cannot tell which of the two numbers moved."""
        assert abs(float(Decimal("33.33")) - float(Decimal("33.32"))) < 0.05

    def test_it_does_not_go_through_float_at_its_own_boundary(self):
        """0.05 was the stated threshold and 0.05 did not reach it."""
        assert abs(float(Decimal("50.05")) - float(Decimal("50.00"))) < 0.05
        stored = PropertyScoringService.stored_score
        assert stored(Decimal("50.05")) != stored(Decimal("50.00"))

    def test_noise_below_the_column_is_not_a_difference(self):
        """The other direction matters too: a preview that counts a row the
        save does not change is a wrong number in the reassuring direction."""
        stored = PropertyScoringService.stored_score
        assert stored(Decimal("33.331")) == stored(Decimal("33.334"))
        assert stored(50.0) == stored(Decimal("50.00"))

    def test_absence_stays_absent(self):
        stored = PropertyScoringService.stored_score
        assert stored(None) is None
        assert stored(None) != stored(Decimal("0.00"))

    @pytest.mark.parametrize(
        "value,stored_by_postgresql",
        [
            ("0.005", "0.01"),
            ("0.015", "0.02"),
            ("0.025", "0.03"),
            ("0.065", "0.07"),
            ("0.075", "0.08"),
            ("33.335", "33.34"),
            ("-0.005", "-0.01"),
        ],
    )
    def test_it_breaks_ties_the_way_the_database_does(
        self, value, stored_by_postgresql
    ):
        """Measured on the deployment's own PostgreSQL, not chosen.

        `SELECT 0.005::numeric(5,2)` there is `0.01` — half **away from
        zero**. `Decimal`'s default is `ROUND_HALF_EVEN`, which makes it
        `0.00`, and `round(float(...), 2)` gets that one right and `0.015`
        wrong. A helper that claims to say what the database keeps has to
        break ties the database's way, and the first version of this one did
        not; these seven pairs are the psql output.
        """
        assert str(PropertyScoringService.stored_score(Decimal(value))) == (
            stored_by_postgresql
        )

    def test_it_reads_its_precision_from_the_column(self):
        """Written once, in the schema. A helper carrying its own `0.01` is
        one that stays behind when the column moves."""
        scale = Property.__table__.c.score_total.type.scale
        assert scale == 2
        smallest = Decimal(1).scaleb(-scale)
        stored = PropertyScoringService.stored_score
        assert stored(Decimal("10") + smallest) != stored(Decimal("10"))
        assert stored(Decimal("10") + smallest / 10) == stored(Decimal("10"))


class TestThePreviewSaysWhatTheConfirmDoes:
    """End to end, through the route the owner presses."""

    def _profile_with_listings(self, app):
        from models import SearchProfile

        profile = SearchProfile(name="Preview", is_active=True)
        db.session.add(profile)
        db.session.commit()
        for index in range(4):
            db.session.add(
                Property(
                    source_email_id=f"prev-{index}",
                    title="Plot",
                    search_profile_id=profile.id,
                    property_category="land",
                    score_investment=Decimal("33.33"),
                    score_lifestyle=Decimal("33.33"),
                    score_total=Decimal("33.33"),
                )
            )
        db.session.commit()
        return profile

    def test_a_one_cent_shift_is_reported_rather_than_swallowed(self, app, monkeypatch):
        """The reproduced case. The preview must not say "0 would change" for
        a save that writes a different number to every row."""
        profile = self._profile_with_listings(app)

        def _rescore(self, prop, commit=True):
            # Exactly one cent, on the number the list sorts by.
            prop.score_total = Decimal("33.32")
            return True

        monkeypatch.setattr(PropertyScoringService, "calculate_for_property", _rescore)

        client = app.test_client()
        response = client.post(
            f"/profiles/{profile.id}/edit",
            data={
                "action": "save_scoring_weights",
                "name": profile.name,
                "scoring__land__lifestyle__hazard_score": "0.0001",
            },
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        assert "0 of 4 listings would change" not in body, body[:400]
        assert "4 of 4 listings would change" in body, body[:400]
        # And the shift beside the count is reported at the same precision the
        # count is taken at: "4 of 4 would change (mean total shift +0.0)" is
        # the two halves of one sentence contradicting each other.
        assert "mean total shift -0.01" in body, body[:400]
