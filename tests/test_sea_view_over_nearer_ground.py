"""The sea seen *over* the ground that hides the shore under the house.

Property 1282 (Seiruga, Malpica de Bergantinos) is the row this file is built
from. Its stored verdict:

    sea_view "no", reason "terrain_blocks_line_of_sight",
    distance_m 394.7, bearing_deg 21.0, observer_elevation_m 45.2,
    eye_height_m 5.0, blocked_at_m 91.1, blocking_elevation_m 41.0

Every number in it is right. The eye is at 50.2 m, the water's edge is 394.7 m
out, and a brow of 41.0 m at 91.1 m does cut the sight line to it -- the line
is at 38.6 m there. What is wrong is the question: the nearest water's edge is
the *first* thing any rise in the ground occludes, and the open sea beyond it
is 7-13 m clear over the same brow. Measured by hand on EU-DEM over 21
bearings, water is visible from roughly 600 m out across a ~60 degree sector,
and the listing's own photographs show the bay and the Sisargas.

So the terrain in these tests is that terrain: a near hillock just over the
sight line to the shore, and open water past it. An abstract ridge would not
reproduce the defect, because an abstract ridge blocks everything.

Nothing here touches the network.
"""

import math

import pytest

from services import sea_view_service as svc

# Seiruga. Only the relative geometry matters, but using the real point keeps
# the bearings in this file readable against the production row.
PLOT_LAT, PLOT_LON = 43.3136771, -8.8641389

OBSERVER_GROUND_M = 45.2  # eye ends up at 50.2 m, as production recorded
BROW_DISTANCE_M = 91.1
BROW_ELEVATION_M = 41.0
SHORE_DISTANCE_M = 394.7
SHORE_BEARING_DEG = 21.0

# The coastline node production measured to, 394.7 m out on bearing 21.
SHORE_NODE = svc._destination(PLOT_LAT, PLOT_LON, SHORE_BEARING_DEG, SHORE_DISTANCE_M)


def _coast_arc(bearings, distance_m=SHORE_DISTANCE_M):
    """Coastline nodes on a set of bearings, all at one distance.

    A real coastline is a line of nodes; what the fan reads off it is a set of
    bearings, so a node per bearing is the whole of what matters here.
    """
    return [
        svc._destination(PLOT_LAT, PLOT_LON, heading, distance_m)
        for heading in bearings
    ]


def _distance_and_bearing(point):
    return (
        svc.haversine_m(PLOT_LAT, PLOT_LON, point[0], point[1]),
        svc.bearing_deg(PLOT_LAT, PLOT_LON, point[0], point[1]),
    )


def _seiruga_terrain(water_bearings=None, water_from_m=550.0):
    """1282's ground: a brow at 91 m, then a fall to the shore, then water.

    `water_bearings` is the sector the sea is actually in; a bearing outside it
    stays land at the brow's height all the way out, which is what a headland
    looks like from here. `None` means water in every direction, the
    radially-symmetric case.
    """

    def _elevation(point):
        distance, heading = _distance_and_bearing(point)
        if distance < 1.0:
            return OBSERVER_GROUND_M
        wet = water_bearings is None or any(
            svc._bearing_gap_deg(heading, edge) <= 30.0 for edge in water_bearings
        )
        if not wet:
            # Dry land at the brow's height, so this bearing is blocked and
            # never reaches water. It has to stay *below* the eye, or it would
            # block by height rather than by there being no sea.
            return BROW_ELEVATION_M
        if distance >= water_from_m:
            return None  # EU-DEM has no value over open water
        if distance <= BROW_DISTANCE_M:
            # Rising to the brow.
            return OBSERVER_GROUND_M + (BROW_ELEVATION_M - OBSERVER_GROUND_M) * (
                distance / BROW_DISTANCE_M
            )
        # Falling from the brow to the water's edge.
        fall = (distance - BROW_DISTANCE_M) / (water_from_m - BROW_DISTANCE_M)
        return max(0.0, BROW_ELEVATION_M * (1.0 - fall))

    def _fetch(points, session=None):
        return [_elevation(point) for point in points]

    return _fetch


@pytest.fixture
def seiruga(monkeypatch):
    """The whole coast wet, which is the simplest form of 1282."""
    monkeypatch.setattr(
        svc,
        "fetch_coastline_points",
        lambda lat, lon, session=None: _coast_arc(range(0, 360, 5)),
    )
    monkeypatch.setattr(svc, "fetch_elevations", _seiruga_terrain())


class TestTheNearHillockDoesNotDecideIt:
    def test_the_shoreline_ray_really_is_blocked(self, seiruga):
        """The premise. If this stops being true the rest proves nothing.

        Asserted through the public verdict rather than by re-deriving the
        arithmetic, so it fails when the *service* stops seeing the brow.
        """
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["shoreline_visible"] is False
        assert detail["blocked_at_m"] < 0.5 * detail["distance_m"]
        assert detail["blocking_elevation_m"] == pytest.approx(
            BROW_ELEVATION_M, abs=3.0
        )

    def test_open_water_past_the_brow_is_likely_not_no(self, seiruga):
        """The defect, and the fix. This is the assertion the mutation kills."""
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.LIKELY
        assert detail["reason"] == "sea_visible_beyond_terrain"
        assert detail["sea_probe"]["visible"] is True
        # Past the brow and past the hidden shore, which is the point.
        assert detail["sea_probe"]["visible_at_m"] > BROW_DISTANCE_M

    def test_geometry_alone_still_stops_at_likely(self, seiruga):
        """Bare earth cannot see the pine wood on the brow, fan or no fan."""
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)
        state, source, _ = svc.combine({"claim": svc.TEXT_NONE}, detail)

        assert detail["state"] != svc.YES
        assert (state, source) == (svc.LIKELY, "geometry")

    def test_the_two_facts_are_named_apart_on_the_page(self, seiruga):
        """A buyer is not asking whether the shore under the house is visible.

        Both readings are `likely` from geometry, so one label for both would
        put this row back where it started -- indistinguishable from the case
        where nothing is in the way at all.
        """
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)
        over = svc.state_label_key(
            {"state": svc.LIKELY, "source": "geometry", "detail": {"geometry": detail}}
        )
        clear = svc.state_label_key(
            {
                "state": svc.LIKELY,
                "source": "geometry",
                "detail": {"geometry": {"reason": "clear_line_of_sight"}},
            }
        )

        assert over == "likely_geometry_over_terrain"
        assert clear == "likely_geometry"
        assert over != clear


class TestTheFanLooksWhereTheSeaIs:
    def test_a_headland_on_the_nearest_bearing_does_not_hide_the_bay(self, monkeypatch):
        """One extended ray is not enough, which is why this is a fan.

        The nearest coastline node sits at 21 degrees and that bearing runs
        into a headland -- land all the way out. The bay is 60 degrees away.
        A probe that only continued the shoreline ray would answer `no` here,
        and a fixed sector around it might too.
        """
        bay = SHORE_BEARING_DEG + 60.0
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: (
                # The nearest node is on the headland bearing: 300 m against
                # the bay's 900 m, so the shoreline ray goes to the headland.
                _coast_arc([SHORE_BEARING_DEG], 300.0)
                + _coast_arc([bay - 10, bay, bay + 10], 900.0)
            ),
        )
        monkeypatch.setattr(
            svc,
            "fetch_elevations",
            _seiruga_terrain(water_bearings=[bay], water_from_m=900.0),
        )

        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["bearing_deg"] == pytest.approx(SHORE_BEARING_DEG, abs=1.0)
        assert detail["state"] == svc.LIKELY
        assert detail["reason"] == "sea_visible_beyond_terrain"
        assert (
            svc._bearing_gap_deg(detail["sea_probe"]["visible_bearing_deg"], bay)
            <= 30.0
        )

    def test_the_bearings_spread_instead_of_clustering(self):
        """Five rays down one street is one ray that cost five.

        Coastline nodes crowd where the shore is nearest, so picking the five
        nearest would aim the whole fan inside a couple of degrees.
        """
        # Five near-identical nodes where the shore is closest, and four other
        # directions: as many distinct bearings as the fan has rays, so a fan
        # that clustered would have to leave one of the four unlooked at.
        coastline = _coast_arc([21, 21.5, 22, 22.5, 23], 300.0) + _coast_arc(
            [90, 150, 220, 300], 2500.0
        )
        nearest = min(coastline, key=lambda p: _distance_and_bearing(p)[0])

        headings = svc.probe_bearings(PLOT_LAT, PLOT_LON, coastline, 3000.0, 5)

        assert len(headings) == 5
        # The first ray is the shoreline ray: the fan extends the profile the
        # blocked branch already ran rather than replacing it.
        assert (
            svc._bearing_gap_deg(headings[0], _distance_and_bearing(nearest)[1]) <= 0.6
        )
        widest_cluster = max(
            svc._bearing_gap_deg(a, b) for a in headings for b in headings
        )
        assert widest_cluster > 90.0
        # And nothing in the cluster of five near-identical nodes is picked
        # twice over: at most one ray comes out of it.
        from_cluster = [
            heading for heading in headings if svc._bearing_gap_deg(heading, 22.0) < 5.0
        ]
        assert len(from_cluster) == 1

    def test_a_narrow_near_ridge_is_not_stepped_over(self, monkeypatch):
        """The spacing is load bearing in the other direction too.

        A ridge 60 m wide at 60-120 m out hides everything beyond it. Even
        spacing over 3 km puts the first sample at 158 m, past the ridge and
        into the ground behind it, and the fan would report the sea it cannot
        actually see -- turning a false negative into a false positive, which
        is the failure this whole file is about, mirrored.
        """
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20)),
        )

        def _narrow_ridge(points, session=None):
            out = []
            for point in points:
                distance, _ = _distance_and_bearing(point)
                if distance < 1.0:
                    out.append(OBSERVER_GROUND_M)
                elif 60.0 <= distance <= 120.0:
                    out.append(80.0)  # well over the 50.2 m eye
                elif distance >= 550.0:
                    out.append(None)
                else:
                    out.append(5.0)
            return out

        monkeypatch.setattr(svc, "fetch_elevations", _narrow_ridge)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.NO
        assert detail["sea_probe"]["visible"] is False

    def test_the_near_field_is_sampled_finely_enough_to_see_the_brow(self):
        """Even spacing over 3 km starts at 158 m and steps over a 91 m brow.

        This is the arithmetic that makes the fan able to be wrong in the
        opposite direction, so it is asserted rather than trusted.
        """
        fractions = svc.probe_fractions(svc.SEA_PROBE_SAMPLES_PER_RAY)
        reach = svc.probe_distance_m(SHORE_DISTANCE_M, 12_000)
        near = [reach * fraction for fraction in fractions if reach * fraction < 200.0]

        assert reach >= svc.SEA_PROBE_MIN_DISTANCE_M
        assert len(near) >= 4
        assert min(near) < BROW_DISTANCE_M


class TestTheFanCostsOneRequestAndCannotOverflowIt:
    def test_a_blocked_row_spends_exactly_one_extra_elevation_request(self, seiruga):
        calls = []
        original = svc.fetch_elevations

        def _count(points, session=None):
            calls.append(len(points))
            return original(points, session=session)

        import unittest.mock as mock

        with mock.patch.object(svc, "fetch_elevations", _count):
            svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        rays, per_ray = svc._probe_plan(svc.SEA_PROBE_RAYS)
        assert len(calls) == 2
        # The shoreline profile, then the whole fan in one request. Asserted by
        # value: a fan that quietly fell back to one ray would still be "two
        # calls, both inside the cap" and would answer the old question again.
        assert calls[1] == 1 + rays * per_ray == 96
        assert all(
            count <= svc.Config.SEA_VIEW_ELEVATION_MAX_LOCATIONS for count in calls
        )

    def test_a_clear_row_spends_none(self, monkeypatch):
        """The fan runs only where the old verdict was `no`, so nothing that
        already answered pays for it."""
        calls = []
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc([SHORE_BEARING_DEG]),
        )

        def _flat(points, session=None):
            calls.append(len(points))
            return [120.0] + [0.0] * (len(points) - 1)

        monkeypatch.setattr(svc, "fetch_elevations", _flat)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["reason"] == "clear_line_of_sight"
        assert detail["shoreline_visible"] is True
        assert len(calls) == 1

    def test_a_smaller_location_cap_shrinks_the_fan_rather_than_raising(
        self, monkeypatch
    ):
        """`fetch_elevations` refuses a request over the cap. The plan is
        derived from the cap so a lower one costs resolution, not every
        blocked row."""
        monkeypatch.setattr(svc.Config, "SEA_VIEW_ELEVATION_MAX_LOCATIONS", 26)
        rays, per_ray = svc._probe_plan(svc.SEA_PROBE_RAYS)

        assert 1 + rays * per_ray <= 26
        assert rays >= 1 and per_ray >= 1


class TestNotEveryHoleInTheModelIsTheAtlantic:
    """EU-DEM has no value over water *generally*, not over the sea.

    A reservoir, a quarry pond, a wide river or a coastal lagoon reads exactly
    like open sea, and because a ray is walked near-to-far and returns on the
    first qualifying run, a nearer inland gap masked the real answer about the
    sea further out -- which might still be blocked. Found by review after the
    change had shipped; no mutation of the code as written could have seen it,
    because the code did exactly what it said.
    """

    def test_a_pond_nearer_than_the_coast_is_not_reported_as_sea(self, monkeypatch):
        coast_m = 2400.0
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20), coast_m),
        )

        def _pond_then_a_blocked_coast(points, session=None):
            out = []
            for point in points:
                distance, _ = _distance_and_bearing(point)
                if distance < 1.0:
                    out.append(OBSERVER_GROUND_M)
                elif 600.0 <= distance <= 1000.0:
                    out.append(None)  # a reservoir, well inside the shore
                elif distance >= 1800.0:
                    out.append(300.0)  # a ridge hiding the real sea
                else:
                    out.append(5.0)
            return out

        monkeypatch.setattr(svc, "fetch_elevations", _pond_then_a_blocked_coast)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.NO
        assert detail["sea_probe"]["visible"] is False
        # The bound is exact and stored, not tuned: it is the distance to the
        # nearest mapped coastline node, which the shoreline profile already
        # measured.
        assert detail["sea_probe"]["min_water_distance_m"] == pytest.approx(
            detail["distance_m"], abs=0.2
        )

    def test_the_same_water_beyond_the_coast_is_still_the_sea(self, monkeypatch):
        """The guard must not cost the case the fan exists for."""
        coast_m = 400.0
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20), coast_m),
        )

        def _water_past_the_shore(points, session=None):
            out = []
            for point in points:
                distance, _ = _distance_and_bearing(point)
                if distance < 1.0:
                    out.append(OBSERVER_GROUND_M)
                elif 60.0 <= distance <= 120.0:
                    # The brow that hides the 400 m shore, as at Seiruga. The
                    # sight line is at 38.7 m here and the ground at 41.0.
                    out.append(BROW_ELEVATION_M)
                elif 600.0 <= distance <= 1000.0:
                    out.append(None)  # the same gap, now beyond the shore
                elif distance >= 1800.0:
                    out.append(300.0)
                else:
                    out.append(5.0)
            return out

        monkeypatch.setattr(svc, "fetch_elevations", _water_past_the_shore)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.LIKELY
        assert detail["reason"] == "sea_visible_beyond_terrain"

    def test_the_water_it_saw_is_recorded_as_a_point(self, seiruga):
        """#334's rule, applied to the fan's own answer.

        A distance and a bearing are stored rounded, so casting the ray back
        out lands metres off -- enough to put the reconstruction on the far
        bank of a channel. "What water is this?" is exactly the question an
        estuary makes worth asking.
        """
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)
        probe = detail["sea_probe"]

        assert probe["visible"] is True
        back = svc.haversine_m(
            PLOT_LAT, PLOT_LON, probe["visible_lat"], probe["visible_lon"]
        )
        assert back == pytest.approx(probe["visible_at_m"], rel=0.01)
        assert svc.bearing_deg(
            PLOT_LAT, PLOT_LON, probe["visible_lat"], probe["visible_lon"]
        ) == pytest.approx(probe["visible_bearing_deg"], abs=0.5)


class TestTheLocationCapCannotProduceAWrongAnswer:
    """`_probe_plan` promised that a lower cap costs resolution rather than
    raising. It did not keep that promise, and the test that was supposed to
    pin it used a cap of 26 -- a value the arithmetic happens to handle. It
    stepped around the defect instead of at it."""

    @pytest.mark.parametrize("cap", [1, 2, 3, 4, 5, 6, 7, 10, 20, 26, 50, 96, 100, 101])
    def test_the_plan_never_asks_for_more_than_the_cap(self, monkeypatch, cap):
        monkeypatch.setattr(svc.Config, "SEA_VIEW_ELEVATION_MAX_LOCATIONS", cap)
        rays, per_ray = svc._probe_plan(svc.SEA_PROBE_RAYS)

        if rays:
            assert 1 + rays * per_ray <= cap
            # A ray too short to hold a water run can only answer "no water",
            # which is a wrong answer rather than a coarse one.
            assert per_ray >= svc.MIN_WATER_RUN_SAMPLES
        else:
            assert per_ray == 0

    def test_a_cap_too_small_for_a_water_run_is_unknown_not_no(self, monkeypatch):
        """The whole point of deriving the plan. Answering `no` because the
        request budget was small is #98 wearing a config's clothes."""
        monkeypatch.setattr(svc.Config, "SEA_VIEW_ELEVATION_MAX_LOCATIONS", 2)
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20)),
        )
        monkeypatch.setattr(svc, "fetch_elevations", _seiruga_terrain())

        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.UNKNOWN
        assert detail["reason"] == "elevation_source_unavailable"

    def test_a_small_cap_still_answers_when_it_can(self, monkeypatch):
        """Rays are given up before samples, so a tight cap narrows the fan
        rather than blinding it."""
        monkeypatch.setattr(svc.Config, "SEA_VIEW_ELEVATION_MAX_LOCATIONS", 26)
        calls = []
        original = svc.fetch_elevations
        terrain = _seiruga_terrain()

        def _count(points, session=None):
            calls.append(len(points))
            return terrain(points)

        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20)),
        )
        monkeypatch.setattr(svc, "fetch_elevations", _count)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert original is not _count  # the stub really replaced it
        assert calls[1] <= 26
        assert detail["state"] in (svc.LIKELY, svc.NO)


class TestAFanThatDidNotRunIsNotAFanThatFoundNothing:
    def test_a_refused_probe_is_unknown_not_no(self, monkeypatch):
        """Half a measurement is not a measurement (#98).

        The shoreline being hidden was never enough for `no` on its own, so a
        probe the elevation model refused must not promote it into one.
        """
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc([SHORE_BEARING_DEG]),
        )
        terrain = _seiruga_terrain()
        state = {"calls": 0}

        def _refuse_the_second(points, session=None):
            state["calls"] += 1
            if state["calls"] == 1:
                return terrain(points)
            raise svc.SeaViewSourceError("opentopodata is busy")

        monkeypatch.setattr(svc, "fetch_elevations", _refuse_the_second)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.UNKNOWN
        assert detail["reason"] == "elevation_source_unavailable"
        # And it is a *refusal*, so an earlier measurement survives it.
        assert detail["reason"] in svc.SOURCE_REFUSAL_REASONS

    def test_a_refused_probe_is_not_cached(self, monkeypatch):
        stored = {}
        monkeypatch.setattr(
            svc,
            "_cache_set",
            lambda lat, lon, key, data, timeout: stored.update({key: data}),
        )
        monkeypatch.setattr(svc, "_cache_get", lambda lat, lon, key: None)
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc([SHORE_BEARING_DEG]),
        )
        terrain = _seiruga_terrain()
        state = {"calls": 0}

        def _refuse_the_second(points, session=None):
            state["calls"] += 1
            if state["calls"] == 1:
                return terrain(points)
            raise svc.SeaViewSourceError("opentopodata is busy")

        monkeypatch.setattr(svc, "fetch_elevations", _refuse_the_second)
        svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=True)

        assert stored == {}


class TestARealNegativeSurvives:
    def test_a_wall_of_land_in_every_direction_is_still_no(self, monkeypatch):
        """The fan must not turn every row `likely`.

        A plot behind a ridge that runs right around it sees no water on any
        bearing, and the verdict has to stay the confident negative it was.
        """
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20), 2000.0),
        )

        def _ridge(points, session=None):
            return [40.0] + [300.0] * (len(points) - 1)

        monkeypatch.setattr(svc, "fetch_elevations", _ridge)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.NO
        assert detail["reason"] == "terrain_blocks_line_of_sight"
        assert detail["shoreline_visible"] is False
        assert detail["sea_probe"]["visible"] is False

    def test_one_hole_in_the_model_is_not_a_sea(self, monkeypatch):
        """`None` is open water *and* a gap in EU-DEM's coverage.

        A single null behind the brow must not read as the Atlantic, which is
        why a run of them is required.
        """
        monkeypatch.setattr(
            svc,
            "fetch_coastline_points",
            lambda lat, lon, session=None: _coast_arc(range(0, 360, 20), 2000.0),
        )

        def _one_hole_per_ray(points, session=None):
            out = [40.0]
            per_ray = svc.SEA_PROBE_SAMPLES_PER_RAY
            for index in range(len(points) - 1):
                # A single null in the middle of otherwise blocking ground.
                out.append(None if index % per_ray == 9 else 300.0)
            return out

        monkeypatch.setattr(svc, "fetch_elevations", _one_hole_per_ray)
        detail = svc.evaluate_geometry(PLOT_LAT, PLOT_LON, "precise", use_cache=False)

        assert detail["state"] == svc.NO
        assert detail["sea_probe"]["visible"] is False


class TestTheVisibilityTestIsTheSameArithmeticAsTheProfile:
    @pytest.mark.parametrize("target_m", [500.0, 2000.0, 8000.0])
    def test_a_sample_blocks_a_target_exactly_when_the_profile_says_so(self, target_m):
        """`_sight_slope` is the profile's own test, divided through by `d`.

        Written as a slope so a ray can be walked once with a running maximum;
        if the two ever disagree, one of the sight lines is wrong and the
        blocked branch and the fan would answer different questions.
        """
        observer_height = 50.2
        for sample_m, elevation in ((91.1, 41.0), (250.0, 30.0), (400.0, 5.0)):
            if sample_m >= target_m:
                continue
            fraction = sample_m / target_m
            profile_blocks = (
                elevation - svc._curvature_drop_m(sample_m)
                > observer_height * (1.0 - fraction) + svc.TERRAIN_CLEARANCE_M
            )
            slope_blocks = (
                svc._sight_slope(elevation, sample_m, observer_height)
                > -observer_height / target_m
            )

            assert profile_blocks == slope_blocks

    def test_the_running_maximum_finds_the_nearest_visible_water(self):
        distances = [50.0, 100.0, 200.0, 400.0, 800.0, 1600.0]
        # A brow at 100 m hides the water at 200 and 400; the sea at 800 and
        # 1600 clears it.
        elevations = [10.0, 45.0, None, None, None, None]

        seen = svc._first_visible_water_m(distances, elevations, 50.0)

        assert seen is not None
        assert seen > 400.0
        assert math.isclose(seen, 800.0)


class TestTheCardSurvivesAHandEditedBlock:
    """`enrichment` is a JSON column and direct SQL is a supported workflow
    here, so a geometry block is untrusted input. `routes/main_routes.py`
    turns a template error into a flash and a second render with no rows, so
    the assertion has to be that the page *rendered* -- an absent line looks
    identical either way.
    """

    @pytest.fixture
    def app(self):
        from tests import setup_test_environment

        setup_test_environment()
        from app import create_app, db

        application = create_app()
        application.config["TESTING"] = True
        application.config["WTF_CSRF_ENABLED"] = False
        with application.app_context():
            db.create_all()
            yield application
            db.drop_all()

    def test_a_block_missing_its_distance_still_renders_the_page(self, app):
        from app import db
        from models import Property

        listing = Property(
            source_email_id="sea-fan-handedited",
            title="Seiruga hand-edited",
            municipality="Malpica",
            location_lat=PLOT_LAT,
            location_lon=PLOT_LON,
            enrichment={
                "environment": {
                    "sea_view": "no",
                    "sea_view_detail": {
                        "source": "geometry",
                        "reason": "terrain_blocks_line_of_sight",
                        "geometry": {
                            "distance_m": 394.7,
                            "observer_elevation_m": 45.2,
                            # Hand-edited: the flag arrives without its number.
                            "shoreline_visible": False,
                        },
                    },
                }
            },
        )
        db.session.add(listing)
        db.session.commit()

        response = app.test_client().get(f"/properties/{listing.id}")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "terrain_blocks_line_of_sight" in body
        assert "0.4 km to the coastline" in body

    def test_a_probe_with_a_non_numeric_distance_still_renders(self, app):
        """The sibling guard, which the first fix missed.

        `blocked_at_m` got `is number` and `visible_at_m` kept `is not none`,
        in the same block, in the same commit -- so a string there still threw
        into the redirect. Found by review, not by a mutation: one writer sets
        both, so no exercise of the writer can produce the shape.
        """
        from app import db
        from models import Property

        listing = Property(
            source_email_id="sea-fan-handedited-probe",
            title="Seiruga hand-edited probe",
            municipality="Malpica",
            location_lat=PLOT_LAT,
            location_lon=PLOT_LON,
            enrichment={
                "environment": {
                    "sea_view": "likely",
                    "sea_view_detail": {
                        "source": "geometry",
                        "reason": "sea_visible_beyond_terrain",
                        "geometry": {
                            "distance_m": 394.7,
                            "observer_elevation_m": 45.2,
                            "shoreline_visible": False,
                            "blocked_at_m": 91.1,
                            "sea_probe": {
                                "visible": True,
                                "visible_at_m": "673,1",  # hand-edited
                            },
                        },
                    },
                }
            },
        )
        db.session.add(listing)
        db.session.commit()

        response = app.test_client().get(f"/properties/{listing.id}")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "sea_visible_beyond_terrain" in body
        # The half that *is* readable is still shown.
        assert "91 m" in body
