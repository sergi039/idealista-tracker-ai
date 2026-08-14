"""The pool criterion (proposal D17) — the acceptance matrix the review set.

The invariants that must never break:
* only a measured drive time to a qualifying pool scores; `unverified_absence`
  is None and NEVER 0 — the single cross-check proves nothing;
* the owner's hand-set flag is the only path to 0, outranks everything and
  survives recomputes;
* a refusal never overwrites measured candidates;
* `require_indoor` narrows to evidence-backed candidates, and evidence is
  labeled, not asserted;
* the criterion ships at weight 0 — adding pool data changes no score until
  the owner enables the weight through the two-step preview/confirm flow.
"""

from types import SimpleNamespace

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.pool_service import PoolService
from services.property_scoring_service import (
    HousingPropertyScorer,
    PropertyScoringService,
)
from tests import setup_test_environment
from utils.backfill_pool import needs_pool

SPORTS_CENTRE = {
    "type": "way",
    "center": {"lat": 43.53, "lon": -7.05},
    "tags": {
        "leisure": "sports_centre",
        "sport": "swimming",
        "name": "Piscina Municipal de Ribadeo",
        "covered": "yes",
    },
}
NAMED_POOL = {
    "type": "way",
    "center": {"lat": 43.54, "lon": -6.72},
    "tags": {"leisure": "swimming_pool", "name": "Piscina Municipal de Navia"},
}
UNNAMED_POOL = {
    "type": "way",
    "center": {"lat": 43.55, "lon": -6.8},
    "tags": {"leisure": "swimming_pool", "access": "yes"},
}
HOTEL_POOL = {
    "type": "way",
    "center": {"lat": 43.55, "lon": -6.81},
    "tags": {
        "leisure": "swimming_pool",
        "name": "Hotel Pool",
        "access": "customers",
    },
}


class _FakeEnrichment:
    def __init__(self, elements=None, failure=None):
        self.elements = elements
        self.failure = failure

    def _overpass_elements(self, query):
        if self.failure is not None:
            return None, SimpleNamespace(reason=self.failure)
        return self.elements, None


class _FakeTravel:
    def __init__(self, minutes=None, text_place=None, text_fails=False):
        self.minutes = minutes if minutes is not None else []
        self.text_place = text_place
        self.text_fails = text_fails
        self.measure_calls = []
        self.text_calls = 0

    def measure_drive_minutes(self, lat, lon, points):
        self.measure_calls.append(points)
        return (self.minutes + [None] * len(points))[: len(points)]

    def _nearest_place_text_search(self, lat, lon, query, place_types):
        self.text_calls += 1
        if self.text_fails:
            return SimpleNamespace(place=None, failure=SimpleNamespace(reason="denied"))
        if self.text_place:
            return SimpleNamespace(place=self.text_place, failure=None)
        return SimpleNamespace(place=None, failure=None)


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


def _prop(**overrides):
    fields = dict(
        source_email_id=f"pool-{len(overrides)}-{overrides.get('title', 'x')}",
        title="PoolFixture",
        municipality="Navia",
        location_lat=43.55,
        location_lon=-6.83,
        property_category="housing",
    )
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def _service(
    elements=None, failure=None, minutes=None, text_place=None, text_fails=False
):
    return PoolService(
        enrichment_service=_FakeEnrichment(elements=elements, failure=failure),
        travel_service=_FakeTravel(
            minutes=minutes, text_place=text_place, text_fails=text_fails
        ),
    )


class TestAcceptanceMatrix:
    """OSM {found, noise-only, empty, error} × Google {measured, refused,
    found, empty, refused} — every cell lands on a legal status."""

    def test_found_and_measured_is_ok(self, app):
        prop = _prop(title="a")
        part = _service(elements=[SPORTS_CENTRE, NAMED_POOL], minutes=[12, 18]).enrich(
            prop
        )
        assert part["status"] == "ok"
        # Nearest-first: Navia (~9 km, no indoor tags) before Ribadeo
        # (~18 km, covered=yes) — evidence stays per candidate.
        assert part["candidates"][0]["name"] == "Piscina Municipal de Navia"
        assert part["candidates"][0]["drive_min"] == 12
        assert part["candidates"][0]["indoor_status"] == "unknown"
        assert part["candidates"][1]["indoor_status"] == "verified"
        assert part["candidates"][1]["indoor_evidence"] == "covered=yes"

    def test_found_but_dm_refused_is_pending_never_absence(self, app):
        prop = _prop(title="b")
        part = _service(elements=[SPORTS_CENTRE], minutes=[None]).enrich(prop)
        assert part["status"] == "pending_measurement"
        assert needs_pool(prop) is True, "a pending row stays in the rerun scope"

    def test_noise_only_runs_the_cross_check(self, app):
        prop = _prop(title="c")
        service = _service(elements=[UNNAMED_POOL, HOTEL_POOL])
        part = service.enrich(prop)
        assert part["status"] == "unverified_absence"
        assert part["cross_check"]["ran"] is True
        assert service.travel_service.text_calls == 1

    def test_empty_and_cross_check_found_measures_it(self, app):
        prop = _prop(title="d")
        part = _service(
            elements=[],
            minutes=[22],
            text_place={"name": "Piscina X", "lat": 43.5, "lon": -6.9},
        ).enrich(prop)
        assert part["status"] == "ok"
        assert part["candidates"][0]["source"] == "places_text_search"
        assert part["candidates"][0]["drive_min"] == 22

    def test_empty_and_cross_check_refused_is_unverified_absence(self, app):
        prop = _prop(title="e")
        part = _service(elements=[], text_fails=True).enrich(prop)
        assert part["status"] == "unverified_absence"
        assert part["cross_check"]["outcome"] == "refused"

    def test_overpass_refusal_is_unavailable(self, app):
        prop = _prop(title="f")
        part = _service(failure="overpass_query_error").enrich(prop)
        assert part["status"] == "unavailable"

    def test_refusal_never_overwrites_measured_candidates(self, app):
        prop = _prop(title="g")
        _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        part = _service(failure="overpass_query_error").enrich(prop)
        assert part["status"] == "ok"
        assert part["candidates"][0]["drive_min"] == 12
        assert part["last_attempt_status"] == "unavailable"

    def test_owner_flag_survives_recompute(self, app):
        prop = _prop(title="h")
        _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        enrichment = dict(prop.enrichment)
        enrichment["pool"]["owner_no_pool"] = {"set_at": "2026-08-14T00:00:00+00:00"}
        prop.enrichment = enrichment
        part = _service(elements=[SPORTS_CENTRE], minutes=[9]).enrich(prop)
        assert isinstance(part.get("owner_no_pool"), dict)


class TestScorerInvariants:
    def _score(self, pool_block, best=10.0, worst=40.0, require_indoor=True):
        prop = Property(enrichment={"pool": pool_block} if pool_block else {})
        scorer = HousingPropertyScorer()
        return scorer._pool_score(
            prop, best_min=best, worst_min=worst, require_indoor=require_indoor
        )

    def test_unverified_absence_is_none_never_zero(self):
        score, meta = self._score({"status": "unverified_absence"})
        assert score is None
        assert meta["status"] == "unverified_absence"

    def test_owner_flag_is_the_only_zero(self):
        score, meta = self._score(
            {"status": "unverified_absence", "owner_no_pool": {"set_at": "x"}}
        )
        assert score == 0.0
        assert meta["status"] == "owner_verified_absence"

    def test_measured_minutes_score_linearly(self):
        block = {
            "status": "ok",
            "candidates": [{"indoor_status": "verified", "drive_min": 25, "name": "P"}],
        }
        score, meta = self._score(block)
        assert score == 50.0
        assert meta["candidate"] == "P"

    def test_require_indoor_excludes_unknown_evidence(self):
        block = {
            "status": "ok",
            "candidates": [{"indoor_status": "unknown", "drive_min": 5, "name": "O"}],
        }
        score, meta = self._score(block, require_indoor=True)
        assert score is None
        assert meta["status"] == "no_qualifying_candidate"
        score2, _ = self._score(block, require_indoor=False)
        assert score2 == 100.0

    def test_weight_zero_shipping_changes_no_score(self, app):
        with app.app_context():
            prop = _prop(title="neutral", price=100000, area=200)
            service = PropertyScoringService()
            service.calculate_for_property(prop, commit=False)
            before = (prop.score_investment, prop.score_lifestyle)
            enrichment = dict(prop.enrichment or {})
            enrichment["pool"] = {
                "status": "ok",
                "candidates": [
                    {"indoor_status": "verified", "drive_min": 5, "name": "P"}
                ],
            }
            prop.enrichment = enrichment
            service.calculate_for_property(prop, commit=False)
            after = (prop.score_investment, prop.score_lifestyle)
            assert after == before, "weight 0 means pool data moves nothing"

    def test_bad_require_indoor_falls_back_to_defaults(self):
        from services.property_scoring_service import _resolve_pool_config

        resolved, error = _resolve_pool_config(
            {"require_indoor": 0.5}, HousingPropertyScorer.DEFAULT_POOL
        )
        assert error is not None
        assert resolved == HousingPropertyScorer.DEFAULT_POOL


class TestPreviewConfirmFlow:
    def _profile_with_property(self):
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        prop = _prop(
            title="preview",
            search_profile_id=profile.id,
            price=100000,
            area=200,
            enrichment={
                "pool": {
                    "status": "ok",
                    "candidates": [
                        {"indoor_status": "verified", "drive_min": 5, "name": "P"}
                    ],
                }
            },
        )
        PropertyScoringService().calculate_for_property(prop, commit=True)
        return profile, prop

    def test_enabling_the_weight_previews_first_then_confirms(self, app, client):
        profile, prop = self._profile_with_property()
        before = prop.score_lifestyle

        resp = client.post(
            f"/profiles/{profile.id}/edit",
            data={
                "action": "save_scoring_weights",
                "scoring__housing__lifestyle__pool_score": "0.2",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(prop)
        assert prop.score_lifestyle == before, "the preview must not commit"
        db.session.refresh(profile)
        stored = profile.scoring_config or {}
        assert not (
            (stored.get("categories") or {})
            .get("housing", {})
            .get("lifestyle", {})
            .get("pool_score")
        ), "the config must not be stored before the confirm"

        resp = client.post(
            f"/profiles/{profile.id}/edit",
            data={"action": "confirm_pool_scoring"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(prop)
        assert prop.score_lifestyle != before, "the confirm applies and rescores"

    def test_confirm_without_a_pending_preview_is_refused(self, app, client):
        profile, prop = self._profile_with_property()
        before = prop.score_lifestyle
        client.post(
            f"/profiles/{profile.id}/edit",
            data={"action": "confirm_pool_scoring"},
        )
        db.session.refresh(prop)
        assert prop.score_lifestyle == before


class TestCardAndOwnerFlag:
    def test_measured_pool_renders_with_indoor_evidence(self, app, client):
        prop = _prop(
            title="card",
            enrichment={
                "quality_of_life": {"municipality": {"status": "not_matched"}},
                "pool": {
                    "status": "ok",
                    "candidates": [
                        {
                            "name": "Piscina Municipal de Ribadeo",
                            "indoor_status": "verified",
                            "indoor_evidence": "covered=yes",
                            "drive_min": 14,
                            "lat": 43.53,
                            "lon": -7.05,
                            "straight_km": 18.0,
                        }
                    ],
                },
            },
        )
        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "Piscina Municipal de Ribadeo" in body
        assert "14min" in body
        assert ">indoor<" in body

    def test_owner_flag_round_trip(self, app, client):
        prop = _prop(
            title="flag",
            enrichment={
                "quality_of_life": {"municipality": {"status": "not_matched"}},
                "pool": {"status": "unverified_absence"},
            },
        )
        resp = client.post(
            f"/properties/{prop.id}/pool-absence", data={"pool_absence": "set"}
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(prop)
        assert isinstance(prop.enrichment["pool"]["owner_no_pool"], dict)

        client.post(
            f"/properties/{prop.id}/pool-absence", data={"pool_absence": "clear"}
        )
        db.session.refresh(prop)
        assert "owner_no_pool" not in prop.enrichment["pool"]
