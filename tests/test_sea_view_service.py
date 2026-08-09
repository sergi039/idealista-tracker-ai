"""The sea-view verdict, and the ways it is allowed to be wrong.

The verdict replaces a boolean that was `true` on exactly one of 168 rows and
was built from a ~300-character email fragment. The point of these tests is not
that the estimate is accurate -- a 25 m bare-earth terrain model cannot be --
but that it never dresses an unknown up as an answer:

* a refusal from OpenStreetMap or the elevation model becomes `unknown`, never
  `no` (the exact confusion #98 documented for travel times);
* a municipality centroid -- what an "approximate" coordinate is -- produces
  `unknown` when there is sea nearby, and only a confident `no` when the whole
  neighbourhood is far inland;
* terrain alone never reaches `yes`, because trees and buildings are invisible
  to the model;
* the mirrored `Land` boolean reads as `likely`, never as `yes`.

Every external source is mocked; nothing here touches the network.
"""

import pytest

from services import sea_view_service as svc


class _FakeProperty:
    """The attributes the service actually reads off a Property."""

    def __init__(
        self,
        title=None,
        description=None,
        lat=None,
        lon=None,
        accuracy="precise",
        enrichment=None,
    ):
        self.title = title
        self.description = description
        self.location_lat = lat
        self.location_lon = lon
        self.location_accuracy = accuracy
        self.enrichment = enrichment or {}

    @property
    def environment(self):
        """Mirrors `Property.environment`: the computed section, else the
        mirrored legacy blob."""
        section = self.enrichment.get("environment")
        if isinstance(section, dict):
            return section
        legacy = self.enrichment.get("legacy_land") or {}
        legacy_env = legacy.get("environment")
        return legacy_env if isinstance(legacy_env, dict) else {}


# Cudillero, on the Asturian coast, and a point ~1.5 km due north of it in the
# water. Only the relative geometry matters to these tests.
COAST_LAT, COAST_LON = 43.5623, -6.1450
INLAND_LAT, INLAND_LON = 43.3635, -5.7082


def _coastline(*points):
    return lambda lat, lon, session=None: list(points)


def _flat_profile(observer_elevation, sample_elevation=0.0):
    def _fetch(points, session=None):
        return [observer_elevation] + [sample_elevation] * (len(points) - 1)

    return _fetch


class TestGeometryNeverInventsAnAnswer:
    def test_a_refusing_coastline_source_is_unknown_not_no(self, monkeypatch):
        """The failure #98 is about: an external refusal written to the
        database as a computed negative."""

        def _refuse(lat, lon, session=None):
            raise svc.SeaViewSourceError("Overpass returned HTTP 429")

        monkeypatch.setattr(svc, "fetch_coastline_points", _refuse)
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.UNKNOWN
        assert result["reason"] == "coastline_source_unavailable"

    def test_a_refusing_elevation_source_is_unknown_not_no(self, monkeypatch):
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.01, COAST_LON))
        )

        def _refuse(points, session=None):
            raise svc.SeaViewSourceError("Elevation API returned HTTP 503")

        monkeypatch.setattr(svc, "fetch_elevations", _refuse)
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.UNKNOWN
        assert result["reason"] == "elevation_source_unavailable"

    def test_no_coordinates_is_unknown(self, monkeypatch):
        prop = _FakeProperty(title="Plot", description="")
        verdict = svc.evaluate_property(prop, use_ai=False)
        assert verdict["sea_view"] == svc.UNKNOWN
        assert verdict["sea_view_detail"]["geometry"]["reason"] == "no_coordinates"


class TestOutgoingRequests:
    """overpass-api.de refuses the default `python-requests` User-Agent with a
    406, and refuses a UA carrying a parenthetical comment too. That is not a
    style preference -- without a bare product token every coastline lookup
    comes back as a refusal and every verdict degrades to `unknown`.
    """

    def test_the_user_agent_is_a_bare_product_token(self):
        assert "(" not in svc.HTTP_USER_AGENT
        assert "python-requests" not in svc.HTTP_USER_AGENT.lower()

    def test_the_coastline_request_sends_it(self, monkeypatch):
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"elements": []}

        def _post(url, **kwargs):
            captured.update(kwargs)
            return _Response()

        monkeypatch.setattr(svc.requests, "post", _post)
        svc.fetch_coastline_points(43.0, -6.0)
        assert captured["headers"]["User-Agent"] == svc.HTTP_USER_AGENT

    def test_the_elevation_request_sends_it(self, monkeypatch):
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "status": "OK",
                    "results": [{"elevation": 10.0}],
                }

        def _get(url, **kwargs):
            captured.update(kwargs)
            return _Response()

        monkeypatch.setattr(svc.requests, "get", _get)
        monkeypatch.setattr(svc.Config, "SEA_VIEW_ELEVATION_MIN_INTERVAL_S", 0.0)
        svc.fetch_elevations([(43.0, -6.0)])
        assert captured["headers"]["User-Agent"] == svc.HTTP_USER_AGENT


class TestApproximateCoordinates:
    def test_a_centroid_near_the_sea_is_unknown(self, monkeypatch):
        """Two different plots in one town share a Nominatim centroid, so a
        verdict computed from it describes the town, not the plot."""
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.01, COAST_LON))
        )
        monkeypatch.setattr(svc, "fetch_elevations", _flat_profile(80.0))
        result = svc.evaluate_geometry(
            COAST_LAT, COAST_LON, "approximate", use_cache=False
        )
        assert result["state"] == svc.UNKNOWN
        assert result["reason"] == "approximate_coordinates"

    def test_a_centroid_far_inland_is_a_confident_no(self, monkeypatch):
        """No coastline anywhere near the whole municipality, so the imprecision
        of the point cannot change the answer."""
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())
        result = svc.evaluate_geometry(
            INLAND_LAT, INLAND_LON, "approximate", use_cache=False
        )
        assert result["state"] == svc.NO
        assert result["reason"] == "no_coastline_in_range"

    def test_the_distance_that_decides_a_negative_grows(self, monkeypatch):
        """Sea 14 km from a precise point rules a view out. The same 14 km from
        a municipality centroid does not, because the plot itself may be
        kilometres closer to the water than the centroid is."""
        sea_14km = (INLAND_LAT + 0.126, INLAND_LON)
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline(sea_14km))
        monkeypatch.setattr(svc, "fetch_elevations", _flat_profile(100.0))

        precise = svc.evaluate_geometry(
            INLAND_LAT, INLAND_LON, "precise", use_cache=False
        )
        assert precise["state"] == svc.NO
        assert precise["reason"] == "sea_too_far"

        approximate = svc.evaluate_geometry(
            INLAND_LAT, INLAND_LON, "approximate", use_cache=False
        )
        assert approximate["state"] == svc.UNKNOWN


class TestCoastlineIsFetchedPerCell:
    """One Overpass query per property would be both abusive and unworkable:
    the public instance grants two slots per IP and answers 504 while they are
    busy. Cells collapse the 351 rows onto 67 queries."""

    def test_neighbouring_points_share_one_cell(self):
        # The two Siero plots that sit on the same Nominatim centroid, and a
        # point a few hundred metres away.
        assert svc.coastline_cell(43.3635, -5.7082) == svc.coastline_cell(
            43.3661, -5.7104
        )

    def test_the_cell_query_reaches_past_every_decision_distance(self):
        """Otherwise "no coastline in the cell" would not be a sound negative
        for a property sitting at the cell's corner."""
        widest_decision = svc.MAX_SEA_DISTANCE_M + svc.APPROXIMATE_COORD_SLACK_M
        assert (
            svc.COASTLINE_QUERY_RADIUS_M
            >= widest_decision + svc.COASTLINE_CELL_HALF_DIAGONAL_M
        )

    def test_the_query_is_issued_for_the_cell_centre(self, monkeypatch):
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"elements": []}

        def _post(url, **kwargs):
            captured["query"] = kwargs["data"]["data"]
            return _Response()

        monkeypatch.setattr(svc.requests, "post", _post)
        monkeypatch.setattr(svc, "OVERPASS_MIN_INTERVAL_S", 0.0)
        svc.fetch_coastline_points(43.3635, -5.7082)
        centre_lat, centre_lon = svc.coastline_cell(43.3635, -5.7082)
        assert f"{centre_lat:.4f},{centre_lon:.4f}" in captured["query"]
        assert str(svc.COASTLINE_QUERY_RADIUS_M) in captured["query"]


class TestTerrain:
    def test_clear_line_of_sight_is_likely_never_yes(self, monkeypatch):
        """EU-DEM is bare earth: it cannot see the pine wood on the ridge, so
        geometry on its own is not allowed to reach `yes`."""
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.02, COAST_LON))
        )
        monkeypatch.setattr(svc, "fetch_elevations", _flat_profile(120.0, 10.0))
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.LIKELY
        assert result["reason"] == "clear_line_of_sight"

    def test_a_hill_in_the_way_blocks_the_view(self, monkeypatch):
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.02, COAST_LON))
        )

        def _ridge(points, session=None):
            # Observer at 40 m, then a 300 m ridge immediately in front.
            return [40.0] + [300.0] * (len(points) - 1)

        monkeypatch.setattr(svc, "fetch_elevations", _ridge)
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.NO
        assert result["reason"] == "terrain_blocks_line_of_sight"
        assert result["blocked_at_m"] > 0

    def test_sea_beyond_the_maximum_distance_is_no(self, monkeypatch):
        far = (COAST_LAT + 0.5, COAST_LON)  # ~55 km away
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline(far))
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.NO
        assert result["reason"] == "sea_too_far"

    def test_a_sea_level_plot_far_from_the_shore_is_below_the_horizon(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.09, COAST_LON))
        )
        monkeypatch.setattr(svc, "fetch_elevations", _flat_profile(0.0))
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.NO
        assert result["reason"] == "below_the_horizon"

    def test_the_elevation_request_stays_inside_the_free_tier_cap(self, monkeypatch):
        captured = {}

        def _capture(points, session=None):
            captured["count"] = len(points)
            return [100.0] * len(points)

        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.1, COAST_LON))
        )
        monkeypatch.setattr(svc, "fetch_elevations", _capture)
        svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert captured["count"] <= svc.MAX_PROFILE_SAMPLES + 1


class TestTextSignal:
    def test_text_without_a_sea_mention_never_reaches_the_ai(self, monkeypatch):
        def _explode(text):
            raise AssertionError("the AI must not be called for text with no mention")

        monkeypatch.setattr(svc, "classify_text_with_ai", _explode)
        result = svc.evaluate_text("Buildable plot in Siero", "quiet street", True)
        assert result["claim"] == svc.TEXT_NONE
        assert result["source"] == "keywords"

    def test_the_ai_demotes_a_proximity_phrase(self, monkeypatch):
        """This is the whole job of the AI filter: `cerca del mar` is the agency
        saying the beach is close, not that you can see it."""
        monkeypatch.setattr(
            svc,
            "classify_text_with_ai",
            lambda text: {"claim": svc.TEXT_PROXIMITY, "quote": "cerca del mar"},
        )
        result = svc.evaluate_text("Plot", "Parcela cerca del mar, 800 m2", True)
        assert result["claim"] == svc.TEXT_PROXIMITY
        assert result["source"] == "ai"

    def test_an_unavailable_bridge_falls_back_to_keywords_and_says_so(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            svc,
            "classify_text_with_ai",
            lambda text: {"claim": svc.TEXT_UNAVAILABLE, "error": "bridge down"},
        )
        result = svc.evaluate_text("Casa con vistas al mar", "", True)
        assert result["claim"] == svc.TEXT_VIEW
        assert result["source"] == "keywords_only"
        assert result["ai_error"] == "bridge down"

    def test_the_keyword_fallback_will_not_promote_an_ambiguous_phrase(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            svc,
            "classify_text_with_ai",
            lambda text: {"claim": svc.TEXT_UNAVAILABLE, "error": "bridge down"},
        )
        result = svc.evaluate_text("Piso en primera línea", "", True)
        assert result["claim"] == svc.TEXT_PROXIMITY

    def test_a_malformed_ai_answer_is_unavailable_not_a_guess(self, monkeypatch):
        """Prose instead of JSON must not be read as agreement."""
        import services.subscription_transport as transport

        monkeypatch.setattr(
            transport, "complete", lambda *a, **k: {"text": "I think maybe yes?"}
        )
        assert svc.classify_text_with_ai("vistas al mar")["claim"] == (
            svc.TEXT_UNAVAILABLE
        )

    def test_an_ai_refusal_does_not_take_down_the_verdict(self, monkeypatch):
        import services.subscription_transport as transport

        def _refuse(*args, **kwargs):
            raise RuntimeError("bridge unreachable")

        monkeypatch.setattr(transport, "complete", _refuse)
        assert svc.classify_text_with_ai("vistas al mar")["claim"] == (
            svc.TEXT_UNAVAILABLE
        )


class TestCombination:
    def test_text_and_terrain_agreeing_is_the_only_route_to_yes(self):
        state, source, _ = svc.combine({"claim": svc.TEXT_VIEW}, {"state": svc.LIKELY})
        assert state == svc.YES
        assert source == "text+geometry"

    def test_a_claimed_view_the_terrain_denies_stays_likely(self):
        state, source, reason = svc.combine(
            {"claim": svc.TEXT_VIEW},
            {"state": svc.NO, "reason": "terrain_blocks_line_of_sight"},
        )
        assert state == svc.LIKELY
        assert source == "text"
        assert "terrain_blocks_line_of_sight" in reason

    def test_terrain_alone_is_likely(self):
        state, source, _ = svc.combine({"claim": svc.TEXT_NONE}, {"state": svc.LIKELY})
        assert (state, source) == (svc.LIKELY, "geometry")

    def test_nothing_computable_is_unknown(self):
        state, _, _ = svc.combine(
            {"claim": svc.TEXT_NONE}, {"state": svc.UNKNOWN, "reason": "no_coordinates"}
        )
        assert state == svc.UNKNOWN


class TestLegacyBooleanIsNotEvidence:
    def test_a_mirrored_true_reads_as_likely(self):
        assert svc.normalize_state(True) == svc.LIKELY

    def test_a_mirrored_false_reads_as_unknown(self):
        """The legacy `false` was produced by the same weak keyword pass over a
        truncated email body. It is the absence of a match, not a finding."""
        assert svc.normalize_state(False) == svc.UNKNOWN
        assert svc.normalize_state(None) == svc.UNKNOWN

    def test_read_verdict_folds_the_legacy_blob(self):
        prop = _FakeProperty(
            enrichment={"legacy_land": {"environment": {"sea_view": True}}}
        )
        assert svc.read_verdict(prop)["state"] == svc.LIKELY


class TestManualOverride:
    def test_a_hand_set_verdict_survives_recomputation(self, monkeypatch):
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())
        prop = _FakeProperty(
            title="Plot",
            description="",
            lat=INLAND_LAT,
            lon=INLAND_LON,
            enrichment={
                "environment": {
                    "sea_view": svc.YES,
                    "sea_view_detail": {"source": "manual", "reason": "set by hand"},
                }
            },
        )
        verdict = svc.evaluate_property(prop, use_ai=False)
        assert verdict["sea_view"] == svc.YES
        assert verdict["sea_view_detail"]["source"] == "manual"
        # The computed opinion is kept so a disagreement stays visible.
        assert verdict["sea_view_detail"]["computed_state"] == svc.NO


class TestPersistence:
    def test_applying_a_verdict_keeps_the_other_environment_keys(self, monkeypatch):
        prop = _FakeProperty(
            enrichment={"environment": {"orientation": "south"}, "google": {"x": 1}}
        )
        svc.apply_to_property(
            prop, {"sea_view": svc.LIKELY, "sea_view_detail": {}}, commit=False
        )
        assert prop.enrichment["environment"]["orientation"] == "south"
        assert prop.enrichment["environment"]["sea_view"] == svc.LIKELY
        assert prop.enrichment["google"] == {"x": 1}


@pytest.fixture
def app():
    from app import create_app, db
    from tests import setup_test_environment

    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.drop_all()


@pytest.fixture
def stored_property(app):
    from app import db
    from models import Property

    with app.app_context():
        prop = Property(
            source_email_id="manual_override",
            title="Plot with no verdict yet",
            location_lat=43.3635,
            location_lon=-5.7082,
            location_accuracy="approximate",
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


class TestManualOverrideEndToEnd:
    """The correction path has to be reachable, not merely implemented.

    It was not: the environment card only rendered when the property already
    had environment data, so on every row that needed a hand-set verdict there
    was no control to set one with.
    """

    def test_the_environment_card_renders_with_no_data_at_all(
        self, app, stored_property
    ):
        response = app.test_client().get(f"/properties/{stored_property}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'id="environment-form"' in body, (
            "the sea-view override must be reachable on a property that has no "
            "environment data yet -- that is precisely when it is needed"
        )
        assert 'name="sea_view"' in body

    def test_posting_a_state_marks_it_as_set_by_hand(self, app, stored_property):
        from app import db
        from models import Property

        response = app.test_client().post(
            f"/api/property/{stored_property}/environment",
            json={"sea_view": "yes", "orientation": "north"},
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True

        prop = db.session.get(Property, stored_property)
        assert prop.environment["sea_view"] == svc.YES
        assert prop.environment["sea_view_detail"]["source"] == "manual"

    def test_a_hand_set_verdict_is_not_overwritten_by_a_recalculation(
        self, app, stored_property, monkeypatch
    ):
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment", json={"sea_view": "yes"}
        )
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())

        prop = db.session.get(Property, stored_property)
        svc.calculate_for_property(prop, use_ai=False, commit=True)

        refreshed = db.session.get(Property, stored_property)
        assert refreshed.environment["sea_view"] == svc.YES
        assert refreshed.environment["sea_view_detail"]["computed_state"] == svc.NO

    def test_an_unknown_value_is_not_silently_promoted(self, app, stored_property):
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment",
            json={"sea_view": "definitely maybe"},
        )
        prop = db.session.get(Property, stored_property)
        assert prop.environment["sea_view"] == svc.UNKNOWN

    def test_a_boolean_from_the_older_form_still_works(self, app, stored_property):
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment", json={"sea_view": True}
        )
        prop = db.session.get(Property, stored_property)
        assert prop.environment["sea_view"] == svc.YES


class TestDistanceMath:
    @pytest.mark.parametrize(
        "lat1,lon1,lat2,lon2,expected_km",
        [
            (43.5623, -6.1450, 43.5623, -6.1450, 0.0),
            (43.5623, -6.1450, 43.6523, -6.1450, 10.0),
        ],
    )
    def test_haversine_matches_known_separations(
        self, lat1, lon1, lat2, lon2, expected_km
    ):
        got = svc.haversine_m(lat1, lon1, lat2, lon2) / 1000.0
        assert got == pytest.approx(expected_km, abs=0.1)

    def test_curvature_drop_is_the_textbook_value(self):
        # ~6.4 m at 10 km with the 7/6 refraction radius.
        assert svc._curvature_drop_m(10_000) == pytest.approx(6.7, abs=0.3)
