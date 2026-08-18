"""The travel presets are answered from OpenStreetMap, for free and honestly.

Step 2 of the plan agreed after the EUR 190 Google invoice of 1-18 August
2026. Five of the seven Places calls a listing cost are these presets; the
hospital went to the national register in step 1.

Every number and name below was measured against six real production
coordinates through the live Overpass instance before any of this was written,
and the measurement is what the tests encode:

* at Oviedo the nearest `aeroway=aerodrome` is *Aeródromo de La Morgal*, 9.2
  km -- exactly the class of thing #171 spent a ticket teaching this
  repository to refuse in Google's `airport` type -- and the shipped
  `require_name_patterns` refuse it and every other aeroclub while accepting
  *Aeropuerto de Asturias* (OVD) and *Aeroporto da Coruña* (LCG). On all six
  coordinates that is the airport Google named too;
* Cariño resolves A Coruña at **64.3 km**, past the ~50 km ceiling that made
  `wide_search_query` and its second paid call necessary (#254);
* Google's `police` answered *Traffic radar* for property 101 and a private
  security firm for property 67, where `amenity=police` gives the Comisaría
  and the Cuartel.

What the tests are *for* is the three ways this could quietly stop being free
or stop being honest: a refusal turning into "nothing nearby", a refusal
turning into a paid fallback, and five presets turning back into five
round trips.
"""

import pytest

from services import osm_places
from services.search_profile_service import TRAVEL_PRESET_DEFS
from utils.google_api import GoogleApiFailure

# Measured at 43.3561224,-5.8763042 (property 67, Oviedo).
OVIEDO = (43.3561224, -5.8763042)
LA_MORGAL = {"name": "Aeródromo de La Morgal", "lat": 43.4278, "lon": -5.8306}
ASTURIAS_AIRPORT = {"name": "Aeropuerto de Asturias", "lat": 43.5636, "lon": -6.0348}
AEROCLUB = {"name": "Aeroclub Arnao", "lat": 43.58, "lon": -6.9}


class _Transport:
    """Stands in for `EnrichmentService._overpass_elements`."""

    def __init__(self, elements=None, failure=None):
        self.elements = elements
        self.failure = failure
        self.queries = []

    def _overpass_elements(self, query):
        self.queries.append(query)
        if self.failure is not None:
            return None, self.failure
        return list(self.elements or []), None


def _node(place, tag_key, tag_value, **extra):
    tags = {"name": place["name"], tag_key: tag_value}
    tags.update(extra)
    return {
        "type": "node",
        "id": 1,
        "lat": place["lat"],
        "lon": place["lon"],
        "tags": tags,
    }


@pytest.fixture
def app():
    from app import create_app, db
    from tests import setup_test_environment

    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.drop_all()


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch):
    """conftest stubs this module for every other suite; here it is the subject."""
    import importlib

    monkeypatch.setattr(
        osm_places, "lookup_candidates", importlib.reload(osm_places).lookup_candidates
    )
    yield


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Each test asks Overpass for itself rather than reading a neighbour's answer."""
    monkeypatch.setattr(osm_places, "get_cached_enrichment_data", lambda *a, **k: None)
    monkeypatch.setattr(osm_places, "cache_enrichment_data", lambda *a, **k: None)


class TestTheSpecComesFromThePreset:
    def test_a_preset_without_a_tag_is_not_answered_here(self):
        assert osm_places.osm_spec({"label": "x"}) is None

    def test_a_malformed_tag_is_refused_rather_than_guessed(self):
        assert osm_places.osm_spec({"osm_tag": "aeroway", "osm_radius_m": 100}) is None
        assert osm_places.osm_spec({"osm_tag": "a=b"}) is None
        assert osm_places.osm_spec({"osm_tag": "a=b", "osm_radius_m": 0}) is None

    def test_the_five_shipped_presets_declare_one(self):
        answered = {
            key
            for key, spec in TRAVEL_PRESET_DEFS.items()
            if osm_places.osm_spec(spec) is not None
        }
        assert answered == {
            "airport",
            "train_station",
            "supermarket",
            "school",
            "police",
        }
        # The sixth is the national register's, and must not also be here:
        # two sources for one preset is two answers that can disagree.
        assert osm_places.osm_spec(TRAVEL_PRESET_DEFS["hospital"]) is None


class TestOneQueryAnswersEveryPreset:
    def test_all_declared_types_ride_in_a_single_round_trip(self):
        specs = {
            key: osm_places.osm_spec(spec)
            for key, spec in TRAVEL_PRESET_DEFS.items()
            if osm_places.osm_spec(spec) is not None
        }
        transport = _Transport(elements=[])

        osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert len(transport.queries) == 1, (
            "five presets must cost one Overpass round trip, not five: the gate "
            "is 5 s and a bulk run pays it per request"
        )
        query = transport.queries[0]
        for tag in (
            '"aeroway"="aerodrome"',
            '"railway"="station"',
            '"amenity"="police"',
        ):
            assert tag in query
        # Ways and relations carry the station buildings and the aerodrome
        # polygons; without `out center` they have no point to route from.
        assert "out center tags" in query

    def test_a_candidate_outside_its_own_radius_is_dropped(self):
        """One query, several radii: the union asks wide and the filter narrows."""
        specs = {"supermarket": ("shop", "supermarket", 15_000)}
        far = {"name": "Far Market", "lat": 44.6, "lon": -5.87}
        transport = _Transport(elements=[_node(far, "shop", "supermarket")])

        found, failure = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert failure is None
        assert found["supermarket"] == []


class TestTheAirportRulesOfIssue171:
    def _airport_candidates(self, elements):
        specs = {"airport": osm_places.osm_spec(TRAVEL_PRESET_DEFS["airport"])}
        transport = _Transport(elements=elements)
        found, _ = osm_places.lookup_candidates(transport, specs, *OVIEDO)
        return found["airport"]

    def test_the_aerodrome_is_refused_and_the_airport_taken(self):
        candidates = self._airport_candidates(
            [
                _node(LA_MORGAL, "aeroway", "aerodrome"),
                _node(ASTURIAS_AIRPORT, "aeroway", "aerodrome"),
                _node(AEROCLUB, "aeroway", "aerodrome"),
            ]
        )
        # Nearest first, so the rules have to walk past the refused one.
        assert candidates[0]["name"] == "Aeródromo de La Morgal"

        chosen = osm_places.pick(TRAVEL_PRESET_DEFS["airport"], candidates)
        assert chosen["name"] == "Aeropuerto de Asturias"

    def test_only_aerodromes_are_a_real_absence(self):
        """Every candidate refused is an answer, not a failure (#98)."""
        candidates = self._airport_candidates(
            [
                _node(LA_MORGAL, "aeroway", "aerodrome"),
                _node(AEROCLUB, "aeroway", "aerodrome"),
            ]
        )
        assert osm_places.pick(TRAVEL_PRESET_DEFS["airport"], candidates) is None

    def test_a_preset_with_no_rules_takes_the_nearest(self):
        market = {"name": "Alimerka", "lat": 43.357, "lon": -5.877}
        specs = {"supermarket": osm_places.osm_spec(TRAVEL_PRESET_DEFS["supermarket"])}
        transport = _Transport(elements=[_node(market, "shop", "supermarket")])
        found, _ = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        chosen = osm_places.pick(
            TRAVEL_PRESET_DEFS["supermarket"], found["supermarket"]
        )
        assert chosen["name"] == "Alimerka"


class TestARefusalIsNotAnAbsence:
    def test_overpass_refusing_comes_back_as_a_failure(self):
        specs = {"airport": ("aeroway", "aerodrome", 100_000)}
        transport = _Transport(failure=GoogleApiFailure(reason="http_error"))

        found, failure = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert found is None, "None is 'we do not know', {} would be 'nothing here'"
        assert failure is not None

    def test_a_refusal_is_never_cached(self, monkeypatch):
        """A cached refusal would answer "nothing nearby" for a month."""
        written = []
        monkeypatch.setattr(
            osm_places,
            "cache_enrichment_data",
            lambda lat, lon, key, value, timeout=None: written.append(key),
        )
        specs = {"airport": ("aeroway", "aerodrome", 100_000)}
        transport = _Transport(failure=GoogleApiFailure(reason="http_error"))

        osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert written == []

    def test_an_empty_answer_is_cached(self, monkeypatch):
        """ "Overpass looked and there is no station here" is a measurement."""
        written = []
        monkeypatch.setattr(
            osm_places,
            "cache_enrichment_data",
            lambda lat, lon, key, value, timeout=None: written.append(key),
        )
        specs = {"train_station": ("railway", "station", 30_000)}
        transport = _Transport(elements=[])

        found, failure = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert failure is None
        assert found == {"train_station": []}
        assert len(written) == 1


class TestWhatIsRecorded:
    def test_the_place_says_where_it_came_from(self):
        specs = {"police": osm_places.osm_spec(TRAVEL_PRESET_DEFS["police"])}
        station = {
            "name": "Comisaría de la Policía Local",
            "lat": 43.357,
            "lon": -5.877,
        }
        transport = _Transport(elements=[_node(station, "amenity", "police")])

        found, _ = osm_places.lookup_candidates(transport, specs, *OVIEDO)
        place = found["police"][0]

        assert place["source"] == "osm"
        assert place["osm_type"] == "node"
        # No `place_id`: a Google id invented here would send the map link to
        # a listing that does not exist.
        assert "place_id" not in place

    def test_an_unnamed_school_keeps_its_absence_of_a_name(self):
        """OSM leaves many schools unnamed. A school is still a school, and a\n        name invented from the tag would be a fact nobody recorded."""
        specs = {"school": osm_places.osm_spec(TRAVEL_PRESET_DEFS["school"])}
        element = {
            "type": "way",
            "id": 7,
            "center": {"lat": 43.357, "lon": -5.877},
            "tags": {"amenity": "school"},
        }
        transport = _Transport(elements=[element])

        found, _ = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert found["school"][0]["name"] is None

    def test_a_way_is_read_through_its_centre(self):
        specs = {
            "train_station": osm_places.osm_spec(TRAVEL_PRESET_DEFS["train_station"])
        }
        element = {
            "type": "way",
            "id": 9,
            "center": {"lat": 43.3571, "lon": -5.8771},
            "tags": {"railway": "station", "name": "Las Campas"},
        }
        transport = _Transport(elements=[element])

        found, _ = osm_places.lookup_candidates(transport, specs, *OVIEDO)

        assert found["train_station"][0]["lat"] == 43.3571


class TestTheResolverUsesIt:
    """The wiring, not the module: a green unit suite over a dead hook is the
    defect this repository keeps finding (#309), so the seam has its own tests.
    """

    def _service(self, monkeypatch, transport):
        from services.property_travel_service import PropertyTravelService

        service = PropertyTravelService(
            google_maps_key="unused-key", google_places_key="unused-key"
        )
        service.enrichment_service = transport

        def explode(*args, **kwargs):
            raise AssertionError("a paid Places request was made")

        import services.property_travel_service as travel_module

        monkeypatch.setattr(travel_module, "request_with_retries", explode)
        return service

    def test_a_shipped_preset_resolves_from_osm_without_touching_google(
        self, app, monkeypatch
    ):
        transport = _Transport(
            elements=[
                _node(LA_MORGAL, "aeroway", "aerodrome"),
                _node(ASTURIAS_AIRPORT, "aeroway", "aerodrome"),
            ]
        )
        service = self._service(monkeypatch, transport)
        with app.app_context():
            lookup = service._nearest_place_for_preset(
                OVIEDO[0], OVIEDO[1], "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.failure is None
        assert lookup.place["name"] == "Aeropuerto de Asturias"
        assert lookup.place["source"] == "osm"
        assert lookup.place["preset_key"] == "airport"
        assert len(transport.queries) == 1

    def test_an_overpass_refusal_does_not_fall_through_to_the_paid_search(
        self, app, monkeypatch
    ):
        """The airport preset still carries `wide_search_query`. It must not fire:
        falling through would spend exactly when the free source is down."""
        transport = _Transport(failure=GoogleApiFailure(reason="http_error"))
        service = self._service(monkeypatch, transport)
        with app.app_context():
            lookup = service._nearest_place_for_preset(
                OVIEDO[0], OVIEDO[1], "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is None
        assert lookup.failure is not None

    def test_nothing_qualifying_is_an_absence_and_not_a_failure(self, app, monkeypatch):
        transport = _Transport(elements=[_node(LA_MORGAL, "aeroway", "aerodrome")])
        service = self._service(monkeypatch, transport)
        with app.app_context():
            lookup = service._nearest_place_for_preset(
                OVIEDO[0], OVIEDO[1], "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is None
        assert lookup.failure is None, (
            "every aerodrome refused is Overpass answering, not Overpass refusing"
        )
