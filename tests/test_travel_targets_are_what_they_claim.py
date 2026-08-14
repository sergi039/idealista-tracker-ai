"""What the travel card measures, and what it admits it measured.

Three defects, all found by reading property 356 against the database.

* **The nearest "airport" was a contractor.** Google's `airport` place type
  covers helipads and anything else that carries the tag, and the lookup took
  `results[0]` unconditionally. Across the owner's 188 geocoded listings that
  produced a helipad for 107 -- hospital helipads among them -- some unrelated
  business for 59, and something actually named an airport for 22. Property
  356 read "Nearest Airport 10 min, 2.4 km", measured to "GlueWay System",
  while Asturias Airport is roughly 40 km away. It is a scoring input, so the
  wrong number does not stop at the page.
* **A walking time nobody measured.** The Transport card printed
  `car_minutes * 4` behind a walking icon whenever the product came to 15
  minutes or less -- an invented figure shown as a measurement, on 45
  listings for the hospital alone.
* **Measured targets the page never showed.** The card looped over exactly
  `airport`, `train_station` and `hospital`. School (354 listings) and
  supermarket (330) were resolved, stored, quoted by Claude in its analysis
  -- "Immediate access to Alimerka supermarket (0 min)" -- and rendered
  nowhere.
"""

import re
from unittest.mock import patch

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.property_travel_service import (  # noqa: E402
    PropertyTravelService,
    _place_rules,
)
from services.search_profile_service import TRAVEL_PRESET_DEFS  # noqa: E402


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


def _place(name, types):
    """A Nearby Search result, shaped as Google returns it."""
    return {
        "name": name,
        "place_id": f"place-{name}",
        "types": types,
        "geometry": {"location": {"lat": 43.5, "lng": -5.6}},
    }


# Verbatim from the owner's database.
GLUEWAY = _place(
    "GlueWay System",
    ["airport", "general_contractor", "point_of_interest", "establishment"],
)
HOSPITAL_HELIPAD = _place(
    "Helipuerto Hospital Universitario de Cabueñes", ["airport", "establishment"]
)
FLYING_FIELD = _place("Campo de Vuelo Capitan M RIVERA", ["airport", "establishment"])
HOTEL = _place("Hotel A Marisqueira", ["airport", "lodging", "establishment"])
REAL_AIRPORT = _place(
    "Asturias Airport", ["airport", "point_of_interest", "establishment"]
)


class TestThePresetRefusesWhatIsNotAnAirport:
    @pytest.mark.parametrize(
        "candidate", [GLUEWAY, HOSPITAL_HELIPAD, FLYING_FIELD, HOTEL]
    )
    def test_the_real_false_positives_are_refused(self, candidate):
        rules = _place_rules(TRAVEL_PRESET_DEFS["airport"])

        assert rules is not None
        assert rules.rejects(candidate), candidate["name"]

    @pytest.mark.parametrize(
        "name",
        ["Asturias Airport", "Aeropuerto de Alicante-Elche", "Santiago Airport"],
    )
    def test_an_actual_airport_is_accepted(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["airport"])

        assert not rules.rejects(_place(name, ["airport", "establishment"]))

    @pytest.mark.parametrize("preset", ["school", "police", "train_station"])
    def test_presets_without_rules_are_unchanged(self, preset):
        """These three resolve correctly and keep their cache keys."""
        assert _place_rules(TRAVEL_PRESET_DEFS[preset]) is None


class TestTheSupermarketIsAShopAndNotAPetrolStation:
    """The supermarket preset refuses by type and name, and requires nothing.

    Requiring the name to say "supermercado" would throw away Mercadona, Lidl
    and Alimerka, and a list of chains needs feeding forever. Google's tag is
    broadly right here -- 324 of the owner's 356 listings resolve to a real
    grocery shop. It is wrong in two narrow, identifiable ways: a petrol
    station with a shop ("bp" was the nearest supermarket for 21 listings)
    carries `gas_station`, and a butcher or fishmonger says so in its name
    (11 more).
    """

    def test_a_petrol_station_shop_is_refused_by_its_type(self):
        rules = _place_rules(TRAVEL_PRESET_DEFS["supermarket"])
        # Verbatim from the owner's database.
        bp = _place(
            "bp",
            [
                "gas_station",
                "cafe",
                "supermarket",
                "grocery_or_supermarket",
                "food",
                "store",
                "establishment",
            ],
        )

        assert rules.rejects(bp)

    @pytest.mark.parametrize(
        "name",
        [
            "Pescados GINER",
            "Carnicería y Administración de Lotería Marisa",
            "Frutería La Huerta",
            "Panadería Pastelería Rosal",
        ],
    )
    def test_a_specialty_shop_is_refused_by_its_name(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["supermarket"])

        assert rules.rejects(_place(name, ["supermarket", "food", "store"]))

    @pytest.mark.parametrize(
        "name",
        [
            "Supermercados Alimerka",
            "Mercadona",
            "Lidl",
            "masymas supermercados",
            "Eroski City",
            # A local grocery Google files as a convenience store: still a shop.
            "Alimentos El Arco, S. A.",
        ],
    )
    def test_a_real_shop_is_taken_whatever_it_is_called(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["supermarket"])
        types = ["convenience_store", "supermarket", "grocery_or_supermarket", "store"]

        assert not rules.rejects(_place(name, types))


class TestThePlaceHasToSayWhatItIs:
    """A deny-list promoted the next tagged business; the trial run proved it.

    Recalculating three listings replaced "Campo de Vuelo Capitan M RIVERA"
    with "Grupo 21" -- another business carrying Google's `airport` tag -- and
    offered "Nefer Clínica de Medicina Estética" as the nearest hospital. So a
    preset can require the name to carry the word (owner decision,
    2026-08-10), and a target with nothing qualifying nearby is reported as
    not found, which the scorer drops rather than scoring as zero.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "Grupo 21",
            "GlueWay System",
            "Hotel A Marisqueira",
            "Campo de Vuelo Capitan M RIVERA",
            "Helipuerto Hospital Universitario de Cabueñes",
        ],
    )
    def test_a_place_that_does_not_say_airport_is_refused(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["airport"])

        assert rules.rejects(_place(name, ["airport", "establishment"]))

    @pytest.mark.parametrize(
        "name", ["Asturias Airport", "Mutxamel Airport", "Aeropuerto de Alicante-Elche"]
    )
    def test_a_place_that_says_airport_is_taken(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["airport"])

        assert not rules.rejects(_place(name, ["airport", "establishment"]))

    @pytest.mark.parametrize(
        "name",
        [
            "Nefer Clínica de Medicina Estética",
            "Centro Sanitario Dra Silvia Carrillo",
            "Clínica Dental Alicante",
            "Clínica Veterinaria Gijón",
            # Carries the word "hospital" and is a landing pad.
            "Helipuerto Hospital Universitario de Cabueñes",
        ],
    )
    def test_a_clinic_is_not_the_nearest_hospital(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["hospital"])

        assert rules.rejects(_place(name, ["hospital", "health", "establishment"]))

    @pytest.mark.parametrize(
        "name",
        [
            "Hospital Universitario Central de Asturias",
            "Centro de Salud de Cudillero",
            "Ambulatorio de Gijón",
        ],
    )
    def test_a_hospital_or_health_centre_is_taken(self, name):
        rules = _place_rules(TRAVEL_PRESET_DEFS["hospital"])

        assert not rules.rejects(_place(name, ["hospital", "establishment"]))


class TestTheLookupWalksPastRefusedCandidates:
    def _service(self):
        service = PropertyTravelService()
        service.google_places_key = "test-key"
        return service

    def _payload(self, results):
        class _Response:
            status_code = 200

            def json(self):
                return {"status": "OK", "results": results}

        return _Response()

    def test_it_takes_the_nearest_candidate_the_preset_accepts(self, app):
        """`rankby=distance` orders them, so the first accepted one is nearest."""
        service = self._service()
        with patch(
            "services.property_travel_service.request_with_retries",
            return_value=self._payload([GLUEWAY, HOSPITAL_HELIPAD, REAL_AIRPORT]),
        ):
            lookup = service._nearest_place_for_preset(
                43.5, -5.6, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is not None
        assert lookup.place["name"] == "Asturias Airport"

    def test_all_refused_is_an_answer_not_a_failure(self, app):
        """ "No airport near here" must not read as an API refusal (#98)."""
        service = self._service()
        with patch(
            "services.property_travel_service.request_with_retries",
            return_value=self._payload([GLUEWAY, HOSPITAL_HELIPAD, HOTEL]),
        ):
            lookup = service._nearest_place_for_preset(
                43.5, -5.6, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is None
        assert lookup.failure is None, (
            "an empty result after filtering is Google answering, not refusing"
        )

    def test_a_preset_without_rules_still_takes_the_first_result(self, app):
        service = self._service()
        school = _place("Escuela La Serena", ["school", "establishment"])
        with patch(
            "services.property_travel_service.request_with_retries",
            return_value=self._payload([school]),
        ):
            lookup = service._nearest_place_for_preset(
                43.5, -5.6, "school", TRAVEL_PRESET_DEFS["school"]
            )

        assert lookup.place["name"] == "Escuela La Serena"

    def test_the_rules_are_part_of_the_cache_key(self, app):
        """A helipad cached before the rules existed must not be served again."""
        service = self._service()
        seen = []

        def _capture(lat, lon, cache_type):
            seen.append(cache_type)
            return None

        with patch(
            "services.property_travel_service.get_cached_enrichment_data",
            side_effect=_capture,
        ):
            with patch(
                "services.property_travel_service.request_with_retries",
                return_value=self._payload([REAL_AIRPORT]),
            ):
                service._nearest_place_for_preset(
                    43.5, -5.6, "airport", TRAVEL_PRESET_DEFS["airport"]
                )
                service._nearest_place_for_preset(
                    43.5, -5.6, "school", TRAVEL_PRESET_DEFS["school"]
                )

        airport_key, school_key = seen[0], seen[1]
        assert airport_key != "places_nearest_v1:airport", (
            "the airport key must change when the preset gains rules"
        )
        assert school_key == "places_nearest_v1:school", (
            "a preset without rules keeps its key, so its cache survives"
        )


def _listing_with_targets(**targets):
    prop = Property(
        source_email_id="travel-card-fixture",
        title="TravelCardFixture",
        municipality="Gijón",
        location_lat=43.529796,
        location_lon=-5.665516,
        location_accuracy="approximate",
    )
    prop.travel = {"targets": targets}
    db.session.add(prop)
    db.session.commit()
    return prop.id


# The six targets property 356 actually holds.
P356_TARGETS = {
    "airport": {
        "kind": "preset",
        "enabled": True,
        "status": "ok",
        "duration_min": 10,
        "distance_km": 2.422,
        "place": {"name": "GlueWay System"},
    },
    "train_station": {
        "kind": "preset",
        "enabled": True,
        "status": "ok",
        "duration_min": 8,
        "distance_km": 1.75,
        "place": {"name": "Gijón-Sanz Crespo"},
    },
    "hospital": {
        "kind": "preset",
        "enabled": True,
        "status": "ok",
        "duration_min": 9,
        "distance_km": 2.69,
        "place": {"name": "Hospital"},
    },
    "school": {
        "kind": "preset",
        "enabled": True,
        "status": "ok",
        "duration_min": 7,
        "distance_km": 1.771,
        "place": {"name": "Escuela de Educación Infantil La Serena"},
    },
    "supermarket": {
        "kind": "preset",
        "enabled": True,
        "status": "ok",
        "duration_min": 0,
        "distance_km": 0.029,
        "place": {"name": "Supermercados Alimerka"},
    },
}


class TestTheTravelCardShowsWhatWasMeasured:
    def test_school_and_supermarket_reach_the_page(self, app, client):
        listing = _listing_with_targets(**P356_TARGETS)

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Escuela de Educación Infantil La Serena" in body
        assert "Supermercados Alimerka" in body

    def test_a_zero_minute_target_is_shown_rather_than_hidden(self, app, client):
        """The Alimerka is 29 m away. `0` is the measurement, not its absence."""
        listing = _listing_with_targets(**P356_TARGETS)

        body = re.sub(
            r"\s+", " ", client.get(f"/properties/{listing}").get_data(as_text=True)
        )
        # Anchor inside the Travel Times card: the hero chip row (D6) names
        # the place earlier in the DOM, and the propertyData JSON blob names
        # it again later — only the card row carries the min/km badges.
        travel_card = body[body.index("Travel Times") :]
        row = travel_card[travel_card.index("Supermercados Alimerka") :][:300]

        assert "0min" in row
        assert "0.0km" in row

    def test_the_place_that_was_measured_is_named(self, app, client):
        """Naming it is how "10 min to the airport" can be caught out."""
        listing = _listing_with_targets(**P356_TARGETS)

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "GlueWay System" in body, (
            "an owner cannot audit a distance to an unnamed place"
        )

    def test_no_invented_walking_time_anywhere(self, app, client):
        listing = _listing_with_targets(**P356_TARGETS)

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "fa-walking" not in body
        assert "walking_time" not in body
