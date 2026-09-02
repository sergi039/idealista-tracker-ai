"""Similar to the favorites: the reading, and every surface that shows it.

The owner asked (2026-09-02) for a filter that selects, from the whole
Galicia subscription, the listings most alike the two they had starred.
`services/favorite_similarity.py` is that reading: the favorites of the
row's OWN subscription are the references, every fact both sides state is
compared and every fact one side lacks abstains (#98), the nearest reference
wins, and the number rests on a location or it does not rank at all.

What is pinned here, and why each case is here:

* **By value.** The neighbour's 84.1 is hand-computed from the module's own
  formulas in the test, component by component, so a weight that drifts or a
  component that silently stops being compared fails here rather than
  rendering politely (the None×None lesson).
* **Absence abstains.** A row without bedrooms is compared on the rest, with
  its coverage lower; a row of a different kind is gated, not scored 0; a
  row nobody can place keeps its number for the reader and never ranks.
* **The municipality point.** 299 of 543 Galicia rows have no coordinate;
  the median of the located rows sharing the municipality key places them,
  and the reading says so.
* **One reading, every surface.** The list's rows, its chip, its disclosure
  line, the map's markers, the CSV's columns, the API's payload and notes and
  the row's own page all read the same context; the sweeps in
  tests/test_map_and_list_agree_on_the_filters.py and
  tests/test_api_properties_reads_the_pages_filters.py walk the parameter with
  the rest, and the cases here are the vocabulary's own.
"""

from __future__ import annotations

import csv
import io
import math
import re
from html import unescape

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import favorite_similarity as fs
from tests import setup_test_environment

# Malpica de Bergantiños, the two production favorites' municipality; Vigo is
# ~120 km south, Ponteceso ~7 km east.
MALPICA = (43.31, -8.86)
NEAR_MALPICA = (43.33, -8.83)
PONTECESO = (43.24, -8.90)
VIGO = (42.24, -8.72)


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


_SEQ = iter(range(1, 10_000))


def _profile(name="Galicia · costa", **overrides):
    values = dict(
        name=name, is_active=True, travel_targets={"presets": {}, "custom": []}
    )
    values.update(overrides)
    row = SearchProfile(**values)
    db.session.add(row)
    db.session.commit()
    return row


def _mk(profile_id, **overrides):
    """A detached house in Malpica at 290 000 EUR / 300 m², unless told otherwise."""
    n = next(_SEQ)
    coords = overrides.pop("coords", None)
    values = dict(
        source_email_id=f"sim:{n}",
        title=overrides.pop("title", f"Casa {n}"),
        url=f"https://www.idealista.com/inmueble/9{n}/",
        price=290000,
        area=300,
        area_type="built",
        property_category="housing",
        property_subtype="house",
        municipality="Malpica de Bergantiños",
        search_profile_id=profile_id,
        listing_status="active",
    )
    if coords:
        values["location_lat"], values["location_lon"] = coords
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


@pytest.fixture
def world(app):
    """One subscription with one favorite, and one row per way of reading.

    The favorite is precise at Malpica with rooms, bathrooms, a sea distance
    and a sea view; the neighbour states every one of those facts; the rest
    each lack or contradict one of them.
    """
    profile = _profile()
    pid = profile.id
    rows = {
        "favorite": _mk(
            pid,
            title="The favorite",
            is_favorite=True,
            coords=MALPICA,
            location_accuracy="precise",
            attributes={"rooms": 5, "bathrooms": 2},
            enrichment={
                "sea": {"status": "ok", "distance_m": 400.0},
                "environment": {"sea_view": "likely"},
            },
        ),
        # Everything stated, everything close: 275k, 292 m², 3.3 km away,
        # the same bedrooms, one more bathroom, and no sea view where the
        # favorite likely has one. Its sea distance is a centroid's figure
        # (approximate coordinate), so against the favorite's precise 400 m
        # the 5 km slack leaves the answer open and the component abstains.
        "neighbour": _mk(
            pid,
            title="The neighbour",
            price=275000,
            area=292,
            coords=NEAR_MALPICA,
            location_accuracy="approximate",
            attributes={"bedrooms": 5, "bathrooms": "3"},
            enrichment={
                "sea": {
                    "status": "approximate_origin",
                    "distance_m": None,
                    "origin_distance_m": 500.0,
                },
                "environment": {"sea_view": "no"},
            },
        ),
        # No coordinate: placed by the municipality point the favorite and
        # the neighbour make. Price 250k, area 300, nothing else stated.
        "unlocated": _mk(pid, title="Unlocated in Malpica", price=250000),
        # Same house, 120 km away.
        "far": _mk(pid, title="Far away in Vigo", municipality="Vigo", coords=VIGO),
        # A plot, whatever its price: a different kind of listing.
        "plot": _mk(
            pid,
            title="A plot",
            property_category="land",
            property_subtype="plot",
            area=2000,
            area_type="plot",
            coords=NEAR_MALPICA,
        ),
        # Same price and area as the favorite, and nowhere: no coordinate,
        # and no located listing anywhere in its municipality.
        "nowhere": _mk(pid, title="Nowhere", municipality="Ares"),
    }
    return {"pid": pid, "rows": rows, "ids": {k: v.id for k, v in rows.items()}}


def _reading(world, name):
    ctx = fs.build_context()
    assert ctx is not None
    return ctx.read(world["ids"][name])


def _weighted(components):
    weight = sum(fs.WEIGHTS[name] for name in components)
    return sum(fs.WEIGHTS[name] * value for name, value in components.items()) / weight


def _ratio(a, b):
    return max(0.0, 1 - abs(math.log(a / b)) / math.log(2)) * 100


class TestTheReading:
    def test_the_favorite_reads_as_reference(self, world):
        reading = _reading(world, "favorite")
        assert reading["state"] == fs.STATE_REFERENCE
        assert reading["score"] == 100.0
        assert reading["reference_count"] == 1

    def test_the_neighbour_scores_on_every_fact_both_state_by_value(self, world):
        reading = _reading(world, "neighbour")
        assert reading["state"] == fs.STATE_OK
        assert reading["reference_id"] == world["ids"]["favorite"]
        assert reading["compared"] == [
            "price",
            "area",
            "geography",
            "bedrooms",
            "bathrooms",
            "sea_view",
        ]
        parts = reading["components"]
        assert parts["price"] == pytest.approx(_ratio(275000, 290000), abs=0.05)
        assert parts["area"] == pytest.approx(_ratio(292, 300), abs=0.05)
        # 5 bedrooms both; 3 bathrooms against 2 (a string in the JSON, parsed
        # not cast); no view against likely; the sea distance abstains (the
        # slack case is its own test below).
        assert parts["bedrooms"] == 100.0
        assert parts["bathrooms"] == 60.0
        assert "sea_distance" not in parts
        assert parts["sea_view"] == 0.0
        # 3.3 km apart on a 60 km scale.
        assert 94.0 <= parts["geography"] <= 95.0
        assert reading["score"] == pytest.approx(_weighted(parts), abs=0.06)
        assert reading["score"] == pytest.approx(84.1, abs=0.1)
        assert reading["coverage"] == pytest.approx(10.5 / fs.TOTAL_WEIGHT, abs=0.001)
        assert reading["compared_count"] == 6 and reading["fact_count"] == 8
        assert reading["base_only"] is False
        # The approximate coordinate is the row's own point, labelled as
        # approximate (a pin or a locality centroid: the label does not
        # claim to know which).
        assert reading["geography_basis"] == fs.BASIS_APPROXIMATE
        assert reading["municipality_point_n"] is None

    def test_an_absent_fact_abstains_and_lowers_the_coverage(self, world):
        reading = _reading(world, "unlocated")
        assert reading["state"] == fs.STATE_OK
        assert reading["compared"] == ["price", "area", "geography"]
        assert "bedrooms" not in reading["components"]
        assert reading["coverage"] == pytest.approx(8.0 / fs.TOTAL_WEIGHT, abs=0.001)
        assert reading["components"]["area"] == 100.0
        assert reading["components"]["price"] == pytest.approx(
            _ratio(250000, 290000), abs=0.05
        )

    def test_a_row_without_a_coordinate_is_placed_by_its_municipality(self, world):
        reading = _reading(world, "unlocated")
        # The municipality point is the median of the three located Malpica
        # rows (the favorite, the neighbour and the plot), which lands on the
        # neighbour's point 3.3 km from the favorite.
        assert reading["geography_basis"] == fs.BASIS_MUNICIPALITY
        assert reading["municipality_point_n"] == 3
        assert reading["components"]["geography"] == pytest.approx(94.5, abs=0.1)
        # (3 x 78.6 + 2 x 100 + 3 x 94.5) / 8
        assert reading["score"] == pytest.approx(89.9, abs=0.1)
        assert reading["base_only"] is True

    def test_a_far_row_scores_zero_on_location_and_stays_rankable(self, world):
        reading = _reading(world, "far")
        assert reading["state"] == fs.STATE_OK
        assert reading["components"]["geography"] == 0.0
        assert reading["components"]["price"] == 100.0
        assert reading["score"] == pytest.approx(
            _weighted(reading["components"]), abs=0.06
        )
        assert reading["score"] < 70

    def test_a_different_kind_is_gated_not_scored(self, world):
        reading = _reading(world, "plot")
        assert reading["state"] == fs.STATE_DIFFERENT_KIND
        assert reading["score"] is None

    def test_the_kind_folds_land_together_and_leaves_an_unstated_side_alone(self, app):
        """Sub 17's shape: 14 starred `plot` rows beside 2 legacy `developed`
        parcels -- one kind, compared; a flat against a house is gated; a
        housing row with no subtype is compared, never gated (#98)."""
        land = _profile("Plots")
        ref = _mk(
            land.id,
            is_favorite=True,
            property_category="land",
            property_subtype="plot",
            area=2100,
            area_type="plot",
            coords=MALPICA,
        )
        developed = _mk(
            land.id,
            property_category="land",
            property_subtype="developed",
            area=2000,
            area_type="plot",
            coords=MALPICA,
        )
        houses = _profile("Houses")
        _mk(houses.id, is_favorite=True, coords=MALPICA)
        flat = _mk(houses.id, property_subtype="apartment", coords=MALPICA)
        untyped = _mk(houses.id, property_subtype=None, coords=MALPICA)
        ctx = fs.build_context()
        assert ctx.read(developed.id)["state"] == fs.STATE_OK
        assert ctx.read(developed.id)["reference_id"] == ref.id
        assert ctx.read(flat.id)["state"] == fs.STATE_DIFFERENT_KIND
        assert ctx.read(untyped.id)["state"] == fs.STATE_OK

    def test_an_attached_house_is_not_similar_to_a_detached_one(self, world):
        """The owner's own definition (/agencies): adosados and pareados are
        not detached houses; casa de pueblo and casa rural are. Read from
        the title head only -- a street called Pareada is not a terrace."""
        pid = world["pid"]
        terraced = _mk(pid, title="Casa adosada en venta en Malpica", coords=MALPICA)
        paired = _mk(pid, title="Chalet pareado en Ponteceso", coords=PONTECESO)
        village = _mk(pid, title="Casa de pueblo en Laxe", coords=PONTECESO)
        bare = _mk(
            pid, title="Chalet en venta en calle Pareada, Malpica", coords=MALPICA
        )
        ctx = fs.build_context()
        # The favorite's title states no typology, so nothing is gated on it
        # yet; a detached favorite gates the attached rows.
        assert ctx.read(terraced.id)["state"] == fs.STATE_OK
        detached_ref = _mk(
            pid,
            title="Casa o chalet independiente en venta en Malpica",
            is_favorite=True,
            coords=MALPICA,
        )
        ctx = fs.build_context()
        assert ctx.read(terraced.id)["reference_id"] == world["ids"]["favorite"]
        assert ctx.read(village.id)["state"] == fs.STATE_OK
        assert ctx.read(bare.id)["state"] == fs.STATE_OK
        # Against the detached favorite alone, the attached rows are gated.
        world["rows"]["favorite"].is_favorite = False
        db.session.commit()
        ctx = fs.build_context()
        assert ctx.read(terraced.id)["state"] == fs.STATE_DIFFERENT_KIND
        assert ctx.read(paired.id)["state"] == fs.STATE_DIFFERENT_KIND
        assert ctx.read(village.id)["reference_id"] == detached_ref.id
        assert ctx.read(bare.id)["reference_id"] == detached_ref.id

    def test_a_row_nobody_can_place_is_thin_and_never_kept(self, world):
        reading = _reading(world, "nowhere")
        assert reading["state"] == fs.STATE_THIN
        # The number is still there for the reader, and honest about itself.
        assert reading["score"] == 100.0
        assert reading["compared"] == ["price", "area"]
        assert reading["missing_required"] == ["geography"]
        ctx = fs.build_context()
        assert world["ids"]["nowhere"] not in ctx.kept_ids(60.0)
        assert world["ids"]["nowhere"] not in ctx.sort_keys()

    def test_the_cut_keeps_the_references_and_the_rankable_rows_at_or_above_it(
        self, world
    ):
        ctx = fs.build_context()
        ids = world["ids"]
        assert ctx.kept_ids(70.0) == sorted(
            [ids["favorite"], ids["neighbour"], ids["unlocated"]]
        )
        assert ctx.similar_ids(70.0) == sorted([ids["neighbour"], ids["unlocated"]])
        # A cut nothing but the reference clears still keeps the reference.
        assert ctx.kept_ids(99.9) == [ids["favorite"]]

    def test_an_unstated_subtype_is_not_gated(self, world):
        untyped = _mk(world["pid"], property_subtype=None, coords=NEAR_MALPICA)
        assert fs.build_context().read(untyped.id)["state"] == fs.STATE_OK

    def test_price_and_area_are_log_ratios_symmetric_and_zero_at_double(self, world):
        pid = world["pid"]
        double = _mk(pid, price=580000, coords=MALPICA)
        half = _mk(pid, price=145000, coords=MALPICA)
        ctx = fs.build_context()
        assert ctx.read(double.id)["components"]["price"] == 0.0
        assert ctx.read(half.id)["components"]["price"] == 0.0
        assert ctx.read(double.id)["score"] == ctx.read(half.id)["score"]

    def test_zero_is_an_absence_not_a_measurement(self, world):
        blank = _mk(
            world["pid"],
            coords=MALPICA,
            attributes={"bedrooms": 0, "bathrooms": "abc"},
            enrichment={"sea": {"status": "ok", "distance_m": 0}},
        )
        reading = fs.build_context().read(blank.id)
        for name in ("bedrooms", "bathrooms", "sea_distance"):
            assert name not in reading["components"], name

    def test_a_row_stating_many_facts_but_no_location_is_still_thin(self, world):
        """The rule is the location, not a share of the weight: a row on
        price, area, bedrooms, bathrooms and a sea view (7.5 of 13) with
        nowhere to be placed is thin, exactly like the two-fact one."""
        row = _mk(
            world["pid"],
            municipality="Ares",
            attributes={"bedrooms": 5, "bathrooms": 2},
            enrichment={"environment": {"sea_view": "likely"}},
        )
        ctx = fs.build_context()
        reading = ctx.read(row.id)
        assert reading["state"] == fs.STATE_THIN
        assert reading["compared"] == [
            "price",
            "area",
            "bedrooms",
            "bathrooms",
            "sea_view",
        ]
        assert reading["missing_required"] == ["geography"]
        assert row.id not in ctx.kept_ids(60.0)
        assert row.id not in ctx.sort_keys()

    def test_a_detached_house_is_gated_by_an_attached_favorite_too(self, app):
        """The typology gate works in both directions."""
        profile = _profile("Attached")
        _mk(
            profile.id,
            title="Casa adosada en venta en Malpica",
            is_favorite=True,
            coords=MALPICA,
        )
        detached = _mk(
            profile.id, title="Casa o chalet independiente en Malpica", coords=MALPICA
        )
        bare = _mk(profile.id, title="Casa en Malpica", coords=MALPICA)
        ctx = fs.build_context()
        assert ctx.read(detached.id)["state"] == fs.STATE_DIFFERENT_KIND
        assert ctx.read(bare.id)["state"] == fs.STATE_OK

    def test_one_rounding_rule_for_the_chip_and_the_cut(self, world):
        """A row at 79.6: printed as 79.6, left out by the 80 cut, kept by
        the 70 cut -- the chip and the count can never disagree."""
        # Same coordinate and area as the favorite; the price ratio alone
        # sets the score: (3 x 45.6 + 2 x 100 + 3 x 100) / 8 = 79.6.
        row = _mk(
            world["pid"], price=422820, coords=MALPICA, location_accuracy="precise"
        )
        ctx = fs.build_context()
        reading = ctx.read(row.id)
        assert 79.5 <= reading["score"] < 79.7
        assert row.id not in ctx.kept_ids(80.0)
        assert row.id in ctx.kept_ids(70.0)

    def test_the_favorites_own_basis_is_named_when_it_is_the_looser_side(
        self, client, app
    ):
        """Sub 6's shape: the favorite is a locality centroid, the row is
        precise -- the distance is to a centroid and the page says so."""
        profile = _profile("Centroid favorite")
        _mk(
            profile.id,
            is_favorite=True,
            coords=MALPICA,
            location_accuracy="approximate",
        )
        precise = _mk(profile.id, coords=NEAR_MALPICA, location_accuracy="precise")
        reading = fs.build_context().read(precise.id)
        assert reading["geography_basis"] == fs.BASIS_COORDINATE
        assert reading["reference_geography_basis"] == fs.BASIS_APPROXIMATE
        assert fs.weaker_basis(reading) == fs.BASIS_APPROXIMATE
        text = _card(client.get(f"/properties/{precise.id}").get_data(as_text=True))
        assert (
            "the favorite's location from the listing's approximate coordinate" in text
        )

    def test_a_coordinate_off_the_globe_is_no_coordinate(self, world):
        """NaN and a latitude of 400 are bad input, not a location: such a
        row takes the municipality path instead of scoring 60 km away under
        basis `coordinate`."""
        row = list(
            (
                99,
                world["pid"],
                False,
                "housing",
                "house",
                "Casa",
                290000,
                300,
                "built",
                None,
                None,
                None,
                None,
                float("nan"),
                -8.86,
                "precise",
                "Malpica de Bergantiños",
                None,
                None,
                None,
            )
        )
        facts = fs.facts_from_row(row)
        assert facts.lat is None and facts.lon is None
        row[13], row[14] = 400.0, -8.86
        assert fs.facts_from_row(row).lat is None
        points = fs.municipality_points()
        located = fs.locate(facts, points)
        assert located is not None and located[2] == fs.BASIS_MUNICIPALITY

    def test_the_plot_is_the_criteria_modules_reading(self, app):
        """Bare land: `area` IS the plot where `plot_area` is unstated, and
        it is then compared ONCE, as the plot -- never a second time as a
        built surface."""
        profile = _profile("Plots")
        ref = _mk(
            profile.id,
            is_favorite=True,
            property_category="land",
            property_subtype="plot",
            area=2100,
            area_type="plot",
            plot_area=None,
            coords=MALPICA,
        )
        other = _mk(
            profile.id,
            property_category="land",
            property_subtype="plot",
            area=2000,
            area_type="plot",
            coords=MALPICA,
        )
        reading = fs.build_context().read(other.id)
        assert reading["reference_id"] == ref.id
        assert reading["components"]["plot"] == pytest.approx(
            _ratio(2000, 2100), abs=0.05
        )
        assert "area" not in reading["components"]
        assert reading["compared"] == ["price", "geography", "plot"]

    def test_a_house_whose_area_is_the_parcel_abstains_on_area(self, world):
        """Production rows 1550/1551's shape: subtype `house`, `area_type`
        plot -- the parcel is not scored as a built surface."""
        row = _mk(world["pid"], area=2142, area_type="plot", coords=MALPICA)
        reading = fs.build_context().read(row.id)
        assert reading["state"] == fs.STATE_OK
        assert "area" not in reading["components"]
        assert "plot" not in reading["components"]

    def test_the_sea_distance_abstains_wherever_the_slack_leaves_it_open(self, world):
        """The #358 rule the scorer applies: an approximate coordinate's sea
        distance is a band 5 km either way, and against a 2 km scale the
        band settles the component only when it cannot come within the
        scale (then 0) -- never by its endpoints, since the decay peaks at
        the reference and a band can contain the peak while both ends
        score 0."""
        pid = world["pid"]

        def row(distance, accuracy):
            return _mk(
                pid,
                coords=MALPICA,
                location_accuracy=accuracy,
                enrichment={"sea": {"status": "ok", "distance_m": distance}},
            )

        a = row(1500.0, "approximate")  # band [0, 6500] straddles 400
        b = row(9000.0, "approximate")  # band [4000, 14000]: gap 3600 >= 2000
        c = row(1000.0, "approximate")  # band [0, 6000] contains 400; ends 0
        d = row(1300.0, "precise")  # a point against a point: 100 - 900/2000
        ctx = fs.build_context()
        assert "sea_distance" not in ctx.read(a.id)["components"]
        assert ctx.read(b.id)["components"]["sea_distance"] == 0.0
        assert ctx.read(b.id)["sea_distance_basis"] == fs.SEA_BASIS_BAND
        assert "sea_distance" not in ctx.read(c.id)["components"]
        assert ctx.read(d.id)["components"]["sea_distance"] == pytest.approx(
            55.0, abs=0.1
        )
        assert ctx.read(d.id)["sea_distance_basis"] == fs.SEA_BASIS_PARCEL
        # (e) the reference's own slack counts too: an approximate favorite at
        # 2000 m is a band [0, 7000], and a precise 9500 m row is past it.
        other = _profile("Approximate favorite")
        _mk(
            other.id,
            is_favorite=True,
            coords=MALPICA,
            location_accuracy="approximate",
            enrichment={"sea": {"status": "ok", "distance_m": 2000.0}},
        )
        e = _mk(
            other.id,
            coords=MALPICA,
            location_accuracy="precise",
            enrichment={"sea": {"status": "ok", "distance_m": 9500.0}},
        )
        near = _mk(
            other.id,
            coords=MALPICA,
            location_accuracy="precise",
            enrichment={"sea": {"status": "ok", "distance_m": 3000.0}},
        )
        ctx = fs.build_context()
        assert ctx.read(e.id)["components"]["sea_distance"] == 0.0
        assert "sea_distance" not in ctx.read(near.id)["components"]

    def test_the_loader_reads_the_sea_facts_as_the_filters_do(self, app):
        """The loader parses JSON in Python where the filters cast in SQL;
        one fixture through both, so the two readings cannot drift -- and a
        hand-edited leaf reads as absent instead of raising."""
        from services.listing_attribute_filters import (
            sea_distance_m_expr,
            sea_view_state_expr,
        )

        profile = _profile()
        shapes = {
            "precise": {"sea": {"status": "ok", "distance_m": 350.0}},
            "centroid": {
                "sea": {
                    "status": "approximate_origin",
                    "distance_m": None,
                    "origin_distance_m": 620.0,
                }
            },
            "computed": {"environment": {"sea_view": "yes"}},
            "legacy_true": {"legacy_land": {"environment": {"sea_view": True}}},
            "legacy_false": {"legacy_land": {"environment": {"sea_view": False}}},
            "junk": {
                "sea": {"status": "ok", "distance_m": "junk"},
                "environment": {"sea_view": "maybe"},
                "legacy_land": {"environment": {"sea_view": "maybe"}},
            },
            "nothing": None,
        }
        ids = {
            name: _mk(profile.id, is_favorite=(name == "precise"), enrichment=block).id
            for name, block in shapes.items()
        }
        by_id = {facts.id: facts for facts in fs.load_facts([profile.id])}
        sql = {
            row_id: (distance, state)
            for row_id, distance, state in db.session.query(
                Property.id,
                sea_distance_m_expr(Property),
                sea_view_state_expr(Property),
            ).filter(Property.search_profile_id == profile.id)
        }
        for name, row_id in ids.items():
            if name == "junk":
                # The hazard itself: SQLite hands the text through its CAST
                # and PostgreSQL raises on it, so the SQL side has no one
                # answer to agree with. The loader's answer is asserted
                # below: absent, never an exception.
                continue
            facts = by_id[row_id]
            distance, state = sql[row_id]
            assert facts.sea_distance_m == (
                float(distance) if distance is not None else None
            ), name
            assert facts.sea_view_state == state, name
        assert by_id[ids["centroid"]].sea_distance_m == 620.0
        assert by_id[ids["legacy_true"]].sea_view_state == "likely"
        assert by_id[ids["junk"]].sea_distance_m is None
        assert by_id[ids["junk"]].sea_view_state is None

    def test_the_nearest_reference_wins_and_a_rankable_one_beats_a_thin_one(
        self, world
    ):
        pid = world["pid"]
        # A second favorite in Ponteceso, and a row beside it.
        second = _mk(pid, is_favorite=True, coords=PONTECESO, price=200000)
        beside = _mk(pid, coords=PONTECESO, price=200000)
        # A third favorite nobody can place: every comparison against it is
        # thin, and must never outrank a rankable one however high it reads.
        _mk(pid, is_favorite=True, municipality="Ares", price=200000)
        ctx = fs.build_context()
        reading = ctx.read(beside.id)
        assert reading["state"] == fs.STATE_OK
        assert reading["reference_id"] == second.id
        assert reading["reference_count"] == 3

    def test_a_duplicate_of_a_favorite_never_sorts_above_it(self, app):
        """The table holds the same house under two portals. A copy scores
        100 on every fact; the favorite still leads, whatever their ids."""
        profile = _profile("Twins")
        copy = _mk(
            profile.id,
            coords=MALPICA,
            location_accuracy="precise",
            attributes={"bedrooms": 5, "bathrooms": 2},
        )
        favorite = _mk(
            profile.id,
            is_favorite=True,
            coords=MALPICA,
            location_accuracy="precise",
            attributes={"bedrooms": 5, "bathrooms": 2},
        )
        assert copy.id < favorite.id
        ctx = fs.build_context()
        assert ctx.read(copy.id)["score"] == 100.0
        keys = ctx.sort_keys()
        assert keys[favorite.id] > keys[copy.id] == 100.0

    def test_references_are_per_subscription(self, world):
        """A favorite elsewhere is nobody else's reference, and a
        subscription without one has nothing to compare against."""
        other = _profile("Asturias")
        row = _mk(other.id, coords=MALPICA)
        ctx = fs.build_context()
        assert ctx.read(row.id)["state"] == fs.STATE_NO_REFERENCE
        assert ctx.reference_count_for(other.id) == 0
        assert ctx.reference_count_for(world["pid"]) == 1
        # Scoped to the other subscription alone, the context is dormant.
        assert fs.build_context(profile_ids=[other.id]) is None

    def test_an_unassigned_row_has_no_reference(self, world):
        row = _mk(None, coords=MALPICA)
        assert fs.build_context().read(row.id)["state"] == fs.STATE_NO_REFERENCE

    def test_the_municipality_point_reads_every_subscription(self, world):
        """A located row in another subscription still places this one."""
        other = _profile("Asturias")
        _mk(other.id, municipality="Carballo", coords=PONTECESO)
        unplaced = _mk(world["pid"], municipality="Carballo")
        reading = fs.build_context().read(unplaced.id)
        assert reading["geography_basis"] == fs.BASIS_MUNICIPALITY
        assert reading["state"] == fs.STATE_OK


class TestTheClause:
    def test_an_unknown_value_hands_back_the_same_query(self, world):
        ctx = fs.build_context()
        base = Property.query
        assert fs.apply_filter(base, Property, ctx, "banana") is base
        assert fs.apply_filter(base, Property, ctx, "") is base
        assert fs.read_filter_cut("70 ") == 70.0

    def test_a_known_cut_narrows_even_without_a_favorite_anywhere(self, app):
        profile = _profile()
        _mk(profile.id, coords=MALPICA)
        assert fs.build_context() is None
        narrowed = fs.apply_filter(Property.query, Property, None, "70")
        assert narrowed is not Property.query
        assert narrowed.count() == 0
        assert fs.similar_count(Property.query, Property, None, "70") == 0

    def test_the_sort_key_is_null_for_everything_that_does_not_rank(self, world):
        ctx = fs.build_context()
        ordered = [
            row.id
            for row in Property.query.order_by(
                fs.sort_expression(Property, ctx).desc().nullslast(),
                Property.id.asc(),
            )
        ]
        ids = world["ids"]
        # 100, then 89.9 (the unlocated row: price, area and location only,
        # every one close), then 85.0 (the neighbour, whose sea view
        # disagrees), then 62.5 (Vigo: 0 on location).
        assert ordered[:3] == [ids["favorite"], ids["unlocated"], ids["neighbour"]]
        assert ordered[3] == ids["far"]
        assert set(ordered[4:]) == {ids["plot"], ids["nowhere"]}
        ascending = [
            row.id
            for row in Property.query.order_by(
                fs.sort_expression(Property, ctx).asc().nullslast(),
                Property.id.asc(),
            )
        ]
        assert ascending[:4] == [
            ids["far"],
            ids["neighbour"],
            ids["unlocated"],
            ids["favorite"],
        ]
        assert set(ascending[4:]) == {ids["plot"], ids["nowhere"]}


def _count(body: str) -> int:
    match = re.search(r"<strong>(\d+) properties found</strong>", body)
    assert match, "the page did not print a result count"
    return int(match.group(1))


def _shown(body: str) -> list[int]:
    return [
        int(pid) for pid in dict.fromkeys(re.findall(r'data-property-id="(\d+)"', body))
    ]


def _coverage_line(body: str):
    match = re.search(
        r'id="similarity-coverage"[^>]*>\s*<i[^>]*></i>(.*?)</span>', body, re.DOTALL
    )
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip() if match else None


class TestTheList:
    def test_the_cut_keeps_the_favorite_and_the_rows_at_or_above_it(
        self, client, world
    ):
        body = client.get("/properties?profile_id=all&similar=70").get_data(
            as_text=True
        )
        ids = world["ids"]
        assert _count(body) == 3
        assert _shown(body) == [ids["favorite"], ids["unlocated"], ids["neighbour"]]
        assert _coverage_line(body) == "Similar: 2 at ≥ 70 to 1 favorite"

    def test_under_the_favorites_switch_the_line_says_what_hides_the_rows(
        self, client, world
    ):
        """The owner's own URL carries `favorites=on`; picking a cut there
        shows the favorites alone, and "Similar: 0" would read as "nothing
        resembles them"."""
        body = client.get(
            "/properties?profile_id=all&favorites=on&similar=70"
        ).get_data(as_text=True)
        assert _shown(body) == [world["ids"]["favorite"]]
        assert _coverage_line(body) == (
            "Similar: 2 at ≥ 70 to 1 favorite — hidden by the Favorites switch"
        )

    def test_a_known_cut_sorts_most_alike_first_unless_a_sort_is_named(
        self, client, world
    ):
        ids = world["ids"]
        body = client.get(
            "/properties?profile_id=all&similar=60&per_page=100"
        ).get_data(as_text=True)
        # References first, then by likeness: the unlocated row (89.9)
        # before the neighbour (85.0) before Vigo (62.5, kept by this cut).
        assert _shown(body) == [
            ids["favorite"],
            ids["unlocated"],
            ids["neighbour"],
            ids["far"],
        ]
        body = client.get(
            "/properties?profile_id=all&similar=60&sort=similarity&order=asc"
        ).get_data(as_text=True)
        assert _shown(body) == [
            ids["far"],
            ids["neighbour"],
            ids["unlocated"],
            ids["favorite"],
        ]
        # A named sort is the owner's: by price, cheapest first, the two
        # 290k rows tied and broken by id.
        body = client.get(
            "/properties?profile_id=all&similar=60&sort=price&order=asc"
        ).get_data(as_text=True)
        assert _shown(body) == [
            ids["unlocated"],
            ids["neighbour"],
            ids["favorite"],
            ids["far"],
        ]

    def test_the_sort_alone_ranks_the_whole_page_with_the_unrankable_last(
        self, client, world
    ):
        ids = world["ids"]
        body = client.get(
            "/properties?profile_id=all&sort=similarity&order=desc&per_page=100"
        ).get_data(as_text=True)
        shown = _shown(body)
        assert shown[:4] == [
            ids["favorite"],
            ids["unlocated"],
            ids["neighbour"],
            ids["far"],
        ]
        assert set(shown[4:]) == {ids["plot"], ids["nowhere"]}
        # Without a cut the line says what is rankable at all, and its
        # tooltip counts what a missing chip means on this page.
        assert _coverage_line(body) == "Similar: 3 of 6 rankable against 1 favorite"
        tooltip = re.search(r'id="similarity-coverage"[^>]*title="([^"]*)"', body)
        assert tooltip and unescape(tooltip.group(1)).startswith(
            "1 cannot be placed · 1 of a different kind · 0 with no favorite to "
            "compare to · 2 of the rankable rest on price, area and location "
            "alone · plot compared on 0."
        )

    def test_an_unknown_value_narrows_nothing_and_is_not_a_cut(self, client, world):
        body = client.get("/properties?profile_id=all&similar=banana").get_data(
            as_text=True
        )
        assert _count(body) == 6
        assert _coverage_line(body) == "Similar: 3 of 6 rankable against 1 favorite"
        assert "Filters:" not in body

    def test_a_chosen_mode_outranks_the_cuts_default_sort(self, client, world):
        """The cut moves only the bare date default: a mode named in the
        URL keeps its own score order, as the page script leaves a chosen
        sort alone."""
        ids = world["ids"]
        world["rows"]["far"].score_investment = 99
        world["rows"]["neighbour"].score_investment = 50
        world["rows"]["unlocated"].score_investment = 10
        db.session.commit()
        body = client.get(
            "/properties?profile_id=all&similar=60&mode=investment"
        ).get_data(as_text=True)
        assert _shown(body)[:3] == [ids["far"], ids["neighbour"], ids["unlocated"]]

    def test_without_a_favorite_the_similarity_sort_is_neither_offered_nor_applied(
        self, client, app
    ):
        """No favorite anywhere: there is no likeness to order by, so the
        option is absent and a typed `sort=similarity` falls back to the
        date order the select then shows -- never the tie-breaker under a
        label claiming similarity. The CSV and the API fall back alike."""
        profile = _profile("Asturias")
        older = _mk(profile.id, coords=MALPICA, price=100000)
        newer = _mk(profile.id, coords=VIGO, price=900000)
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert 'value="similarity"' not in body
        body = client.get(
            "/properties?profile_id=all&sort=similarity&order=asc"
        ).get_data(as_text=True)
        # Date ascending: the older row first; the select shows the date.
        assert _shown(body) == [older.id, newer.id]
        assert 'value="created_at" selected' in body
        assert 'value="similarity"' not in body
        rows = list(
            csv.DictReader(
                io.StringIO(
                    client.get(
                        "/properties/export.csv?profile_id=all&sort=similarity&order=asc"
                    ).get_data(as_text=True)
                )
            )
        )
        assert [int(row["ID"]) for row in rows] == [older.id, newer.id]
        payload = client.get(
            f"/api/properties?profile_id={profile.id}&sort=similarity&order=asc"
        ).get_json()
        assert [p["id"] for p in payload["properties"]] == [older.id, newer.id]

    def test_the_control_is_drawn_only_with_a_favorite_on_screen(self, client, world):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert 'id="similar"' in body
        assert 'value="similarity"' in body
        other = _profile("Asturias")
        _mk(other.id, coords=MALPICA)
        body = client.get(f"/properties?profile_id={other.id}").get_data(as_text=True)
        assert 'id="similar"' not in body
        assert _coverage_line(body) is None
        # An applied cut keeps its control on screen even with nothing to
        # compare to, so the page it emptied can be undone on the control
        # itself; the hint says why it keeps nothing.
        body = client.get(f"/properties?profile_id={other.id}&similar=70").get_data(
            as_text=True
        )
        assert 'id="similar"' in body
        assert 'value="70" selected' in body
        assert "no favorites yet, so this cut keeps nothing" in body
        assert _count(body) == 0
        assert _coverage_line(body) == "Similar: 0 at ≥ 70 to 0 favorites"
        # An unrecognised value is not a cut: it travels as a hidden input.
        body = client.get(f"/properties?profile_id={other.id}&similar=banana").get_data(
            as_text=True
        )
        assert 'name="similar" value="banana"' in body
        assert _count(body) == 1

    def test_the_chip_rides_beside_the_score_with_the_facts_it_rests_on(
        self, client, world
    ):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        chips = re.findall(
            r'class="badge d-block mt-1 similarity-chip" data-similarity-state="(\w+)"'
            r'[^>]*title="([^"]*)"[^>]*>&asymp; ([\d.]+)(?: <span class="opacity-75">'
            r"(\d+/\d+)</span>)?</span>",
            body,
        )
        by_state = {}
        for state, title, number, facts in chips:
            by_state.setdefault(state, []).append((unescape(title), number, facts))
        favorite = world["ids"]["favorite"]
        assert (
            f"Similarity 84.1 to favorite #{favorite}, on: price, built area, "
            "location, bedrooms, bathrooms, sea view",
            "84.1",
            "6/8",
        ) in by_state["ok"]
        assert any(
            title.startswith("Not ranked")
            and "price, built area" in title
            and number == "100.0"
            and facts == "2/8"
            for title, number, facts in by_state["thin"]
        )
        # Three rankable rows, one thin; the favorite, the plot draw none.
        assert len(by_state["ok"]) == 3
        assert len(by_state["thin"]) == 1

    def test_the_links_and_the_clear_link_carry_it(self, client, world):
        body = client.get(
            "/properties?profile_id=all&similar=70&sort=price&order=asc"
        ).get_data(as_text=True)
        price_header = re.search(r'href="(/properties\?[^"]*sort=price[^"]*)"', body)
        assert price_header and "similar=70" in unescape(price_header.group(1))
        clear = re.search(r'href="([^"]+)">clear filters</a>', body)
        assert clear and "similar=" not in unescape(clear.group(1))
        export = re.search(r'href="(/properties/export\.csv\?[^"]*)"', body)
        assert export and "similar=70" in unescape(export.group(1))

    def test_the_option_counts_follow_the_cut(self, client, world):
        """The municipality dropdown counts the page each option opens: under
        the cut, Vigo (the far row) is not offered, Malpica counts three."""
        body = client.get("/properties?profile_id=all&similar=70").get_data(
            as_text=True
        )
        options = dict(
            re.findall(
                r'<option value="([^"]+)"\s*(?:selected)?>\s*([^<]+?)\s*</option>', body
            )
        )
        labels = {v.strip(): k for k, v in options.items()}
        assert "Vigo" not in " ".join(labels)
        assert "Malpica de Bergantiños (3)" in labels

    def test_under_several_subscriptions_the_line_names_each_ones_favorites(
        self, client, world
    ):
        other = _profile("Asturias")
        _mk(other.id, is_favorite=True, coords=VIGO)
        _mk(other.id, coords=VIGO)
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        assert _coverage_line(body) == (
            "Similar: 4 of 8 rankable against each subscription's own favorites "
            "(2 in all)"
        )

    def test_spanish(self, client, world):
        with client.session_transaction() as session:
            session["language"] = "es"
        body = client.get("/properties?profile_id=all&similar=70").get_data(
            as_text=True
        )
        assert "Parecidos a ★: ≥ 70" in body
        assert _coverage_line(body) == "Parecidos: 2 con ≥ 70 respecto a 1 favorito"


class TestTheOtherSurfaces:
    def test_the_map_plots_exactly_the_kept_rows_with_coordinates(self, client, world):
        body = client.get("/map?profile_id=all&similar=70").get_data(as_text=True)
        markers = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
        ids = {
            int(pid) for pid in re.findall(r'"id":\s*(\d+)', unescape(markers.group(1)))
        }
        assert ids == {world["ids"]["favorite"], world["ids"]["neighbour"]}

    def test_the_csv_carries_the_columns_by_value_and_the_sort(self, client, world):
        response = client.get(
            "/properties/export.csv?profile_id=all&sort=similarity&order=desc"
        )
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        ids = world["ids"]
        by_id = {int(row["ID"]): row for row in rows}
        assert [int(row["ID"]) for row in rows][:4] == [
            ids["favorite"],
            ids["unlocated"],
            ids["neighbour"],
            ids["far"],
        ]
        neighbour = by_id[ids["neighbour"]]
        assert neighbour["Similarity State"] == "ok"
        assert float(neighbour["Similarity"]) == pytest.approx(84.1, abs=0.1)
        assert neighbour["Similarity Nearest Favorite"] == str(ids["favorite"])
        assert neighbour["Similarity Compared On"] == (
            "price area geography bedrooms bathrooms sea_view"
        )
        assert neighbour["Similarity Location Basis"] == "approximate"
        assert by_id[ids["favorite"]]["Similarity State"] == "reference"
        assert by_id[ids["plot"]]["Similarity State"] == "different_kind"
        assert by_id[ids["plot"]]["Similarity"] == ""
        assert by_id[ids["nowhere"]]["Similarity State"] == "thin"
        assert by_id[ids["unlocated"]]["Similarity Location Basis"] == "municipality"
        # And the cut narrows the file exactly as the page.
        response = client.get("/properties/export.csv?profile_id=all&similar=70")
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        assert {int(row["ID"]) for row in rows} == {
            ids["favorite"],
            ids["neighbour"],
            ids["unlocated"],
        }

    def test_the_api_applies_the_cut_and_says_what_it_measured_against(
        self, client, world
    ):
        pid = world["pid"]
        payload = client.get(f"/api/properties?profile_id={pid}&similar=70").get_json()
        ids = world["ids"]
        assert payload["scope"]["total"] == 3
        assert {p["id"] for p in payload["properties"]} == {
            ids["favorite"],
            ids["neighbour"],
            ids["unlocated"],
        }
        by_id = {p["id"]: p for p in payload["properties"]}
        assert by_id[ids["neighbour"]]["similarity"] == pytest.approx(84.1, abs=0.1)
        assert by_id[ids["neighbour"]]["similarity_state"] == "ok"
        assert by_id[ids["favorite"]]["similarity_state"] == "reference"
        assert "similar" in payload["scope"]["filters_read"]
        notes = " ".join(payload["scope"]["notes"])
        assert "similar=70" in notes and "1 favorite" in notes
        # The full shape carries the same two fields.
        full = client.get(
            f"/api/properties?profile_id={pid}&similar=70&full=1"
        ).get_json()
        assert {p["id"]: p["similarity_state"] for p in full["properties"]} == {
            p["id"]: p["similarity_state"] for p in payload["properties"]
        }
        # The sort is honored there too.
        ordered = client.get(
            f"/api/properties?profile_id={pid}&sort=similarity&order=desc&limit=200"
        ).get_json()
        assert [p["id"] for p in ordered["properties"]][:3] == [
            ids["favorite"],
            ids["unlocated"],
            ids["neighbour"],
        ]

    def test_the_api_names_an_unknown_value_and_an_empty_reference_set(
        self, client, world
    ):
        pid = world["pid"]
        payload = client.get(
            f"/api/properties?profile_id={pid}&similar=nope"
        ).get_json()
        assert payload["scope"]["total"] == 6
        assert any(
            "similar='nope'" in note and "80, 70, 60" in note
            for note in payload["scope"]["notes"]
        )
        other = _profile("Asturias")
        _mk(other.id, coords=MALPICA)
        payload = client.get(
            f"/api/properties?profile_id={other.id}&similar=70"
        ).get_json()
        assert payload["scope"]["total"] == 0
        assert any("holds no favorites" in note for note in payload["scope"]["notes"])
        # And without a cut the row still says it has no reference.
        payload = client.get(f"/api/properties?profile_id={other.id}").get_json()
        assert payload["properties"][0]["similarity_state"] == "no_reference"
        assert payload["properties"][0]["similarity"] is None


def _card(body: str) -> str:
    match = re.search(
        r'id="similarity-card"(.*?)<div class="card mb-4" id="taste-card"',
        body,
        re.DOTALL,
    )
    assert match, "the property page drew no similarity card"
    return re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(match.group(1)))
    ).strip()


class TestThePropertyPage:
    def test_the_neighbour_names_its_nearest_favorite_and_the_facts_by_value(
        self, client, world
    ):
        ids = world["ids"]
        text = _card(
            client.get(f"/properties/{ids['neighbour']}").get_data(as_text=True)
        )
        assert "84.1/100" in text
        assert f"nearest favorite: #{ids['favorite']}" in text
        assert "price 92, built area 96, location 9" in text
        assert "bedrooms 100, bathrooms 60, sea view 0" in text
        assert "location from the listing's approximate coordinate" in text
        assert "6 of 8 facts" in text
        # The municipality point says how many rows made it, by value.
        text = _card(
            client.get(f"/properties/{ids['unlocated']}").get_data(as_text=True)
        )
        assert "the municipality's located listings (3)" in text
        assert "3 of 8 facts" in text
        assert "Show the listings most similar to the favorites" in text

    def test_the_favorite_reads_as_a_reference(self, client, world):
        text = _card(
            client.get(f"/properties/{world['ids']['favorite']}").get_data(as_text=True)
        )
        assert "one of the subscription's favorites" in text
        assert "/100" not in text

    def test_the_unplaceable_row_says_it_is_not_ranked(self, client, world):
        text = _card(
            client.get(f"/properties/{world['ids']['nowhere']}").get_data(as_text=True)
        )
        assert "100.0/100" in text
        assert "Not ranked" in text

    def test_the_plot_and_a_subscription_without_favorites_say_why(self, client, world):
        text = _card(
            client.get(f"/properties/{world['ids']['plot']}").get_data(as_text=True)
        )
        assert "a different kind of listing" in text
        other = _profile("Asturias")
        row = _mk(other.id, coords=MALPICA)
        text = _card(client.get(f"/properties/{row.id}").get_data(as_text=True))
        assert "no favorites yet" in text
        assert "Show the listings" not in text
