"""A locality centroid may not be scored as if it were the parcel.

The fixture is not invented. Properties 460, 461, 574 and 641 on the live
database all sit on 43.5723710, -5.9963786, which is where Google put four
different queries --

    Lugar San Adriano, 17, Santa María del Mar, Castrillón, Spain
    Lugar San Adriano, 19, Santa María del Mar, Castrillón, Spain
    Calle San Adriano, Santa María del Mar - El Puerto, Castrillón, Spain
    San Miguel de Quiloño s/n, Santa María del Mar, Castrillón, Spain

-- every one answered `Santa María del Mar, 33457 Castrillón, Asturias, Spain`
and labelled `approximate`. That point is **23.8 m** from the OSM coastline, so
the sea-distance score of all four was the shoreline's own, including the last
one, whose address is San Miguel de Quiloño: inland, by the airport.

Measured 2026-08-16 on 652 located rows: 466 approximate against 186 precise,
229 rows sharing a coordinate with another listing across 67 points, worst
point 16 listings.
"""

from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import property_travel_service as travel_module
from services import sea_distance_service as sds
from services.coordinate_quality import (
    APPROXIMATE_COORD_SLACK_M,
    shared_coordinate_peers,
)
from services.property_scoring_service import HousingPropertyScorer
from services.property_travel_service import (
    TRAVEL_STATE_APPROXIMATE_ORIGIN,
    PropertyTravelService,
    effective_travel_state,
)
from services.sea_distance_service import (
    SEARCH_RADIUS_M,
    STATUS_APPROXIMATE_ORIGIN,
    STATUS_NO_COASTLINE,
    STATUS_OK,
    SeaDistanceService,
    parcel_measurement,
)
from tests import setup_test_environment
from utils.cache import cache

# The shared point, and the coastline node 23.8 m from it. 23.8 m north is
# 0.000214 degrees of latitude.
CLUSTER_LAT = 43.5723710
CLUSTER_LON = -5.9963786
COAST_NODE = (CLUSTER_LAT + 0.000214, CLUSTER_LON)

# The four listings' own queries, kept verbatim: they are the evidence that one
# point is standing in for four addresses.
CLUSTER_QUERIES = {
    460: "Lugar San Adriano, 17, Santa María del Mar, Castrillón, Spain",
    461: "Lugar San Adriano, 19, Santa María del Mar, Castrillón, Spain",
    574: "Calle San Adriano, Santa María del Mar - El Puerto, Castrillón, Spain",
    641: "San Miguel de Quiloño s/n, Santa María del Mar, Castrillón, Spain",
}

# What Google actually returned for all four.
CLUSTER_GEOCODE = {
    "formatted_address": "Santa María del Mar, 33457 Castrillón, Asturias, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "lat": CLUSTER_LAT,
    "lng": CLUSTER_LON,
    "address_components": [
        {"types": ["postal_code"], "long_name": "33457"},
        {"types": ["locality", "political"], "long_name": "Santa María del Mar"},
    ],
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        cache.clear()
        yield app
        db.drop_all()


def _patch_coastline(monkeypatch, points):
    monkeypatch.setattr(sds, "fetch_coastline_points", lambda lat, lon, **kw: points)


def _listing(**kwargs):
    defaults = {
        "source_email_id": f"cluster-{kwargs.get('title', 'x')}",
        "property_category": "housing",
        "property_subtype": "house",
        "municipality": "Castrillón",
        "price": Decimal("150000"),
        "area": Decimal("120"),
        "location_lat": Decimal(str(CLUSTER_LAT)),
        "location_lon": Decimal(str(CLUSTER_LON)),
        "location_accuracy": "approximate",
    }
    defaults.update(kwargs)
    prop = Property(**defaults)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheGuardsThatAlreadyExistLetThisThrough:
    """Right place, right province, right scale -- and the wrong point."""

    def test_all_three_existing_geocode_guards_pass_the_cluster(self, app):
        from services.property_location_service import (
            _is_too_coarse,
            _municipality_agreement,
        )

        prop = _listing(title="Castrillón listing")

        # #331: a locality is not a country, so the size rule accepts it.
        assert _is_too_coarse(CLUSTER_GEOCODE) is False
        # #348: 33457 and Castrillón are the same province, so the place rule
        # does not merely abstain -- it actively agrees.
        state, row_province, result_province = _municipality_agreement(
            prop, CLUSTER_GEOCODE
        )
        assert (state, row_province, result_province) == ("agreed", "33", "33")
        # #321: `approximate` is a label the whitelist keeps as it is.
        assert CLUSTER_GEOCODE["accuracy"] == "approximate"


class TestSeaDistanceRefusesTheCentroid:
    def test_the_centroid_is_measured_but_not_claimed_as_the_parcel(
        self, app, monkeypatch
    ):
        _patch_coastline(monkeypatch, [COAST_NODE])
        prop = _listing(title="San Miguel de Quiloño s/n")

        payload = SeaDistanceService().update_property(prop)

        assert payload["status"] == STATUS_APPROXIMATE_ORIGIN
        # The key that means "how far this property is from the sea" is empty,
        # because nobody measured that.
        assert payload["distance_m"] is None
        assert payload["origin_distance_m"] == pytest.approx(23.8, abs=1.0)
        assert payload["min_distance_m"] == 0.0
        assert payload["max_distance_m"] == pytest.approx(
            23.8 + APPROXIMATE_COORD_SLACK_M, abs=1.0
        )

    def test_the_same_geometry_from_a_precise_point_is_measured(self, app, monkeypatch):
        """The control: only the label differs, and it decides everything."""
        _patch_coastline(monkeypatch, [COAST_NODE])
        prop = _listing(title="surveyed address", location_accuracy="precise")

        payload = SeaDistanceService().update_property(prop)

        assert payload["status"] == STATUS_OK
        assert payload["distance_m"] == pytest.approx(23.8, abs=1.0)

    def test_the_sea_score_drops_out_instead_of_landing_at_the_shoreline(
        self, app, monkeypatch
    ):
        _patch_coastline(monkeypatch, [COAST_NODE])
        approximate = _listing(title="centroid row")
        precise = _listing(
            title="surveyed row",
            source_email_id="cluster-precise",
            location_accuracy="precise",
        )
        service = SeaDistanceService()
        service.update_property(approximate)
        service.update_property(precise)

        scorer = HousingPropertyScorer()
        centroid_score, centroid_meta = scorer._sea_score(
            approximate, near_m=300.0, far_m=10000.0
        )
        parcel_score, _ = scorer._sea_score(precise, near_m=300.0, far_m=10000.0)

        # None, so `_weighted_average` renormalises without it. Never 0: zero
        # is a measured claim about a shoreline nobody measured from.
        assert centroid_score is None
        assert centroid_meta["status"] == STATUS_APPROXIMATE_ORIGIN
        assert parcel_score is not None and parcel_score > 95

    def test_a_stored_ok_from_before_this_rule_is_restated_on_read(self, app):
        """The 228 live rows whose block says `ok` about a centroid.

        Rewriting them is a free Overpass recalc, but the score must not wait
        for one, so the restatement happens where the score is read.
        """
        prop = _listing(title="legacy payload")
        prop.enrichment = {
            "sea": {
                "status": STATUS_OK,
                "distance_m": 23.8,
                "searched_m": SEARCH_RADIUS_M,
                "source": "osm_coastline",
                "origin": {"lat": CLUSTER_LAT, "lon": CLUSTER_LON},
            }
        }
        db.session.commit()

        restated = parcel_measurement(prop)
        score, _ = HousingPropertyScorer()._sea_score(prop, near_m=300.0, far_m=10000.0)

        assert restated["status"] == STATUS_APPROXIMATE_ORIGIN
        assert restated["origin_distance_m"] == 23.8
        assert score is None


class TestTheSlackStillAllowsAnAnswerItCannotChange:
    """`sea_view_service`'s exemption, not a second policy."""

    def test_no_coastline_anywhere_near_the_locality_still_scores_zero(
        self, app, monkeypatch
    ):
        _patch_coastline(monkeypatch, [])
        prop = _listing(title="inland")

        payload = SeaDistanceService().update_property(prop)
        score, meta = HousingPropertyScorer()._sea_score(
            prop, near_m=300.0, far_m=10000.0
        )

        assert payload["status"] == STATUS_NO_COASTLINE
        # 17 km was searched around the centroid; the parcel may sit 5 km
        # outside it, so 12 km is what the answer is guaranteed for -- and the
        # profile only asks about 10.
        assert payload["searched_m"] == SEARCH_RADIUS_M - APPROXIMATE_COORD_SLACK_M
        assert score == 0.0
        assert meta["status"] == STATUS_NO_COASTLINE

    def test_a_horizon_past_the_shrunken_guarantee_scores_nothing(
        self, app, monkeypatch
    ):
        """A profile asking about 15 km is asking about ground nobody covered."""
        _patch_coastline(monkeypatch, [])
        prop = _listing(title="inland, wide horizon")
        SeaDistanceService().update_property(prop)

        score, meta = HousingPropertyScorer()._sea_score(
            prop, near_m=300.0, far_m=15000.0
        )

        assert score is None
        assert meta["status"] == "horizon_exceeds_search"


class TestTravelSpendsNothingOnACentroid:
    def _profile(self):
        profile = SearchProfile(
            name="Castrillón",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {"hospital": {"enabled": True, "mode": "driving"}},
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()
        return profile

    def test_no_places_and_no_distance_matrix_call_is_made(self, app, monkeypatch):
        profile = self._profile()
        prop = _listing(title="centroid row", search_profile_id=profile.id)

        def explode(*args, **kwargs):
            raise AssertionError("Google was called for an approximate origin")

        monkeypatch.setattr(travel_module, "request_with_retries", explode)

        ok = PropertyTravelService().calculate_for_property(prop, commit=True)

        assert ok is False
        assert prop.travel["api_status"]["state"] == TRAVEL_STATE_APPROXIMATE_ORIGIN
        assert prop.travel["api_status"]["origin_accuracy"] == "approximate"

    def test_durations_already_bought_are_kept_not_deleted(self, app, monkeypatch):
        """#350 clears a block with no origin; this row has one, just not its own."""
        profile = self._profile()
        prop = _listing(title="already measured", search_profile_id=profile.id)
        prop.travel = {
            "targets": {"hospital": {"duration_min": 27, "mode": "driving"}},
            "api_status": {"state": "ok"},
        }
        db.session.commit()

        monkeypatch.setattr(
            travel_module,
            "request_with_retries",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called Google")),
        )
        PropertyTravelService().calculate_for_property(prop, commit=True)

        assert prop.travel["targets"]["hospital"]["duration_min"] == 27
        assert prop.travel["api_status"]["state"] == TRAVEL_STATE_APPROXIMATE_ORIGIN

    def test_the_state_a_reader_sees_comes_from_the_row_not_the_old_run(self, app):
        """466 rows carry `state: ok` from a run that predates this rule."""
        prop = _listing(title="stale ok")
        prop.travel = {"api_status": {"state": "ok"}, "targets": {}}
        db.session.commit()

        assert effective_travel_state(prop) == TRAVEL_STATE_APPROXIMATE_ORIGIN

    def test_a_precise_row_is_still_measured(self, app, monkeypatch):
        profile = self._profile()
        prop = _listing(
            title="surveyed row",
            search_profile_id=profile.id,
            location_accuracy="precise",
        )

        monkeypatch.setattr(
            PropertyTravelService,
            "_nearest_place_for_preset",
            lambda self, lat, lon, key, defs: travel_module.PlaceLookup(
                place={
                    "place_id": "h1",
                    "name": "Hospital San Agustín",
                    "lat": 43.5,
                    "lon": -5.9,
                }
            ),
        )
        monkeypatch.setattr(
            PropertyTravelService,
            "_beach_candidates",
            lambda self, lat, lon: travel_module.BeachLookup(places=[], total_found=0),
        )
        monkeypatch.setattr(
            PropertyTravelService,
            "_get_distances",
            lambda self, lat, lon, destinations, mode: [
                travel_module.DistanceResult(distance_m=20000, duration_s=1620)
                for _ in destinations
            ],
        )

        ok = PropertyTravelService().calculate_for_property(prop, commit=True)

        assert ok is True
        assert prop.travel["targets"]["hospital"]["duration_min"] == 27


class TestTravelMinutesFromACentroidDoNotScore:
    def _profile(self):
        profile = SearchProfile(
            name="Castrillón",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {"hospital": {"enabled": True, "mode": "driving"}},
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()
        return profile

    def _scored(self, prop, profile, minutes):
        prop.travel = {
            "targets": {"hospital": {"duration_min": minutes, "mode": "driving"}},
            "api_status": {"state": "ok"},
        }
        db.session.commit()
        return HousingPropertyScorer()._travel_score(
            prop, profile, best=10.0, worst=60.0
        )

    def test_a_duration_inside_the_band_scores_nothing(self, app):
        profile = self._profile()
        prop = _listing(title="centroid row", search_profile_id=profile.id)

        score, meta = self._scored(prop, profile, 30)

        # 5 km of positional error is ~6.7 driving minutes, and anywhere in
        # 23.3-36.7 minutes the score moves. There is no honest number.
        assert score is None
        assert meta["status"] == STATUS_APPROXIMATE_ORIGIN
        assert meta["origin_accuracy"] == "approximate"

    def test_a_duration_past_the_worst_bound_still_scores_zero(self, app):
        profile = self._profile()
        prop = _listing(title="far from everything", search_profile_id=profile.id)

        score, _ = self._scored(prop, profile, 90)

        # 83.3 and 96.7 minutes are both past `worst`; the slack changes
        # nothing, so the answer is given.
        assert score == 0.0

    def test_the_same_duration_from_a_precise_point_scores(self, app):
        profile = self._profile()
        prop = _listing(
            title="surveyed row",
            search_profile_id=profile.id,
            location_accuracy="precise",
        )

        score, meta = self._scored(prop, profile, 30)

        assert score == pytest.approx(60.0)
        assert meta["status"] == "ok"


class TestThePageSaysSoRatherThanHidingTheRow:
    """Scope item 3: the listing stays, the derived number stops asserting.

    Rendered, not asserted against the template's source: `/properties/<id>`
    turns a `TemplateSyntaxError` into a redirect (#283), so a page test that
    does not check the status code proves nothing.
    """

    def _profile(self):
        profile = SearchProfile(
            name="Castrillón",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {"hospital": {"enabled": True, "mode": "driving"}},
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()
        return profile

    def test_the_detail_page_reads_not_measured_and_names_the_reason(self, app):
        profile = self._profile()
        prop = _listing(title="centroid row", search_profile_id=profile.id)
        prop.enrichment = {
            "sea": {
                "status": STATUS_OK,
                "distance_m": 23.8,
                "searched_m": SEARCH_RADIUS_M,
                "origin": {"lat": CLUSTER_LAT, "lon": CLUSTER_LON},
            }
        }
        prop.travel = {
            "targets": {"hospital": {"duration_min": 27, "mode": "driving"}},
            "api_status": {"state": "ok"},
        }
        db.session.commit()
        property_id = prop.id

        response = app.test_client().get(f"/properties/{property_id}")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Not measured (approximate location)" in body
        assert "locality centroid" in body
        # What was measured is still shown, in metres and attributed to the
        # geocoded point. Rounding it to km reads "0.0 km", which is the
        # shoreline claim this change exists to stop, in a tooltip.
        assert "23.8 m from the geocoded point" in body
        assert "0.0 km" not in body

    def test_the_list_keeps_the_row_and_marks_the_origin(self, app):
        profile = self._profile()
        _listing(title="centroid row", search_profile_id=profile.id)

        response = app.test_client().get("/properties")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "centroid row" in body
        assert "Approximate location" in body

    def test_a_precise_row_shows_its_distance_unqualified(self, app):
        profile = self._profile()
        prop = _listing(
            title="surveyed row",
            search_profile_id=profile.id,
            location_accuracy="precise",
        )
        prop.enrichment = {
            "sea": {
                "status": STATUS_OK,
                "distance_m": 2500.0,
                "searched_m": SEARCH_RADIUS_M,
                "origin": {"lat": CLUSTER_LAT, "lon": CLUSTER_LON},
            }
        }
        db.session.commit()
        property_id = prop.id

        response = app.test_client().get(f"/properties/{property_id}")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "2.5 km" in body
        assert "Not measured (approximate location)" not in body


class TestSharedCoordinatesAreSurfaced:
    def test_four_listings_on_one_point_name_each_other(self, app):
        rows = [
            _listing(title=query, source_email_id=f"cluster-{listing_id}")
            for listing_id, query in CLUSTER_QUERIES.items()
        ]
        elsewhere = _listing(
            title="somewhere else",
            source_email_id="cluster-other",
            location_lat=Decimal("43.4000000"),
            location_lon=Decimal("-5.8000000"),
        )

        peers = shared_coordinate_peers(rows[0])

        assert peers == [row.id for row in rows[1:]]
        assert shared_coordinate_peers(elsewhere) == []

    def test_the_report_counts_what_a_re_geocode_would_unlock(self, app):
        """`utils/report_coordinate_quality` spends nothing and writes nothing.

        It is the "report the affected row count and stop" half of this
        change: repairing the rows is billed, so it stays the owner's call.
        """
        from utils.report_coordinate_quality import collect

        cluster = [
            _listing(title=query, source_email_id=f"cluster-{listing_id}")
            for listing_id, query in CLUSTER_QUERIES.items()
        ]
        for prop in cluster:
            prop.enrichment = {"sea": {"status": STATUS_OK, "distance_m": 23.8}}
            prop.travel = {"targets": {}, "api_status": {"state": "ok"}}
        surveyed = _listing(
            title="surveyed row",
            source_email_id="cluster-precise",
            location_lat=Decimal("43.4000000"),
            location_accuracy="precise",
        )
        surveyed.enrichment = {"sea": {"status": STATUS_OK, "distance_m": 900.0}}
        db.session.commit()

        report = collect(cluster + [surveyed], cluster_limit=1)

        assert report["located_rows"] == 5
        assert report["by_accuracy"] == {"approximate": 4, "precise": 1}
        assert report["sea_distance_unattributable"] == 4
        assert report["travel_blocks_unattributable"] == 4
        assert report["rows_sharing_a_point"] == 4
        assert report["shared_points"] == 1
        assert report["largest_clusters"][0]["count"] == 4

    def test_it_is_evidence_and_not_a_gate(self, app, monkeypatch):
        """A precise row sharing a point keeps its measurement.

        39 of the 229 rows sharing a coordinate are labelled `precise`, and two
        flats in one building really do share one. The coordinate alone cannot
        tell that from four plots on a centroid, so this is shown, not scored.
        """
        _patch_coastline(monkeypatch, [COAST_NODE])
        first = _listing(title="flat A", location_accuracy="precise")
        second = _listing(
            title="flat B",
            source_email_id="cluster-flat-b",
            location_accuracy="precise",
        )

        payload = SeaDistanceService().update_property(first)

        assert shared_coordinate_peers(first) == [second.id]
        assert payload["status"] == STATUS_OK
