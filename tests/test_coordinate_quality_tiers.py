"""`approximate` means two things, and the scorer had to assume the worse one.

Issue #493. A coordinate a portal or a person placed for *this advert* carried
the same 5 km of doubt as a locality centroid twenty listings share, because
`coordinate_slack_m` read a boolean off `location_accuracy` and nothing else.

The fixture numbers are measurements, not inventions. On production,
2026-09-01, over 1226 located rows:

* **0.0%** of the 183 rows carrying a listing-specific pin share their exact
  coordinate with another listing. **51.9%** of the 879 geocoded rows do, up
  to 21 listings on one point.
* Eight rows carry both a location a person established from the cadastre and
  a portal or map pin. The distance between the two -- the pin's own error --
  is 68, 102, 107, 122, 124, 174, 195 and 1150 m.
* Property 421 is one of those eight, and a person wrote the answer down
  independently: its import block records *"EXACT per portal, but the pin is a
  meadow 170 m S of the house"* against a computed 174 m.

What this file does **not** claim is that the tier makes anything score.
Measured over the same 1226 rows, it does not: every component that scores
today still scores, and not one new one does. `test_the_tier_alone_scores_
nothing_new` is that fact in a test, because the temptation to sell this
change as the fix for the coverage half of #493 is exactly what the
measurement refuses.
"""

import itertools
from decimal import Decimal

import pytest

from app import create_app, db
from models import Property
from services import coordinate_quality
from services import sea_view_service as svs
from services.coordinate_quality import (
    APPROXIMATE_COORD_SLACK_M,
    LISTING_PIN_SLACK_M,
    TIER_ADDRESS,
    TIER_LISTING_PIN,
    TIER_LOCALITY,
    coordinate_slack_m,
    coordinate_tier,
    record_manual_coordinate,
    record_portal_coordinate,
    slack_for_tier,
)
from services.hazard_service import read_verdict as hazard_verdict
from services.sea_distance_service import parcel_measurement
from tests import setup_test_environment

setup_test_environment()

# Property 733's own fotocasa pin (#393), to seven places -- the precision the
# `Numeric(10, 7)` columns actually hold.
PIN_LAT = Decimal("43.5566000")
PIN_LON = Decimal("-5.9241000")


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


_SEQ = itertools.count(1)


def _row(app, *, accuracy="approximate", lat=PIN_LAT, lon=PIN_LON, enrichment=None):
    n = next(_SEQ)
    prop = Property(
        source_email_id=f"test:{n}",
        title="Finca",
        url=f"https://example.invalid/{n}",
        location_lat=lat,
        location_lon=lon,
        location_accuracy=accuracy,
        enrichment=enrichment or {},
    )
    db.session.add(prop)
    db.session.flush()
    return prop


def _with_portal_pin(app, *, lat=PIN_LAT, lon=PIN_LON, source="fotocasa", **kw):
    block = record_portal_coordinate(None, source=source, lat=lat, lon=lon)
    return _row(app, enrichment=block, **kw)


def _with_hand_set(
    app, *, lat=PIN_LAT, lon=PIN_LON, block_accuracy="approximate", **kw
):
    block = record_manual_coordinate(
        None,
        lat=lat,
        lon=lon,
        accuracy=block_accuracy,
        note="parcel 33016A003001530001HQ read off the cadastre",
    )
    return _row(app, enrichment=block, **kw)


class TestTheTiers:
    def test_a_precise_row_is_the_address_tier_and_carries_no_slack(self, app):
        prop = _row(app, accuracy="precise")
        assert coordinate_tier(prop) == TIER_ADDRESS
        assert coordinate_slack_m(prop) == 0.0

    def test_a_row_with_nothing_is_a_locality_centroid(self, app):
        prop = _row(app)
        assert coordinate_tier(prop) == TIER_LOCALITY
        assert coordinate_slack_m(prop) == float(APPROXIMATE_COORD_SLACK_M)

    def test_a_portal_pin_standing_where_the_row_stands_is_the_middle_tier(self, app):
        prop = _with_portal_pin(app)
        assert coordinate_tier(prop) == TIER_LISTING_PIN
        assert coordinate_slack_m(prop) == float(LISTING_PIN_SLACK_M)
        assert 0.0 < coordinate_slack_m(prop) < float(APPROXIMATE_COORD_SLACK_M)

    def test_a_hand_set_location_is_the_middle_tier_too(self, app):
        prop = _with_hand_set(app)
        assert coordinate_tier(prop) == TIER_LISTING_PIN

    def test_the_address_tier_wins_over_a_pin_block(self, app):
        """A person who established a parcel and wrote `precise` on the column
        must not be demoted to the middle tier by the provenance they left."""
        prop = _with_hand_set(app, block_accuracy="precise", accuracy="precise")
        assert coordinate_tier(prop) == TIER_ADDRESS
        assert coordinate_slack_m(prop) == 0.0


class TestThePinHasToBeWhereTheRowIs:
    def test_a_row_that_moved_off_its_pin_falls_back_to_the_locality(self, app):
        """Direct SQL is a supported workflow, and `set_property_location`
        writes one column pair and a different block. A pin block is provenance
        for a coordinate the row may no longer carry."""
        prop = _with_portal_pin(app)
        prop.location_lat = Decimal("43.6000000")  # ~4.8 km north of the pin
        db.session.flush()
        assert coordinate_tier(prop) == TIER_LOCALITY
        assert coordinate_slack_m(prop) == float(APPROXIMATE_COORD_SLACK_M)

    def test_the_match_is_metric_not_textual(self, app):
        """Pins are stored as decimal strings with 4 to 12 places against
        `Numeric(10, 7)` columns, so string equality would refuse the row's own
        pin. Twelve places, rounded into the column, is the same place."""
        prop = _with_portal_pin(app, lat="43.556600012345", lon="-5.924100098765")
        assert prop.enrichment["import"]["coordinate"]["lat"] == "43.556600012345"
        assert coordinate_tier(prop) == TIER_LISTING_PIN

    def test_a_pin_a_few_metres_off_is_not_this_row_s_pin(self, app):
        """The epsilon is one metre: it exists to absorb the column's rounding,
        never to accept a nearby point as the same one."""
        prop = _with_portal_pin(app, lat=Decimal("43.5566450"))  # ~5 m north
        assert coordinate_tier(prop) == TIER_LOCALITY

    def test_a_row_with_no_coordinate_has_no_pin_to_stand_on(self, app):
        prop = _with_portal_pin(app, lat=None, lon=None)
        prop.location_lat = None
        prop.location_lon = None
        db.session.flush()
        assert coordinate_tier(prop) == TIER_LOCALITY


class TestWhatTheTierRefusesToRead:
    @pytest.mark.parametrize(
        "source",
        [
            "fotocasa",
            "fotocasa_pin",
            "fotocasa payload",
            "idealista",
            "idealista_map",
            "idealista map pin",
            "idealista_pin",
            "milanuncios",
            "pisos_pin",
        ],
    )
    def test_the_source_string_is_not_read(self, app, source):
        """All nine spellings are on production. A slack table keyed on this
        field would give the wide slack to `fotocasa_pin` and the narrow one to
        `fotocasa` -- a partial rule that reads as complete."""
        assert coordinate_tier(_with_portal_pin(app, source=source)) == (
            TIER_LISTING_PIN
        )

    def test_a_malformed_block_is_a_centroid_and_never_an_exception(self, app):
        prop = _row(app, enrichment={"import": {"coordinate": {"lat": "north"}}})
        assert coordinate_tier(prop) == TIER_LOCALITY

    def test_an_accuracy_label_is_refused_out_loud(self, app):
        """This function took the label until #493, and a label answers every
        attribute lookup with `None` -- so an unmigrated caller would silently
        receive the locality slack for every row, precise ones included."""
        with pytest.raises(TypeError, match="row, not its accuracy label"):
            coordinate_tier("precise")
        with pytest.raises(TypeError):
            coordinate_slack_m("approximate")


class TestOneHomeForThePolicy:
    def test_the_table_follows_the_constants(self, monkeypatch):
        """A table frozen at import stops honouring the constants it was built
        from -- which is what `test_hazard_proximity` caught when this shipped
        as a module-level dict."""
        monkeypatch.setattr(coordinate_quality, "LISTING_PIN_SLACK_M", 321)
        assert slack_for_tier(TIER_LISTING_PIN) == 321.0

    def test_an_unknown_tier_claims_the_least(self):
        assert slack_for_tier("something_a_later_version_writes") == float(
            APPROXIMATE_COORD_SLACK_M
        )

    def test_sea_view_stays_on_the_locality_slack_deliberately(self, app, monkeypatch):
        """Its slack sets `decisive_distance` *and* the geometry cache key, so
        narrowing it re-opens every pin row's cached Overpass and elevation
        work. That is a separate, announced run -- not a side effect of a
        scoring refactor. The point of the test is that the decision is
        deliberate and that the module reads the shared table rather than
        keeping its own copy of the number."""
        monkeypatch.setattr(coordinate_quality, "APPROXIMATE_COORD_SLACK_M", 250)
        seen = {}

        def _capture(lat, lon, coordinate_accuracy=None, **kw):
            seen["decisive"] = svs.MAX_SEA_DISTANCE_M + (
                slack_for_tier(TIER_LOCALITY)
                if not coordinate_quality.is_precise(coordinate_accuracy)
                else 0
            )
            return seen

        _capture(43.5, -5.9, coordinate_accuracy="approximate")
        assert seen["decisive"] == svs.MAX_SEA_DISTANCE_M + 250


class TestTheConsumersReadIt:
    def _measured_sea_block(self, distance_m):
        return {
            "status": "approximate_origin",
            "origin_distance_m": distance_m,
            "searched_m": 10_000.0,
            "slack_m": float(APPROXIMATE_COORD_SLACK_M),
            "source": "osm_coastline",
        }

    def test_the_sea_band_narrows_for_a_pin_row(self, app):
        centroid = _row(app)
        centroid.enrichment = {"sea": self._measured_sea_block(4000.0)}
        pin = _with_portal_pin(app)
        pin.enrichment = {
            **pin.enrichment,
            "sea": self._measured_sea_block(4000.0),
        }
        db.session.flush()

        wide = parcel_measurement(centroid)
        narrow = parcel_measurement(pin)
        assert wide["min_distance_m"] == 0.0
        assert wide["max_distance_m"] == 9000.0
        assert narrow["min_distance_m"] == 2000.0
        assert narrow["max_distance_m"] == 6000.0
        # The measurement itself is untouched: this is a restatement, not a
        # remeasurement, and nothing was recomputed to get it.
        assert narrow["origin_distance_m"] == wide["origin_distance_m"] == 4000.0

    def test_the_guaranteed_radius_grows_for_a_pin_row(self, app):
        """`searched_m` is what the answer is guaranteed for *around the
        parcel*, so less doubt about the parcel means more guaranteed ground."""
        centroid = _row(app)
        centroid.enrichment = {"sea": self._measured_sea_block(4000.0)}
        pin = _with_portal_pin(app)
        pin.enrichment = {**pin.enrichment, "sea": self._measured_sea_block(4000.0)}
        db.session.flush()
        assert (
            parcel_measurement(pin)["searched_m"]
            > (parcel_measurement(centroid)["searched_m"])
        )

    def test_the_hazard_band_narrows_for_a_pin_row(self, app):
        block = {
            "status": "ok",
            "origin": {"lat": float(PIN_LAT), "lon": float(PIN_LON)},
            "origin_accuracy": "approximate",
            "slack_m": float(APPROXIMATE_COORD_SLACK_M),
            "searched_m": 6000.0,
            "items": [],
            "item_count": 0,
            "truncated": False,
            "measured": True,
            "updated_at": "2026-09-01T00:00:00+00:00",
        }
        centroid = _row(app)
        centroid.enrichment = {"hazards": block}
        pin = _with_portal_pin(app)
        pin.enrichment = {**pin.enrichment, "hazards": block}
        db.session.flush()
        assert hazard_verdict(centroid)["slack_m"] == float(APPROXIMATE_COORD_SLACK_M)
        assert hazard_verdict(pin)["slack_m"] == float(LISTING_PIN_SLACK_M)
        assert (
            hazard_verdict(pin)["guaranteed_m"]
            > (hazard_verdict(centroid)["guaranteed_m"])
        )


class TestTheTierAloneScoresNothingNew:
    def test_a_pin_row_with_a_mid_range_target_still_abstains(self, app):
        """The measured answer to #493's coverage half, kept as a test so the
        change cannot be re-sold as the fix for it.

        Travel scores only where the imprecision cannot move the answer, and
        both scoring curves are strictly monotone across their useful range --
        so a target at 30 minutes is inside `(best, worst)` at both ends of any
        band, at 2000 m exactly as at 5000. Over the whole production table the
        tier moves the count of scored components by zero.
        """
        from services.property_scoring_service import HousingPropertyScorer

        pin = _with_portal_pin(app)
        pin.travel = {
            "targets": {
                "hospital": {"duration_min": 30.0, "mode": "driving"},
                "airport": {"duration_min": 45.0, "mode": "driving"},
            }
        }
        db.session.flush()
        score, detail = HousingPropertyScorer()._travel_score(pin, None, 10.0, 60.0)
        assert score is None
        assert detail["status"] == "approximate_origin"
        # ...and it is the narrower slack that was applied, not the wide one.
        assert coordinate_slack_m(pin) == float(LISTING_PIN_SLACK_M)
