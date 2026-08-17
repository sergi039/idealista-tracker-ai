"""Value is ranked against comparables of a comparable size (issue #378).

Price per m² falls as a plot grows, so ranking a listing against every size at
once makes `value_score` a second reading of `size_score`. Measured on
production 2026-08-17, as rank correlations — which is what these components
are, not an approximation of them:

    land     n=319   corr(area, value%) = +0.702
    housing  n= 87   corr(area, value%) = +0.779

Neither profile double-counts on its own: land carries value only in the
investment weights and size only in the lifestyle ones. But the combined score
is the saved mix (#257), and at the default 0.32/0.68 those two channels drive
46% of the number `/properties` sorts by — so a small plot was marked down
twice for one fact.

What is pinned here is the band and its retreat: a comparable size is given up
only after every geographic scope has been tried at that size, the scope name
says which happened, and a listing with no comparable-size peers still gets a
score rather than a hole.
"""

from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_scoring_service import (
    PEER_AREA_BAND_FACTOR,
    LandPropertyScorer,
)
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def profile(app):
    prof = SearchProfile(
        name="Bands",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(prof)
    db.session.commit()
    return prof


def _land(profile, key, area, ppm2, municipality="Carreño", subtype="plot"):
    prop = Property(
        source_email_id=f"issue_378_{key}",
        title=f"Land in {municipality}",
        property_category="land",
        property_subtype=subtype,
        municipality=municipality,
        search_profile_id=profile.id,
        area=Decimal(str(area)),
        price=Decimal(str(round(area * ppm2, 2))),
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _value(prop):
    """(score, meta) from the land scorer, the way `calculate` calls it."""
    return LandPropertyScorer()._value_score(prop)


def _mixed_set(profile):
    """The production shape: an ordinary €/m² for its size, among big cheap parcels.

    Four comparables at 40/55/85/100 €/m² put the subject's 70 exactly
    mid-pack; twelve 40,000 m² parcels at 8-9 €/m² are what the old scorer
    compared it with instead.
    """
    subject = _land(profile, "subject", 1_000, 70)
    for i, ppm2 in enumerate((40, 55, 85, 100)):
        _land(profile, f"peer_small_{i}", 1_000 + i * 10, ppm2)
    for i in range(12):
        _land(profile, f"peer_big_{i}", 40_000 + i * 100, 8 + i * 0.1)
    return subject


class TestTheBandChangesTheVerdict:
    def test_a_small_plot_is_judged_against_small_plots(self, app, profile):
        with app.app_context():
            subject = _mixed_set(profile)

            score, meta = _value(subject)

            assert meta["comparable_scope"].endswith("+area_band")
            assert meta["peer_count"] == 4
            # Two of the four comparables are cheaper per m², two dearer.
            assert score == pytest.approx(50.0)

    def test_the_same_plot_was_near_the_bottom_without_the_band(self, app, profile):
        """The defect, in the same numbers: it was not the price that sank it."""
        with app.app_context():
            subject = _mixed_set(profile)
            banded, _ = _value(subject)

            # What the old scorer did: rank against every size at once.
            everything = sorted(
                float(p.price) / float(p.area)
                for p in Property.query.filter(Property.id != subject.id).all()
            )
            count_le = sum(1 for v in everything if v <= 70.0)
            unbanded = (1.0 - count_le / len(everything)) * 100.0

            assert unbanded == pytest.approx(12.5)
            assert banded == pytest.approx(50.0)


class TestHowTheBandRetreats:
    def test_geography_is_given_up_before_size(self, app, profile):
        """A comparable size elsewhere beats any size next door."""
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70, municipality="Carreño")
            for i in range(5):  # same size, other municipality
                _land(profile, f"far_{i}", 1_000, 65 + i, municipality="Gozón")
            for i in range(5):  # same municipality, nothing like the same size
                _land(profile, f"near_{i}", 50_000, 9, municipality="Carreño")

            _, meta = _value(subject)

            assert meta["comparable_scope"] == "subtype+area_band"
            assert meta["peer_count"] == 5

    def test_a_size_with_no_comparables_still_scores(self, app, profile):
        """The fallback exists to produce a score, and says it was used."""
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            for i in range(5):
                _land(profile, f"big_{i}", 60_000, 9)

            score, meta = _value(subject)

            assert score is not None
            assert "+area_band" not in meta["comparable_scope"]
            assert meta["status"] == "ok"

    def test_a_listing_with_no_area_is_unaffected(self, app, profile):
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            subject.area = None
            db.session.commit()

            score, meta = _value(subject)

            assert score is None
            assert meta["status"] == "missing_price_or_area"


class TestTheBandItself:
    def test_the_bounds_are_recorded_on_the_row(self, app, profile):
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            for i in range(4):
                _land(profile, f"peer_{i}", 1_000, 60 + i)

            _, meta = _value(subject)

            low, high = meta["area_band_m2"]
            assert low == pytest.approx(1_000 / PEER_AREA_BAND_FACTOR, abs=0.1)
            assert high == pytest.approx(1_000 * PEER_AREA_BAND_FACTOR, abs=0.1)

    def test_twice_the_area_is_not_a_comparable(self, app, profile):
        """Pins the width, not just the existence, of the band.

        A factor loose enough to swallow a plot twice the size gives the
        confound straight back — 2x was the median gap between a plot and the
        parcels that used to outvote it.
        """
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            for i in range(4):
                _land(profile, f"double_{i}", 2_000, 20 + i)

            score, meta = _value(subject)

            assert "+area_band" not in meta["comparable_scope"]
            assert score is not None  # the fallback still answers

    def test_a_thin_band_does_not_beat_a_full_wide_set(self, app, profile):
        """Two comparables are not a ranking; the fallback is preferred to them.

        `min_peers` is the whole reason the wide ladder still exists, so a
        banded scope below it must lose to a scope that clears it.
        """
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            for i in range(2):  # in band, but below min_peers
                _land(profile, f"thin_{i}", 1_050, 60 + i)
            for i in range(5):  # out of band, and enough of them
                _land(profile, f"wide_{i}", 30_000, 9 + i)

            score, meta = _value(subject)

            assert "+area_band" not in meta["comparable_scope"]
            assert meta["peer_count"] == 7
            assert score is not None

    def test_the_edge_of_the_band_is_inside_it(self, app, profile):
        """Inclusive bounds: exactly `factor` away is still a comparable."""
        with app.app_context():
            subject = _land(profile, "subject", 1_000, 70)
            edge = 1_000 * PEER_AREA_BAND_FACTOR
            for i in range(3):
                _land(profile, f"edge_{i}", edge, 60 + i)
            for i in range(3):
                _land(profile, f"outside_{i}", edge * 1.01, 5)

            score, meta = _value(subject)

            assert meta["comparable_scope"].endswith("+area_band")
            assert meta["peer_count"] == 3
            assert score is not None
