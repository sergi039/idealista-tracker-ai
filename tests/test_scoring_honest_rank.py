"""The list ranks by what is known, not by what happened to be measured.

Two defects, one PR (#377, #379), both measured on the live database on
2026-08-17 (#378, the value/size double count, is fixed separately by making the
value rank size-aware — see that PR):

* **#379** — a criterion nobody could measure scores `None` and the branch
  average renormalises without it, so a listing with two perfect measured
  criteria and three unmeasured ones scored 100 and led `/properties`; not one
  row with four or five measured criteria reached 90. Owner decision: the
  number stays what it is; the payload records the coverage (share of enabled
  weight that answered), the list shows it and can filter to fully measured
  rows, and nothing is invented for a criterion nobody measured.
* **#377** — peers were matched on the raw municipality string, so `Gijón`
  and `Gijon` were strangers; peers now share `municipality_grouping.group_key`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import create_app, db
from models import Property
from services.property_scoring_service import (
    HousingPropertyScorer,
    LandPropertyScorer,
    _coverage,
    score_coverage,
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


def _listing(**kwargs):
    defaults = {
        "source_email_id": f"rank-{kwargs.get('title', 'x')}",
        "property_category": "land",
        "property_subtype": "plot",
        "municipality": "Gijón",
        "price": Decimal("100000"),
        "area": Decimal("1000"),
    }
    defaults.update(kwargs)
    prop = Property(**defaults)
    db.session.add(prop)
    db.session.commit()
    return prop


# --- #379: coverage is shown, never invented ---------------------------------


class TestCoverage:
    def test_coverage_counts_only_enabled_weight(self):
        share, measured, enabled = _coverage(
            {
                "value_score": (100.0, 0.6),
                "travel_score": (None, 0.25),
                "sea_score": (None, 0.15),
                "size_score": (70.0, 0.0),  # weight 0: not enabled, not a hole
                "pool_score": (None, 0.0),
            }
        )
        assert share == pytest.approx(0.6)
        assert (measured, enabled) == (1, 3)


class TestTheScoreIsUntouchedAndTheCoverageIsRecorded:
    """Property 681's shape: two perfect measured criteria, the rest refused.

    Owner decision 2026-08-17: the number stays the measured average -- no
    neutral prior, no penalty -- and the page shows how much of the enabled
    weight it rests on and which criteria are missing.
    """

    def _score(self, monkeypatch, scorer, value, travel, sea, size=None, pool=None):
        monkeypatch.setattr(scorer, "_value_score", lambda p: (value, {"m": 1}))
        monkeypatch.setattr(scorer, "_size_score", lambda p: (size, {"m": 1}))
        monkeypatch.setattr(
            scorer, "_travel_score", lambda p, prof, **kw: (travel, {"m": 1})
        )
        monkeypatch.setattr(scorer, "_sea_score", lambda p, **kw: (sea, {"m": 1}))
        monkeypatch.setattr(scorer, "_pool_score", lambda p, **kw: (pool, {"m": 1}))
        return scorer.calculate(Property(property_category="housing"), None)

    def test_the_measured_average_is_still_the_score(self, app, monkeypatch):
        result = self._score(
            monkeypatch, HousingPropertyScorer(), value=100.0, travel=None, sea=None
        )
        assert result.investment == pytest.approx(100.0)
        assert result.combined == pytest.approx(100.0)

    def test_coverage_is_recorded_weighted_with_the_missing_named(
        self, app, monkeypatch
    ):
        result = self._score(
            monkeypatch, HousingPropertyScorer(), value=100.0, travel=None, sea=None
        )
        cov = result.scoring_payload["coverage"]
        assert cov["investment"] == {
            "share": pytest.approx(0.6),
            "measured": 1,
            "enabled": 3,
        }
        assert cov["lifestyle"]["measured"] == 0
        assert cov["share"] < 0.5  # 0.32*0.6 + 0.68*0
        read = score_coverage(result.scoring_payload)
        assert read["derived"] is False
        assert read["share"] == pytest.approx(cov["share"])
        assert set(read["missing"]) == {"travel_score", "sea_score", "size_score"}

    def test_a_fully_measured_row_reads_full_coverage(self, app, monkeypatch):
        result = self._score(
            monkeypatch,
            HousingPropertyScorer(),
            value=85.0,
            travel=85.0,
            sea=85.0,
            size=85.0,
        )
        read = score_coverage(result.scoring_payload)
        assert read["share"] == pytest.approx(1.0)
        assert read["missing"] == []

    def test_a_payload_written_before_coverage_is_derived_not_assumed(self):
        """The rows already scored gain the field on read, without a rescore."""
        legacy = {
            "profiles": {
                "investment": {
                    "weights": {
                        "value_score": 0.6,
                        "travel_score": 0.25,
                        "sea_score": 0.15,
                    },
                    "components": {
                        "value_score": 90.0,
                        "travel_score": None,
                        "sea_score": None,
                    },
                },
                "lifestyle": {
                    "weights": {
                        "travel_score": 0.45,
                        "size_score": 0.3,
                        "sea_score": 0.25,
                    },
                    "components": {
                        "travel_score": None,
                        "size_score": 80.0,
                        "sea_score": None,
                    },
                },
            },
            "combined_mix": {"investment": 0.32, "lifestyle": 0.68},
        }
        read = score_coverage(legacy)
        assert read["derived"] is True
        assert read["share"] == pytest.approx(0.32 * 0.6 + 0.68 * 0.3)
        assert (read["measured"], read["enabled"]) == (2, 6)
        assert set(read["missing"]) == {"travel_score", "sea_score"}
        assert score_coverage(None) is None
        assert score_coverage({}) is None


# --- #377: peers share a municipality key ------------------------------------


class TestPeersShareTheMunicipalityKey:
    def test_spellings_of_one_municipality_are_peers(self, app):
        subject = _listing(title="gijon-subject", municipality="Gijon")
        for i in range(4):
            _listing(
                title=f"gijon-peer-{i}",
                municipality="Gijón",
                price=Decimal(str(80000 + i * 10000)),
                area=Decimal("900"),
            )
        values, meta = LandPropertyScorer()._collect_peer_ppm2(
            subject, min_peers=3, limit=600
        )
        # #378 appends "+area_band" when the peers are also a comparable size,
        # which these are; the tier is what this test is about.
        assert meta["comparable_scope"].startswith("municipality+subtype")
        assert len(values) == 4

    def test_a_truncated_value_keeps_the_exact_match(self, app):
        """`Ovi...` is nobody's key; folding it into Oviedo by prefix is the
        wrong-pick hazard the grouping module refuses."""
        subject = _listing(title="ovi-subject", municipality="Ovi...")
        for i in range(3):
            _listing(title=f"oviedo-peer-{i}", municipality="Oviedo")
        values, meta = LandPropertyScorer()._collect_peer_ppm2(
            subject, min_peers=3, limit=600
        )
        # A prefix check, not an inequality: since #378 the tier can carry an
        # "+area_band" suffix, and `!=` would pass on
        # "municipality+subtype+area_band" while the tier it denies was used.
        assert not meta["comparable_scope"].startswith("municipality+subtype")


# --- #379 on the page: the hint, the detail line, and the filter -------------


def _scored_listing(profile_id, key, share, components=None):
    """A row with a stored scoring payload carrying `coverage.share`."""
    payload = {
        "profiles": {
            "investment": {
                "weights": {
                    "value_score": 0.6,
                    "travel_score": 0.25,
                    "sea_score": 0.15,
                },
                "components": components
                or {"value_score": 90.0, "travel_score": None, "sea_score": None},
            },
            "lifestyle": {
                "weights": {"travel_score": 0.45, "size_score": 0.3, "sea_score": 0.25},
                "components": {
                    "travel_score": None,
                    "size_score": 80.0,
                    "sea_score": None,
                },
            },
        },
        "combined_mix": {"investment": 0.32, "lifestyle": 0.68},
    }
    if share is not None:
        payload["coverage"] = {"share": share}
    prop = Property(
        source_email_id=key,
        title=key,
        municipality="Cudillero",
        search_profile_id=profile_id,
        listing_status="active",
        property_category="housing",
        property_subtype="house",
        price=Decimal("150000"),
        area=Decimal("120"),
        score_total=Decimal("88.0"),
        score_investment=Decimal("90.0"),
        score_lifestyle=Decimal("80.0"),
        scoring=payload,
    )
    db.session.add(prop)
    db.session.commit()
    return prop.id


@pytest.fixture
def profile(app):
    from models import SearchProfile

    profile = SearchProfile(
        name="Houses",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    return profile.id


class TestThePageShowsCoverageAndCanFilterOnIt:
    def test_the_list_shows_measured_over_enabled_for_a_partial_row(self, app, profile):
        _scored_listing(profile, "partial", share=0.32 * 0.6 + 0.68 * 0.3)
        body = app.test_client().get("/properties").get_data(as_text=True)
        assert "2/6" in body
        assert "not measured: travel, sea" in body

    def test_a_row_scored_before_coverage_still_shows_it_derived(self, app, profile):
        _scored_listing(profile, "legacy", share=None)
        body = app.test_client().get("/properties").get_data(as_text=True)
        assert "2/6" in body

    def test_measured_full_keeps_only_fully_covered_rows(self, app, profile):
        _scored_listing(
            profile,
            "row-fullcov-x",
            share=1.0,
            components={"value_score": 90.0, "travel_score": 70.0, "sea_score": 60.0},
        )
        _scored_listing(profile, "row-partialcov-x", share=0.4)
        _scored_listing(profile, "row-legacycov-x", share=None)
        client = app.test_client()
        everything = client.get("/properties").get_data(as_text=True)
        for key in ("row-fullcov-x", "row-partialcov-x", "row-legacycov-x"):
            assert key in everything
        only_full = client.get("/properties?measured=full").get_data(as_text=True)
        assert "row-fullcov-x" in only_full
        assert "row-partialcov-x" not in only_full
        # A row scored before coverage was recorded is not full until rescored:
        # unknown coverage must not pass as full.
        assert "row-legacycov-x" not in only_full

    def test_the_detail_page_names_the_share_and_the_missing_criteria(
        self, app, profile
    ):
        pid = _scored_listing(profile, "detail", share=0.396)
        body = app.test_client().get(f"/properties/{pid}").get_data(as_text=True)
        assert "40% of the enabled weight" in body
        assert "not measured: travel, sea" in body
