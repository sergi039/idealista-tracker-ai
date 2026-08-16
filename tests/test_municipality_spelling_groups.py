"""One municipality is one filter option and one comparison row.

`properties.municipality` is free text out of Idealista's alert emails, and
the same place arrives spelled several ways. The rows seeded here are the
real ones, ids and spellings included, measured against the live database on
2026-08-16: 50 "Gijón" / 577 "Gijon", 38 "Carreño" / 560 "Carreno", and 121
"Muros De Nalón" / 612 "Muros de Nalon" / 210 "Muros de Nalón".

Two failure modes, both silent, both #98's shape:

* the /properties dropdown offered every spelling as its own municipality, so
  picking "Gijón" showed 57 of 73 listings with nothing saying the other 16
  existed;
* /municipalities keyed its grouping with `name.lower()`, which lowercases
  but does not strip accents, so one municipality rendered as two rows with
  two medians and two coverage counts.

What must NOT come with the fix is a truncated artifact (issue #298) being
folded into a full name: "Ovi..." normalizes to "ovi", which is nobody's key,
and a prefix match onto "Oviedo" is exactly the wrong-pick hazard
`resolve_truncated_municipality` refuses. The last class here pins that.
"""

import json
import re

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services import quality_of_life_service as qol_module  # noqa: E402
from services.municipality_comparison_service import (  # noqa: E402
    MunicipalityComparisonService,
)
from utils.municipality_codes import normalize  # noqa: E402
from utils.municipality_grouping import (  # noqa: E402
    group_key,
    group_municipalities,
    preferred_display,
)

# (id, municipality, price) -- the ids and spellings are the live ones.
LIVE_ROWS = [
    (50, "Gijón", 200000),
    (577, "Gijon", 260000),
    (38, "Carreño", 150000),
    (560, "Carreno", 170000),
    (121, "Muros De Nalón", 100000),
    (612, "Muros de Nalon", 120000),
    (210, "Muros de Nalón", 140000),
]


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


@pytest.fixture
def no_reference_files(tmp_path, monkeypatch):
    """Point the QoL reference loaders at an empty dir: hermetic, and the
    service already answers "not matched" for missing files."""
    for attr in ("INE_DATA_PATH", "CNH_DATA_PATH", "SEPE_DATA_PATH"):
        monkeypatch.setattr(qol_module, attr, str(tmp_path / f"{attr}.json"))
    return tmp_path


def _property(prop_id, municipality, price=None, profile_id=None):
    prop = Property(
        id=prop_id,
        source_email_id=f"spelling-{prop_id}",
        title=f"Listing {prop_id}",
        municipality=municipality,
        price=price,
        area=100,
        search_profile_id=profile_id,
        listing_status="active",
    )
    db.session.add(prop)
    db.session.commit()
    return prop


@pytest.fixture
def seeded(app):
    """The live spelling collisions, under one subscription."""
    profile = SearchProfile(
        name="Asturias",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    for prop_id, municipality, price in LIVE_ROWS:
        _property(prop_id, municipality, price, profile.id)
    return profile


def _listings_on_page(body):
    """Which listings the page actually shows.

    Matched on the row's `title="..."` attribute, which closes with a quote,
    so "Listing 12" cannot be read as a hit on "Listing 121".
    """
    return {
        prop_id
        for prop_id in [row[0] for row in LIVE_ROWS] + [900, 901]
        if f'Listing {prop_id}"' in body
    }


def _municipality_select(body):
    """The <select id="municipality"> options, as (value, selected) pairs."""
    block = re.search(
        r'<select[^>]*id="municipality"[^>]*>(.*?)</select>', body, re.DOTALL
    )
    assert block, "no municipality dropdown on the page"
    return [
        (value, "selected" in tail)
        for value, tail in re.findall(
            r'<option value="([^"]*)"([^>]*)>', block.group(1)
        )
    ]


class TestTheKeyIsShared:
    """The join key is the INE primitive, not a second normalizer."""

    def test_the_key_is_utils_municipality_codes_normalize(self):
        assert group_key("Gijón") == normalize("Gijón")

    @pytest.mark.parametrize(
        "left,right",
        [
            ("Gijón", "Gijon"),
            ("Carreño", "Carreno"),
            ("Castrillon", "Castrillón"),
            ("Muros De Nalón", "Muros de Nalon"),
            ("Muros de Nalón", "Muros de Nalon"),
            ("Soto del Barco", "Soto Del Barco"),
            ("Corvera de Asturias", "Corvera De Asturias"),
            ("Avilés", "Aviles"),
            ("Gozón", "Gozon"),
        ],
    )
    def test_the_live_spelling_pairs_share_one_key(self, left, right):
        assert group_key(left) == group_key(right)

    def test_two_municipalities_do_not(self):
        assert group_key("Gijón") != group_key("Gozón")

    def test_a_value_naming_nothing_has_no_key(self):
        assert group_key(None) is None
        assert group_key("   ") is None
        assert group_key("---") is None


class TestTheSpellingShown:
    """Grouping must not cost the owner a readable name."""

    def test_accents_beat_frequency(self):
        # 28 rows spell it "Castrillon" and 18 "Castrillón"; the accented one
        # is still the name of the place.
        assert preferred_display({"Castrillon": 28, "Castrillón": 18}) == "Castrillón"

    def test_spanish_casing_beats_frequency(self):
        assert (
            preferred_display(
                {"Muros De Nalón": 4, "Muros de Nalon": 2, "Muros de Nalón": 2}
            )
            == "Muros de Nalón"
        )

    def test_a_shouted_spelling_never_wins(self):
        # "MUROS DE NALON" is a regression from "Muros de Nalón" even when it
        # is the commonest form and carries no accent to lose.
        assert (
            preferred_display({"MUROS DE NALON": 99, "Muros de Nalón": 1})
            == "Muros de Nalón"
        )

    def test_the_choice_does_not_depend_on_row_order(self):
        counts = {"Muros De Nalón": 4, "Muros de Nalon": 2, "Muros de Nalón": 2}
        assert preferred_display(dict(reversed(list(counts.items())))) == (
            preferred_display(counts)
        )

    def test_the_shown_name_is_always_one_that_was_stored(self):
        counts = {"Gijon": 16, "Gijón": 57}
        assert preferred_display(counts) in counts


class TestGrouping:
    def test_one_group_per_municipality_with_the_combined_count(self):
        groups = group_municipalities(
            [("Gijón", 57), ("Gijon", 16), ("Carreño", 32), ("Carreno", 10)]
        )
        assert [(g.label, g.count) for g in groups] == [
            ("Carreño", 42),
            ("Gijón", 73),
        ]

    def test_a_group_keeps_every_stored_spelling_for_matching(self):
        (group,) = group_municipalities(
            [("Muros De Nalón", 4), ("Muros de Nalon", 2), ("Muros de Nalón", 2)]
        )
        assert group.count == 8
        assert set(group.spellings) == {
            "Muros De Nalón",
            "Muros de Nalon",
            "Muros de Nalón",
        }


class TestPropertiesFilter:
    def test_the_dropdown_offers_one_option_per_municipality(self, client, seeded):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert [value for value, _ in _municipality_select(body)] == [
            "",
            "Carreño",
            "Gijón",
            "Muros de Nalón",
        ]

    def test_each_option_carries_the_combined_count(self, client, seeded):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert "Gijón (2)" in body
        assert "Carreño (2)" in body
        assert "Muros de Nalón (3)" in body

    @pytest.mark.parametrize("spelling", ["Gijón", "Gijon"])
    def test_either_spelling_returns_the_whole_municipality(
        self, client, seeded, spelling
    ):
        body = client.get(
            "/properties", query_string={"profile_id": "all", "municipality": spelling}
        ).get_data(as_text=True)
        assert _listings_on_page(body) == {50, 577}

    def test_every_spelling_of_muros_lands_in_one_result_set(self, client, seeded):
        body = client.get(
            "/properties",
            query_string={"profile_id": "all", "municipality": "Muros de Nalon"},
        ).get_data(as_text=True)
        assert _listings_on_page(body) == {121, 612, 210}

    def test_the_option_shows_selected_whatever_spelling_the_url_carried(
        self, client, seeded
    ):
        """A bookmark holding the other spelling must not leave the control
        reading "All municipalities" over a filtered page."""
        body = client.get(
            "/properties", query_string={"profile_id": "all", "municipality": "Gijon"}
        ).get_data(as_text=True)
        assert [
            value for value, selected in _municipality_select(body) if selected
        ] == ["Gijón"]

    def test_the_export_filters_the_same_way(self, client, seeded):
        body = client.get(
            "/properties/export.csv",
            query_string={"profile_id": "all", "municipality": "Gijon"},
        ).get_data(as_text=True)
        assert "Listing 50" in body and "Listing 577" in body
        assert "Listing 38" not in body

    def test_the_json_api_filters_the_same_way(self, client, seeded):
        payload = json.loads(
            client.get(
                "/api/properties",
                query_string={"profile_id": seeded.id, "municipality": "Gijon"},
            ).get_data(as_text=True)
        )
        ids = {row["id"] for row in payload["properties"]}
        assert ids == {50, 577}

    def test_the_map_filters_the_same_way(self, client, seeded):
        # The map only carries rows with coordinates; what is pinned here is
        # that the filter reaches it at all, not the pin count.
        resp = client.get(
            "/map", query_string={"profile_id": "all", "municipality": "Gijon"}
        )
        assert resp.status_code == 200


class TestMunicipalitiesPage:
    def test_one_municipality_is_one_row(self, app, seeded, no_reference_files):
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        assert sorted(row["name"] for row in rows) == [
            "Carreño",
            "Gijón",
            "Muros de Nalón",
        ]

    def test_the_row_counts_every_spelling(self, app, seeded, no_reference_files):
        rows = {
            row["name"]: row
            for row in MunicipalityComparisonService().build_rows(Property.query.all())
        }
        assert rows["Gijón"]["listings"] == 2
        assert rows["Muros de Nalón"]["listings"] == 3

    def test_the_median_is_taken_over_the_whole_municipality(
        self, app, seeded, no_reference_files
    ):
        rows = {
            row["name"]: row
            for row in MunicipalityComparisonService().build_rows(Property.query.all())
        }
        # 100k / 120k / 140k: the median of all three, over 3 of 3 listings.
        assert rows["Muros de Nalón"]["price"]["median"] == 120000
        assert rows["Muros de Nalón"]["price"]["measured"] == 3
        assert rows["Muros de Nalón"]["price"]["total"] == 3

    def test_the_page_renders_one_row_per_municipality(
        self, client, seeded, no_reference_files
    ):
        body = client.get("/municipalities").get_data(as_text=True)
        assert body.count(">Gijón<") == 1
        assert ">Gijon<" not in body
        assert ">Muros De Nalón<" not in body
        assert ">Muros de Nalon<" not in body

    def test_the_name_sort_is_accent_insensitive(self, app, seeded, no_reference_files):
        service = MunicipalityComparisonService()
        rows = service.sort_rows(
            service.build_rows(Property.query.all()), "municipality", descending=False
        )
        assert [row["name"] for row in rows] == [
            "Carreño",
            "Gijón",
            "Muros de Nalón",
        ]


class TestTruncationSurvives:
    """Issue #298's guarantee, under the new grouping."""

    @pytest.fixture
    def with_a_truncated_row(self, seeded):
        _property(900, "Oviedo", 300000, seeded.id)
        _property(901, "Ovi...", 310000, seeded.id)
        return seeded

    def test_a_truncated_value_is_not_normalised_into_a_municipality(self):
        assert group_key("Ovi...") is None
        assert normalize("Ovi...") != normalize("Oviedo")

    def test_filtering_by_the_full_name_does_not_swallow_it(
        self, client, with_a_truncated_row
    ):
        body = client.get(
            "/properties", query_string={"profile_id": "all", "municipality": "Oviedo"}
        ).get_data(as_text=True)
        assert _listings_on_page(body) == {900}

    def test_the_truncated_row_is_still_reachable_by_its_own_value(
        self, client, with_a_truncated_row
    ):
        body = client.get(
            "/properties", query_string={"profile_id": "all", "municipality": "Ovi..."}
        ).get_data(as_text=True)
        assert _listings_on_page(body) == {901}
        # The applied-choice rule: the control agrees with its query.
        assert ("Ovi...", True) in _municipality_select(body)

    def test_it_is_never_offered_as_a_municipality_of_its_own(
        self, client, with_a_truncated_row
    ):
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        offered = [value for value, _ in _municipality_select(body)]
        assert "Oviedo" in offered
        assert "Ovi..." not in offered

    def test_the_comparison_still_counts_it_aside(
        self, app, with_a_truncated_row, no_reference_files
    ):
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        assert "Ovi..." not in [row["name"] for row in rows]
        body = app.test_client().get("/municipalities").get_data(as_text=True)
        assert "1 listings carry no municipality" in body
