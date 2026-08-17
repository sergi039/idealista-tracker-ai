"""The AI prompt compares like with like, and says so (issue #386).

#378 measured that price per m² collapses as a plot grows — Spearman -0.842
over 459 production plots, a factor of 27 between the smallest and largest
band — and #383 banded the *scorer* accordingly. The prompt was the second
consumer of the word "peer" and kept its own unbanded pool, so property 351
(Salamir, 1,300 m², €46/m²) was handed a "local peer average" of €26/m²
carried by two four-thousand-square-metre parcels, and both providers returned
`OVERPRICED` while the page's own Value component, over comparables of its
size, put the same listing just below the median at 52.6/100.

The comparables were worse than the average: `ORDER BY score_total DESC` and
`size_score` is a component of that score, so the three shown were always the
largest — the cheapest per m². Claude called a 1,697 m² parcel "the smallest
comparable (similar size)" while a 1,271 m² peer sat unshown in the same table.

What is pinned here: the pool is the scorer's own, comparables are chosen by
size proximity, and when no comparable-size peers exist the prompt says the
average spans mixed sizes rather than letting the model read it as the price
of a listing like this one.
"""

from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_ai_service import PropertyAIService
from services.property_scoring_service import LandPropertyScorer
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
        name="Land at Norte",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(prof)
    db.session.commit()
    return prof


def _land(profile, key, area, ppm2, score=None, municipality="Cudillero"):
    prop = Property(
        source_email_id=f"issue_386_{key}",
        title=f"Land plot in {municipality}",
        property_category="land",
        property_subtype="plot",
        municipality=municipality,
        search_profile_id=profile.id,
        area=Decimal(str(area)),
        price=Decimal(str(round(area * ppm2, 2))),
        score_total=None if score is None else Decimal(str(score)),
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _property_351(profile):
    """Production's shape, at production's numbers.

    The subject is 1,300 m² at €46.2/m². Four Cudillero plots of a comparable
    size straddle it (28.7 / 40.7 / 51.1 / 65.4 €/m², median 45.9); the two big
    parcels that dominated the old average are 4,107 and 4,217 m² at €23.9 and
    €22.5, and they carry the highest scores in the table — which is what put
    them, and not the comparable plots, in front of the model.
    """
    subject = _land(profile, "subject", 1_300, 46.153846, score=35.7)
    in_band = [
        _land(profile, "band_247", 1_271, 28.7, score=38.4),
        _land(profile, "band_578", 1_400, 40.7, score=47.5),
        _land(profile, "band_377", 1_350, 51.1, score=49.0),
        _land(profile, "band_610", 1_300, 65.4, score=31.6),
    ]
    out_of_band = [
        _land(profile, "big_337", 4_107, 23.9, score=62.6),
        _land(profile, "big_343", 4_217, 22.5, score=70.9),
        _land(profile, "mid_292", 1_697, 28.9, score=56.5),
    ]
    return subject, in_band, out_of_band


def _prompt_for(prop):
    prompt, _schema = PropertyAIService()._build_prompt(prop)
    return prompt


class TestThePoolIsComparableInSize:
    def test_the_average_is_taken_over_comparable_sizes(self, app, profile):
        with app.app_context():
            subject, in_band, _ = _property_351(profile)
            service = PropertyAIService()

            peers, meta = service._collect_peers(subject)
            snapshot = service._build_market_snapshot(subject, peers, meta)

            assert snapshot["status"] == "ok"
            assert snapshot["size_comparable"] is True
            assert snapshot["comparable_scope"].endswith("+area_band")
            assert snapshot["area_band_m2"] == [1040.0, 1625.0]
            assert {p.id for p in peers} == {p.id for p in in_band}
            assert snapshot["sample_size"] == 4
            # (28.7 + 40.7 + 51.1 + 65.4) / 4
            assert snapshot["avg_price_per_m2"] == pytest.approx(46.475, abs=0.01)

    def test_the_old_pool_is_what_called_it_overpriced(self, app, profile):
        """The defect in the same numbers: €46/m² against parcels three times the size."""
        with app.app_context():
            subject, _, _ = _property_351(profile)
            service = PropertyAIService()

            peers, meta = service._collect_peers(subject)
            banded = service._build_market_snapshot(subject, peers, meta)

            every_size = [
                float(p.price) / float(p.area)
                for p in Property.query.filter(Property.id != subject.id).all()
            ]
            unbanded_avg = sum(every_size) / len(every_size)

            subject_ppm2 = float(subject.price) / float(subject.area)
            # Unbanded, the subject looks 24% dearer than "the neighbours";
            # against comparables of its size it is a shade *below* them.
            assert unbanded_avg == pytest.approx(37.31, abs=0.01)
            assert subject_ppm2 / unbanded_avg > 1.2
            assert subject_ppm2 / banded["avg_price_per_m2"] < 1.0

    def test_the_prompt_and_the_page_describe_one_pool(self, app, profile):
        """A snapshot the Value bar disagrees with is worse than either number."""
        with app.app_context():
            subject, _, _ = _property_351(profile)

            _score, value_meta = LandPropertyScorer()._value_score(subject)
            peers, meta = PropertyAIService()._collect_peers(subject)
            snapshot = PropertyAIService()._build_market_snapshot(subject, peers, meta)

            assert snapshot["comparable_scope"] == value_meta["comparable_scope"]
            assert snapshot["sample_size"] == value_meta["peer_count"]
            assert snapshot["area_band_m2"] == value_meta["area_band_m2"]


class TestTheComparablesAreTheNearestInSize:
    def test_the_biggest_plots_no_longer_win_on_score(self, app, profile):
        with app.app_context():
            subject, in_band, out_of_band = _property_351(profile)
            by_key = {p.source_email_id.replace("issue_386_", ""): p for p in in_band}
            service = PropertyAIService()

            peers, _meta = service._collect_peers(subject)
            similar = service._build_similar_properties(subject, peers)

            # Nearest in size first: 1,300 (0) -> 1,271 (29) -> 1,350 (50).
            # 1,400 m² is the fourth and drops off; the 1,697 m² plot and the
            # two four-thousanders are out of the band whatever they score,
            # and they are exactly the three the old `score_total` order chose.
            assert [s["id"] for s in similar] == [
                by_key["band_610"].id,
                by_key["band_247"].id,
                by_key["band_377"].id,
            ]
            assert [s["area"] for s in similar] == [1300.0, 1271.0, 1350.0]
            assert not ({p.id for p in out_of_band} & {s["id"] for s in similar})

    def test_a_peer_nobody_scored_is_not_reported_as_zero(self, app, profile):
        """`Score: 0.0/100` invented out of a NULL is a judgement, not a number."""
        with app.app_context():
            subject = _land(profile, "subject", 1_300, 46.2, score=35.7)
            for i in range(3):
                _land(profile, f"unscored_{i}", 1_290 + i * 10, 40 + i, score=None)

            prompt = _prompt_for(subject)

            assert "not scored" in prompt
            assert "Score: 0.0/100" not in prompt


class TestThePromptStatesItsBasis:
    def test_a_banded_pool_names_the_band(self, app, profile):
        with app.app_context():
            subject, _, _ = _property_351(profile)

            prompt = _prompt_for(subject)

            assert "peers of a comparable size (1,040-1,625 m²)" in prompt
            assert "MIXED SIZES" not in prompt

    def test_giving_the_band_up_is_said_out_loud(self, app, profile):
        """No comparable-size peers is a different claim, and must read as one."""
        with app.app_context():
            subject = _land(profile, "subject", 1_300, 46.2, score=35.7)
            for i in range(4):  # nothing remotely this size
                _land(profile, f"huge_{i}", 40_000 + i * 100, 8 + i * 0.1)

            service = PropertyAIService()
            peers, meta = service._collect_peers(subject)
            snapshot = service._build_market_snapshot(subject, peers, meta)
            prompt = _prompt_for(subject)

            assert snapshot["size_comparable"] is False
            assert "area_band_m2" not in snapshot
            assert "MIXED SIZES" in prompt
            assert "Price per m² falls steeply as area grows" in prompt
            assert "do not treat a gap against it as evidence" in prompt
