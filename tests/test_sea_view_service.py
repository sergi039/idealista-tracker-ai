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

import json

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


class _FakeResponse:
    """The slice of `requests.Response` the coastline reader actually uses.

    It streams, so a double that only offers `.content` would let a bug in the
    size ceiling pass unnoticed.
    """

    def __init__(self, body: bytes, status_code: int = 200, headers=None):
        self.status_code = status_code
        # `headers={}` means "no Content-Length at all" -- `or` would replace
        # it with an honest default and the chunk-path tests would never get
        # past the declared-length check.
        self.headers = (
            {"Content-Length": str(len(body))} if headers is None else headers
        )
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def close(self):
        self.closed = True


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

        def _post(url, **kwargs):
            captured.update(kwargs)
            return _FakeResponse(b'{"elements": []}')

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
        # The interval is captured when ELEVATION_GATE is built, so setting it
        # on Config afterwards does nothing -- setting it on the gate is what
        # a test has to do now.
        monkeypatch.setattr(svc.ELEVATION_GATE, "min_interval_s", 0.0)
        svc.fetch_elevations([(43.0, -6.0)])
        assert captured["headers"]["User-Agent"] == svc.HTTP_USER_AGENT

    def test_the_elevation_request_is_paced_by_its_own_gate(self, monkeypatch):
        """OpenTopoData's public instance asks for one call a second, and the
        retries count towards that as much as the first attempt.

        Its own gate rather than Overpass's: two endpoints, two budgets, and
        waiting for one because the other is busy would slow a run for nothing.
        """
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"status": "OK", "results": [{"elevation": 10.0}]}

        def _get(url, **kwargs):
            captured.update(kwargs)
            return _Response()

        monkeypatch.setattr(svc.requests, "get", _get)
        monkeypatch.setattr(svc.ELEVATION_GATE, "min_interval_s", 0.0)

        gates = []
        monkeypatch.setattr(
            svc,
            "request_with_retries",
            lambda fn, *a, **kw: gates.append(kw.get("gate")) or _get(*a, **kw),
        )
        svc.fetch_elevations([(43.0, -6.0)])

        assert gates == [svc.ELEVATION_GATE]
        assert gates[0] is not svc.OVERPASS_GATE


class TestAPartialAnswerIsNotAnAnswer:
    """Independent review (Codex, 2026-08-09) on the merged change.

    Overpass reports a query that ran out of time or memory as HTTP 200 with a
    top-level `remark` and whatever it collected. Reading that as an empty
    result wrote a truncated answer to the database as a computed `no` -- the
    #98 mistake with a different source. Worse, it was then cached for 30 days.
    """

    def _responder(self, payload):
        return lambda url, **kwargs: _FakeResponse(json.dumps(payload).encode())

    def test_a_remark_is_a_refusal_not_an_empty_coast(self, monkeypatch):
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(
            svc.requests,
            "post",
            self._responder(
                {
                    "elements": [],
                    "remark": "runtime error: Query timed out in 'query' at line 1",
                }
            ),
        )
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_ways_without_geometry_are_a_refusal(self, monkeypatch):
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(
            svc.requests,
            "post",
            self._responder({"elements": [{"type": "way", "geometry": None}]}),
        )
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_an_empty_remark_is_still_a_remark(self, monkeypatch):
        """Third review round: `if remark:` let `{"remark": ""}` through, and a
        reply that carries the field at all is a reply about a query that did
        not finish."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(
            svc.requests, "post", self._responder({"remark": "", "elements": []})
        )
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            "not an object",
            {"elements": [{"geometry": [{"lat": {}, "lon": 1}]}]},
            {"elements": [{"geometry": [{"lat": "north", "lon": 1}]}]},
            {"elements": [{"geometry": [{"lat": float("nan"), "lon": 1}]}]},
        ],
    )
    def test_every_unreadable_body_raises_one_exception_type(
        self, monkeypatch, payload
    ):
        """Third review round: `float({})` raised TypeError straight through
        `evaluate_geometry`'s SeaViewSourceError handler, aborting the row
        instead of degrading it to `unknown`. Parameterised so one shape
        failing cannot hide the others."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc.requests, "post", self._responder(payload))
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_body_over_the_ceiling_is_refused_before_it_is_parsed(self, monkeypatch):
        """Fourth review round: the reply is untrusted input however well known
        the endpoint, and nothing bounded what was parsed or kept."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc, "MAX_COASTLINE_RESPONSE_BYTES", 64)

        huge = _FakeResponse(b'{"elements": []}' + b" " * 200)
        monkeypatch.setattr(svc.requests, "post", lambda url, **kwargs: huge)
        with pytest.raises(svc.SeaViewSourceError, match="ceiling"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_one_oversized_chunk_is_refused_before_it_is_appended(self, monkeypatch):
        """Sixth review round: the ceiling was checked after extending the
        buffer, so a single chunk larger than it landed in memory first."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc, "MAX_COASTLINE_RESPONSE_BYTES", 64)

        class _OneBigChunk(_FakeResponse):
            def __init__(self):
                super().__init__(b"x" * 200, headers={})

            def iter_content(self, chunk_size=1):
                yield self._body  # one chunk, whatever was asked for

        response = _OneBigChunk()
        monkeypatch.setattr(svc.requests, "post", lambda url, **kwargs: response)
        with pytest.raises(svc.SeaViewSourceError, match="more than"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)
        assert response.closed, "the oversized response should be closed"

    def test_too_many_points_are_refused_rather_than_truncated(self, monkeypatch):
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc, "MAX_COASTLINE_POINTS", 3)
        monkeypatch.setattr(
            svc.requests,
            "post",
            self._responder(
                {
                    "elements": [
                        {"geometry": [{"lat": 43.5, "lon": -6.1} for _ in range(10)]}
                    ]
                }
            ),
        )
        with pytest.raises(svc.SeaViewSourceError, match="more than"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_boolean_coordinate_is_refused(self, monkeypatch):
        """`float(True)` is 1.0, which would have sailed through as a latitude."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(
            svc.requests,
            "post",
            self._responder({"elements": [{"geometry": [{"lat": True, "lon": 0}]}]}),
        )
        with pytest.raises(svc.SeaViewSourceError, match="boolean"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_deeply_nested_body_is_a_source_error_not_a_recursion_error(
        self, monkeypatch
    ):
        """Fourth review round: `response.json()` sat outside the wrapper, so a
        RecursionError escaped as itself."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)

        nested = _FakeResponse((b"[" * 4000) + b"0" + (b"]" * 4000))
        monkeypatch.setattr(svc.requests, "post", lambda url, **kwargs: nested)
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_reply_with_no_elements_array_is_a_refusal(self, monkeypatch):
        """Second review round: `payload.get("elements") or []` turned a body
        with no `elements` at all into a cacheable "no coastline"."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc.requests, "post", self._responder({}))
        with pytest.raises(svc.SeaViewSourceError):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_malformed_elements_shape_raises_a_source_error(self, monkeypatch):
        """...and iterating a dict yielded its keys, so a string reached
        `.get()` and the AttributeError escaped the source-error handling."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        for payload in (
            {"elements": {"type": "way"}},
            {"elements": [None]},
            {"elements": [{"geometry": {"lat": 1}}]},
        ):
            monkeypatch.setattr(svc.requests, "post", self._responder(payload))
            with pytest.raises(svc.SeaViewSourceError):
                svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_node_missing_one_coordinate_is_a_refusal(self, monkeypatch):
        """`out geom` returns complete nodes, so half a coordinate means the
        answer is not what was asked for. Skipping the node would shorten the
        coastline and move the nearest shore (#143 made this a hard refusal)."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(
            svc.requests,
            "post",
            self._responder(
                {
                    "elements": [
                        {
                            "geometry": [
                                {"lat": 43.5, "lon": None},
                                {"lat": 43.6, "lon": -6.1},
                            ]
                        }
                    ]
                }
            ),
        )
        with pytest.raises(svc.SeaViewSourceError, match="without coordinates"):
            svc.fetch_coastline_points(COAST_LAT, COAST_LON)

    def test_a_genuinely_empty_result_is_still_allowed_to_mean_no_coast(
        self, monkeypatch
    ):
        """The one shape that may become `no`: Overpass answered, in full, and
        there is nothing there."""
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
        monkeypatch.setattr(svc.requests, "post", self._responder({"elements": []}))
        assert svc.fetch_coastline_points(INLAND_LAT, INLAND_LON) == []

    def test_a_truncated_answer_never_reaches_a_verdict(self, monkeypatch):
        def _partial(lat, lon, session=None):
            raise svc.SeaViewSourceError("Overpass returned a partial result: timeout")

        monkeypatch.setattr(svc, "fetch_coastline_points", _partial)
        result = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise", use_cache=False)
        assert result["state"] == svc.UNKNOWN


class TestTheGeometryCacheKeepsAccuracyApart:
    """Also from the independent review: the cached verdict was keyed on
    coordinates alone, so a `likely` computed for a surveyed address could be
    served for a municipality centroid that happened to round to the same
    point -- which is exactly the confusion the approximate rule exists to
    prevent."""

    def test_a_precise_verdict_is_not_served_for_an_approximate_point(
        self, monkeypatch, app
    ):
        monkeypatch.setattr(
            svc, "fetch_coastline_points", _coastline((COAST_LAT + 0.02, COAST_LON))
        )
        monkeypatch.setattr(svc, "fetch_elevations", _flat_profile(120.0, 10.0))

        with app.app_context():
            precise = svc.evaluate_geometry(COAST_LAT, COAST_LON, "precise")
            assert precise["state"] == svc.LIKELY

            approximate = svc.evaluate_geometry(COAST_LAT, COAST_LON, "approximate")
            assert approximate["state"] == svc.UNKNOWN
            assert approximate["reason"] == "approximate_coordinates"


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

        def _post(url, **kwargs):
            captured["query"] = kwargs["data"]["data"]
            return _FakeResponse(b'{"elements": []}')

        monkeypatch.setattr(svc.requests, "post", _post)
        # Pacing moved to the gate shared with the amenity query (#152).
        monkeypatch.setattr(svc.OVERPASS_GATE, "min_interval_s", 0.0)
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

    def test_a_recalculation_leaves_a_hand_set_row_completely_alone(
        self, app, stored_property, monkeypatch
    ):
        """The stored row is not touched at all, not merely not flipped.

        Writing the computed opinion back beside the manual one would mean
        another read-modify-write of the whole JSON column from a stale read --
        the defect the second review round named. The computed opinion is still
        available to the caller; it just does not go to the database.
        """
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment", json={"sea_view": "yes"}
        )
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())

        before = dict(db.session.get(Property, stored_property).environment)

        prop = db.session.get(Property, stored_property)
        returned = svc.calculate_for_property(prop, use_ai=False, commit=True)

        refreshed = db.session.get(Property, stored_property)
        assert refreshed.environment == before
        # ...while the caller still gets to see what the model would have said.
        assert returned["sea_view_detail"]["computed_state"] == svc.NO

    def test_an_unknown_value_is_not_silently_promoted(self, app, stored_property):
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment",
            json={"sea_view": "definitely maybe"},
        )
        prop = db.session.get(Property, stored_property)
        assert prop.environment["sea_view"] == svc.UNKNOWN

    def test_a_verdict_set_during_the_calculation_is_not_overwritten(
        self, app, stored_property, monkeypatch
    ):
        """Independent review (Codex, 2026-08-09): `enrichment` is one JSON
        column, so a verdict computed from a row read minutes ago overwrites
        everything written since. Evaluation takes seconds of external calls,
        and the environment endpoint fits comfortably inside that window."""
        from app import db
        from models import Property

        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())
        prop = db.session.get(Property, stored_property)

        # Computed before the owner touches it -- as a backfill would.
        verdict = svc.evaluate_property(prop, use_ai=False)
        assert verdict["sea_view"] == svc.NO

        # ...and the owner sets it by hand while that was being worked out.
        app.test_client().post(
            f"/api/property/{stored_property}/environment", json={"sea_view": "yes"}
        )

        svc.apply_to_property(prop, verdict, commit=True)

        refreshed = db.session.get(Property, stored_property)
        assert refreshed.environment["sea_view"] == svc.YES
        assert refreshed.environment["sea_view_detail"]["source"] == "manual"

    def test_a_stale_write_does_not_take_the_rest_of_the_column_with_it(
        self, app, stored_property, monkeypatch
    ):
        """The same read-modify-write also carried unrelated keys -- travel
        state, geocoding provenance -- back to whatever they were when the row
        was read."""
        from app import db
        from models import Property

        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())
        prop = db.session.get(Property, stored_property)
        verdict = svc.evaluate_property(prop, use_ai=False)

        # Something else writes a different part of the same column meanwhile.
        db.session.execute(
            db.text(
                "UPDATE properties SET enrichment = :value WHERE id = :id"
            ).bindparams(
                value='{"google": {"travel_state": "denied"}}', id=stored_property
            )
        )
        db.session.commit()

        svc.apply_to_property(prop, verdict, commit=True)

        refreshed = db.session.get(Property, stored_property)
        assert refreshed.enrichment["google"] == {"travel_state": "denied"}
        assert refreshed.environment["sea_view"] == svc.NO

    def test_skipping_a_hand_set_row_does_not_park_a_row_lock(
        self, app, stored_property, monkeypatch
    ):
        """Third review round: the skip path took `FOR UPDATE` and returned
        without commit or rollback, leaving the row locked until whatever the
        caller did next -- a whole row away, in the backfill."""
        from app import db
        from models import Property

        app.test_client().post(
            f"/api/property/{stored_property}/environment", json={"sea_view": "yes"}
        )
        monkeypatch.setattr(svc, "fetch_coastline_points", _coastline())

        prop = db.session.get(Property, stored_property)
        svc.calculate_for_property(prop, use_ai=False, commit=True)

        assert db.session().get_nested_transaction() is None, (
            "the skip path left its savepoint open, and with it the row lock"
        )

    def test_a_failed_commit_leaves_the_session_usable(
        self, app, stored_property, monkeypatch
    ):
        """Fourth review round: a commit that raises left the transaction --
        and the row lock -- open, so every later row in the backfill loop
        failed on a poisoned session."""
        from app import db
        from models import Property

        prop = db.session.get(Property, stored_property)
        real_commit = db.session.commit

        def _explode():
            raise RuntimeError("connection lost")

        monkeypatch.setattr(db.session, "commit", _explode)
        with pytest.raises(RuntimeError, match="connection lost"):
            svc.apply_to_property(
                prop, {"sea_view": svc.NO, "sea_view_detail": {}}, commit=True
            )

        monkeypatch.setattr(db.session, "commit", real_commit)
        assert db.session().get_nested_transaction() is None
        # The next row still works, which is the point.
        again = db.session.get(Property, stored_property)
        svc.apply_to_property(
            again, {"sea_view": svc.LIKELY, "sea_view_detail": {}}, commit=True
        )
        assert db.session.get(Property, stored_property).environment["sea_view"] == (
            svc.LIKELY
        )

    def test_a_dirty_session_is_refused_rather_than_flushed(self, app, stored_property):
        """Fifth review round: `begin_nested()` flushes before it opens the
        savepoint. A stale `enrichment` assigned before the call would be
        written out -- erasing a hand-set verdict a moment before the locked
        read goes looking for it -- and an unrelated half-built object would
        raise IntegrityError from a place with no rollback."""
        from app import db
        from models import Property

        prop = db.session.get(Property, stored_property)
        other = Property(source_email_id="pending_row", title="Pending")
        db.session.add(other)

        with pytest.raises(RuntimeError, match="nothing pending"):
            svc.apply_to_property(
                prop, {"sea_view": svc.NO, "sea_view_detail": {}}, commit=True
            )
        db.session.rollback()

    def test_an_expunged_property_is_refused_rather_than_silently_dropped(
        self, app, stored_property
    ):
        """Fourth review round: an expunged object has `state.session is None`,
        so it skipped the membership check and the write went nowhere."""
        from app import db
        from models import Property

        prop = db.session.get(Property, stored_property)
        db.session.expunge(prop)
        with pytest.raises(RuntimeError, match="does not hold"):
            svc.apply_to_property(
                prop, {"sea_view": svc.NO, "sea_view_detail": {}}, commit=True
            )

    def test_a_property_from_another_session_is_refused(self, app, stored_property):
        """...and writing to one would have committed nothing at all, silently."""
        from app import db
        from models import Property

        other = (
            db.create_scoped_session() if hasattr(db, "create_scoped_session") else None
        )
        if other is None:
            from sqlalchemy.orm import Session

            other = Session(bind=db.engine)
        try:
            foreign = other.get(Property, stored_property)
            with pytest.raises(RuntimeError, match="does not hold"):
                svc.apply_to_property(
                    foreign, {"sea_view": svc.NO, "sea_view_detail": {}}, commit=True
                )
        finally:
            other.close()

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
