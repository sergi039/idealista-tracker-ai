"""Similar to the favorites: the reading, and every surface that shows it.

The owner asked (2026-09-02) for a filter that selects, from the whole
Galicia subscription, the listings most alike the two they had starred.
`services/favorite_similarity.py` is that reading: the favorites of the
row's OWN subscription are the references, every fact both sides state is
compared and every fact one side lacks abstains (#98), the nearest reference
wins, and the number rests on a location or it does not rank at all.

What is pinned here, and why each case is here:

* **By value.** The neighbour's 85.0 is hand-computed from the module's own
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
        # the same bedrooms, one more bathroom, 100 m further from the sea,
        # and no sea view where the favorite likely has one.
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
            "sea_distance",
            "sea_view",
        ]
        parts = reading["components"]
        assert parts["price"] == pytest.approx(_ratio(275000, 290000), abs=0.05)
        assert parts["area"] == pytest.approx(_ratio(292, 300), abs=0.05)
        # 5 bedrooms both; 3 bathrooms against 2 (a string in the JSON, parsed
        # not cast); 500 m against 400 m from the sea; no view against likely.
        assert parts["bedrooms"] == 100.0
        assert parts["bathrooms"] == 60.0
        assert parts["sea_distance"] == pytest.approx(95.0, abs=0.05)
        assert parts["sea_view"] == 0.0
        # 3.3 km apart on a 60 km scale.
        assert 94.0 <= parts["geography"] <= 95.0
        assert reading["score"] == pytest.approx(_weighted(parts), abs=0.06)
        assert reading["coverage"] == pytest.approx(
            (fs.TOTAL_WEIGHT - fs.WEIGHTS["plot"]) / fs.TOTAL_WEIGHT, abs=0.001
        )
        # The approximate coordinate is the row's own point, labelled as
        # the locality's.
        assert reading["geography_basis"] == fs.BASIS_LOCALITY

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
        assert reading["components"]["geography"] == pytest.approx(94.5, abs=0.1)
        # (3 x 78.6 + 2 x 100 + 3 x 94.5) / 8
        assert reading["score"] == pytest.approx(89.9, abs=0.1)

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

    def test_the_plot_is_the_criteria_modules_reading(self, app):
        """Bare land: `area` IS the plot where `plot_area` is unstated."""
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
        assert _coverage_line(body) is None

    def test_an_unknown_value_narrows_nothing_and_says_nothing(self, client, world):
        body = client.get("/properties?profile_id=all&similar=banana").get_data(
            as_text=True
        )
        assert _count(body) == 6
        assert _coverage_line(body) is None
        assert "Filters:" not in body

    def test_the_control_is_drawn_only_with_a_favorite_on_screen(self, client, world):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert 'id="similar"' in body
        assert 'value="similarity"' in body
        other = _profile("Asturias")
        _mk(other.id, coords=MALPICA)
        body = client.get(f"/properties?profile_id={other.id}").get_data(as_text=True)
        assert 'id="similar"' not in body
        # A cut typed into the URL still travels with the form, like `source`.
        body = client.get(f"/properties?profile_id={other.id}&similar=70").get_data(
            as_text=True
        )
        assert 'name="similar" value="70"' in body
        assert _count(body) == 0

    def test_the_chip_rides_beside_the_score_with_the_facts_it_rests_on(
        self, client, world
    ):
        body = client.get("/properties?profile_id=all&per_page=100").get_data(
            as_text=True
        )
        chips = re.findall(
            r'class="badge d-block mt-1 similarity-chip" data-similarity-state="(\w+)"'
            r'[^>]*title="([^"]*)"[^>]*>&asymp; (\d+)</span>',
            body,
        )
        by_state = {}
        for state, title, number in chips:
            by_state.setdefault(state, []).append((unescape(title), int(number)))
        favorite = world["ids"]["favorite"]
        assert (
            f"Similarity 85 to favorite #{favorite}, on: price, built area, location, "
            "bedrooms, bathrooms, distance to the sea, sea view",
            85,
        ) in by_state["ok"]
        assert any(
            title.startswith("Not ranked")
            and "price, built area" in title
            and number == 100
            for title, number in by_state["thin"]
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
        assert float(neighbour["Similarity"]) == pytest.approx(85.0, abs=0.1)
        assert neighbour["Similarity Nearest Favorite"] == str(ids["favorite"])
        assert neighbour["Similarity Compared On"] == (
            "price area geography bedrooms bathrooms sea_distance sea_view"
        )
        assert neighbour["Similarity Location Basis"] == "locality"
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
        assert by_id[ids["neighbour"]]["similarity"] == pytest.approx(85.0, abs=0.1)
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
        assert "85/100" in text
        assert f"nearest favorite: #{ids['favorite']}" in text
        assert "price 92, built area 96, location 9" in text
        assert "bedrooms 100, bathrooms 60, distance to the sea 95, sea view 0" in text
        assert "location from the locality centroid" in text
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
        assert "100/100" in text
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
