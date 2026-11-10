"""The one rule for "publications of the same home" (#633).

Pure unit tests on lightweight rows, plus real `Property` rows on SQLite for
the CLI. The numbers in the fixtures follow the 2026-11-10 measurement in the
module docstring: a shared idealista/yaencontre id is the same home, an
idealista `area` is never evidence, three fields alone are only `possible`.
"""

import itertools
import random
from types import SimpleNamespace

import pytest

from app import create_app, db
from models import Property, PropertyActivity
from services import property_duplicates as dup
from tests import setup_test_environment
from utils import report_duplicates

setup_test_environment()

_IDS = itertools.count(1000)


def row(**kw):
    """A lightweight listing row; every field defaults to 'not stated'."""
    base = dict(
        id=next(_IDS),
        url=None,
        source_email_id="x",
        idealista_property_id=None,
        municipality="Bergondo",
        price=200000,
        previous_price=None,
        area=None,
        title=None,
        location_lat=None,
        location_lon=None,
        location_accuracy="unknown",
        enrichment=None,
        cadastral_reference=None,
        is_favorite=False,
        owner_verdict=None,
        owner_verdict_reason=None,
        owner_verdict_at=None,
        next_action=None,
        next_action_due_on=None,
        listing_status="active",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def yae(listing_id, **kw):
    return row(
        url=f"https://www.yaencontre.com/venta/casas/inmueble-46389-{listing_id}",
        source_email_id=f"yaencontre:{listing_id}",
        **kw,
    )


def ide(listing_id, **kw):
    return row(
        url=f"https://www.idealista.com/inmueble/{listing_id}/",
        idealista_property_id=listing_id,
        **kw,
    )


def foto(listing_id, **kw):
    return row(
        url=f"https://www.fotocasa.es/es/comprar/vivienda/x/{listing_id}/d", **kw
    )


def tier(a, b):
    return dup.explain(a, b)["tier"]


YAE_TITLE = "Chalet en venta en calle Fiobre, Bergondo"
FOTO_TITLE = "House for sale in calle Fiobre, Bergondo"


class TestConfirmed:
    def test_a_shared_id_across_idealista_and_yaencontre_is_confirmed(self):
        # price and area differ: 4 and 44 of 47 measured pairs.
        a = ide(93000001, price=300000, area=150)
        b = yae(93000001, price=290000, area=110, municipality="Elsewhere")
        assert tier(a, b) == dup.CONFIRMED

    def test_the_yaencontre_id_falls_back_to_source_email_id(self):
        a = ide(93000002)
        b = row(
            url="https://www.yaencontre.com/not-a-detail-page",
            source_email_id="yaencontre:93000002",
        )
        assert tier(a, b) == dup.CONFIRMED

    def test_the_idealista_id_falls_back_to_the_url(self):
        a = row(url="https://www.idealista.com/inmueble/93000003/")
        assert tier(a, yae(93000003)) == dup.CONFIRMED

    def test_different_ids_are_not_confirmed(self):
        assert tier(ide(93000004), yae(93000005)) is None

    def test_the_same_id_on_other_portals_is_not_the_pair(self):
        assert tier(foto(93000006), yae(93000006)) is None

    def test_the_seed_table_is_confirmed_with_source_seed(self):
        a, b = yae(106962112), ide(93478124)
        result = dup.explain(a, b)
        assert result["tier"] == dup.CONFIRMED
        assert result["signals"]["seed"]["status"] == dup.MET

    def test_a_unit_level_cadastral_reference_is_confirmed(self):
        ref = "9872023VH5797S0001WX"
        a = foto(1, cadastral_reference=ref)
        b = foto(2, cadastral_reference=ref.lower())
        assert tier(a, b) == dup.CONFIRMED

    def test_a_parcel_level_reference_does_not_identify_one_home(self):
        a = foto(1, cadastral_reference="9872023VH5797S", price=1)
        b = foto(2, cadastral_reference="9872023VH5797S", price=2)
        result = dup.explain(a, b)
        assert result["tier"] is None
        assert result["signals"]["cadastral_reference"]["status"] == dup.ABSTAINED

    def test_no_reference_abstains(self):
        result = dup.explain(foto(1), foto(2, price=5))
        assert result["signals"]["cadastral_reference"]["status"] == dup.ABSTAINED


class TestCadastralVeto:
    REF1, REF2 = "9872023VH5797S0001WX", "9872023VH5797S0002WX"

    def pair(self, ref_a, ref_b):
        a = yae(1, title=YAE_TITLE, area=120, price=200000, cadastral_reference=ref_a)
        b = foto(2, title=FOTO_TITLE, area=120, price=200000, cadastral_reference=ref_b)
        return a, b

    def test_different_unit_references_veto_a_corroborated_match(self):
        a, b = self.pair(self.REF1, self.REF2)
        result = dup.explain(a, b)
        assert result["tier"] is None
        assert result["verdict"] == "different"
        assert result["vetoed_by"] == "cadastral_reference"
        assert dup.edges([a, b]) == []
        assert dup.groups([a, b]) == []

    def test_the_veto_outranks_a_shared_listing_id(self):
        a = ide(93555555, cadastral_reference=self.REF1)
        b = yae(93555555, cadastral_reference=self.REF2)
        assert dup.edges([a, b]) == []

    def test_a_parcel_level_reference_on_either_side_does_not_veto(self):
        for ref_a, ref_b in [(self.REF1, "9872023VH5797S"), ("9872023VH5797S", None)]:
            a, b = self.pair(ref_a, ref_b)
            assert tier(a, b) == dup.PROBABLE
            assert "verdict" not in dup.explain(a, b)


class TestProbable:
    def three(self, **kw):
        a = yae(1, title=YAE_TITLE, area=120, **kw)
        b = foto(2, title=FOTO_TITLE, area=120, **kw)
        return a, b

    def test_three_fields_and_the_title_place_is_probable(self):
        a, b = self.three()
        result = dup.explain(a, b)
        assert result["tier"] == dup.PROBABLE
        assert result["signals"]["title_place"] == {
            "status": dup.MET,
            "value": "calle fiobre",
        }

    def test_three_fields_without_corroboration_is_only_possible(self):
        a = yae(1, area=120)
        b = foto(2, area=120)
        assert tier(a, b) == dup.POSSIBLE

    def test_a_different_place_in_the_title_does_not_corroborate(self):
        a = yae(1, title=YAE_TITLE, area=120)
        b = foto(2, title="House for sale in calle Outra, Bergondo", area=120)
        assert tier(a, b) == dup.POSSIBLE

    def test_equal_house_numbers_corroborate(self):
        a = yae(1, title="Casa en venta en calle Fiobre, 12, Bergondo", area=120)
        b = foto(2, title="House for sale in calle Fiobre, 12, Bergondo", area=120)
        assert tier(a, b) == dup.PROBABLE

    def test_a_number_on_one_side_only_is_not_corroboration(self):
        a = yae(1, title="Casa en venta en calle Fiobre, 12, Bergondo", area=120)
        b = foto(2, title=FOTO_TITLE, area=120)
        assert tier(a, b) == dup.POSSIBLE

    def test_streets_that_differ_only_by_a_digit_do_not_corroborate(self):
        a = yae(1, title="Casa en venta en calle 1, Bergondo", area=120)
        b = foto(2, title="House for sale in calle 2, Bergondo", area=120)
        assert dup.place_phrase(a) == "calle 1"
        assert tier(a, b) == dup.POSSIBLE

    def test_km_and_parcela_numbers_are_part_of_the_place(self):
        a = yae(1, title="Casa en venta en carretera km 3, Bergondo", area=120)
        b = foto(2, title="House for sale in carretera km 4, Bergondo", area=120)
        assert tier(a, b) == dup.POSSIBLE

    def test_a_postcode_component_is_not_part_of_the_place(self):
        a = yae(1, title="Casa en venta en calle Fiobre, 15165, Bergondo", area=120)
        b = foto(2, title=FOTO_TITLE, area=120)
        assert tier(a, b) == dup.PROBABLE

    @pytest.mark.parametrize(
        "word", ["calle", "lugar", "rua", "avenida", "camino", "barrio"]
    )
    def test_a_generic_street_word_alone_abstains(self, word):
        a = yae(1, title=f"Casa en venta en {word}, Bergondo", area=120)
        b = foto(2, title=f"House for sale in {word}, Bergondo", area=120)
        assert dup.place_phrase(a) is None
        result = dup.explain(a, b)
        assert result["signals"]["title_place"]["status"] == dup.ABSTAINED
        assert result["tier"] == dup.POSSIBLE

    def test_a_title_naming_only_the_municipality_does_not_corroborate(self):
        a = yae(1, title="Casa en venta en Bergondo", area=120)
        b = foto(2, title="House for sale in Bergondo", area=120)
        assert tier(a, b) == dup.POSSIBLE

    def test_a_title_with_no_place_marker_abstains(self):
        title = "Magnifica casa de piedra con finca"
        a, b = yae(1, title=title, area=120), foto(2, title=title, area=120)
        result = dup.explain(a, b)
        assert result["signals"]["title_place"]["status"] == dup.ABSTAINED
        assert result["tier"] == dup.POSSIBLE

    def test_close_pins_on_one_portal_corroborate(self):
        a = foto(
            1,
            area=120,
            location_accuracy="precise",
            location_lat=43.0,
            location_lon=-8.0,
        )
        b = foto(
            2,
            area=120,
            location_accuracy="precise",
            location_lat=43.0009,
            location_lon=-8.0,
        )
        assert dup.explain(a, b)["signals"]["coordinates"]["value"] < 200
        assert tier(a, b) == dup.PROBABLE

    def test_the_same_distance_is_too_far_on_one_portal_but_fine_across_two(self):
        # ~500 m apart
        kw = dict(area=120, location_accuracy="precise", location_lon=-8.0)
        same = (foto(1, location_lat=43.0, **kw), foto(2, location_lat=43.0045, **kw))
        cross = (foto(3, location_lat=43.0, **kw), yae(4, location_lat=43.0045, **kw))
        assert tier(*same) == dup.POSSIBLE
        assert tier(*cross) == dup.PROBABLE

    def test_beyond_the_cross_portal_radius_is_possible(self):
        kw = dict(area=120, location_accuracy="precise", location_lon=-8.0)
        a = foto(1, location_lat=43.0, **kw)
        b = yae(2, location_lat=43.02, **kw)
        assert tier(a, b) == dup.POSSIBLE

    def test_missing_coordinates_abstain_and_never_corroborate(self):
        a = foto(
            1,
            area=120,
            location_accuracy="precise",
            location_lat=43.0,
            location_lon=-8.0,
        )
        b = foto(2, area=120)
        result = dup.explain(a, b)
        assert result["signals"]["coordinates"]["status"] == dup.ABSTAINED
        assert result["tier"] == dup.POSSIBLE

    def test_a_shared_locality_centroid_does_not_corroborate(self):
        kw = dict(
            area=120,
            location_accuracy="approximate",
            location_lat=43.0,
            location_lon=-8.0,
        )
        assert tier(foto(1, **kw), foto(2, **kw)) == dup.POSSIBLE

    def test_a_different_municipality_spelling_with_the_same_key_matches(self):
        a = yae(1, title=YAE_TITLE, area=120, municipality="Coruña (A)")
        b = foto(2, title=FOTO_TITLE, area=120, municipality="A Coruña")
        assert tier(a, b) == dup.PROBABLE

    def test_a_different_municipality_is_not_a_match(self):
        a, b = self.three()
        b.municipality = "Cambre"
        assert tier(a, b) is None

    def test_a_missing_municipality_abstains(self):
        a, b = self.three()
        b.municipality = None
        result = dup.explain(a, b)
        assert result["tier"] is None
        assert result["signals"]["municipality"]["status"] == dup.ABSTAINED


class TestArea:
    def test_idealista_area_never_corroborates(self):
        a = ide(7, area=150, title=YAE_TITLE)
        b = foto(8, area=150, title=FOTO_TITLE)
        result = dup.explain(a, b)
        assert result["signals"]["area"]["status"] == dup.ABSTAINED
        assert result["tier"] is None

    def test_idealista_area_never_vetoes(self):
        # the only reason the pair is not probable is the idealista side: the
        # shared id still confirms it whatever the areas say.
        a, b = ide(9, area=150, price=1), yae(9, area=80, price=2)
        assert tier(a, b) == dup.CONFIRMED

    def test_idealista_area_equal_to_a_placeholder_is_not_read(self):
        assert dup.built_area(ide(1, area=150)) is None
        assert dup.built_area(foto(1, area=150)) == 150.0

    def test_unknown_area_means_three_fields_are_not_met(self):
        a = yae(1, title=YAE_TITLE, area=None)
        b = foto(2, title=FOTO_TITLE, area=120)
        assert tier(a, b) is None

    def test_different_areas_are_not_a_match(self):
        a = yae(1, title=YAE_TITLE, area=120)
        b = foto(2, title=FOTO_TITLE, area=121)
        assert tier(a, b) is None


class TestPrice:
    def test_a_price_drop_on_one_member_does_not_dissolve_the_pair(self):
        a = yae(1, title=YAE_TITLE, area=120, price=190000, previous_price=200000)
        b = foto(2, title=FOTO_TITLE, area=120, price=200000)
        assert tier(a, b) == dup.PROBABLE

    def test_different_prices_are_not_a_match(self):
        a = yae(1, title=YAE_TITLE, area=120, price=190000)
        b = foto(2, title=FOTO_TITLE, area=120, price=200000)
        assert tier(a, b) is None

    def test_a_missing_price_abstains(self):
        a = yae(1, title=YAE_TITLE, area=120, price=None)
        b = foto(2, title=FOTO_TITLE, area=120)
        result = dup.explain(a, b)
        assert result["signals"]["price"]["status"] == dup.ABSTAINED
        assert result["tier"] is None


class TestGroups:
    def test_possible_edges_are_reported_but_never_grouped(self):
        rows = [yae(1, area=120), foto(2, area=120)]
        assert [e.tier for e in dup.edges(rows)] == [dup.POSSIBLE]
        assert dup.groups(rows) == []

    def test_possible_cannot_be_requested_as_a_group_tier(self):
        with pytest.raises(ValueError):
            dup.groups([], tiers=(dup.POSSIBLE,))

    def test_probable_edges_group_and_confirmed_only_excludes_them(self):
        rows = [
            yae(1, title=YAE_TITLE, area=120),
            foto(2, title=FOTO_TITLE, area=120),
        ]
        assert len(dup.groups(rows)) == 1
        assert dup.groups(rows, tiers=(dup.CONFIRMED,)) == []

    def test_chaining_does_not_walk_across_prices(self):
        # A (dropped 200k -> 190k) shares 200k with B and 190k with C, but B
        # and C share nothing: the component {A, B, C} walks across prices, so
        # only the edge between rows of the same current price survives.
        a = yae(1, title=YAE_TITLE, area=120, price=190000, previous_price=200000)
        b = foto(2, title=FOTO_TITLE, area=120, price=200000)
        c = foto(3, title=FOTO_TITLE, area=120, price=190000)
        assert {(e.a, e.b) for e in dup.edges([a, b, c])} == {
            (a.id, b.id),
            (a.id, c.id),
        }
        assert [g.ids for g in dup.groups([a, b, c])] == [(a.id, c.id)]

    def test_a_price_drop_pair_alone_still_groups(self):
        a = yae(1, title=YAE_TITLE, area=120, price=190000, previous_price=200000)
        b = foto(2, title=FOTO_TITLE, area=120, price=200000)
        assert [g.ids for g in dup.groups([a, b])] == [(a.id, b.id)]

    def test_chaining_does_not_cross_areas(self):
        a = yae(1, title=YAE_TITLE, area=120)
        b = foto(2, title=FOTO_TITLE, area=120)
        c = foto(3, title=FOTO_TITLE, area=130)
        assert [g.ids for g in dup.groups([a, b, c])] == [(a.id, b.id)]

    def test_a_confirmed_edge_may_join_blocks(self):
        a = ide(5, price=100, municipality="Foz")
        b = yae(5, price=999, municipality="Burela")
        found = dup.groups([a, b])
        assert [g.ids for g in found] == [(a.id, b.id)]
        assert found[0].tier == dup.CONFIRMED

    def test_determinism_under_shuffled_input(self):
        rows = [
            ide(11),
            yae(11),
            yae(21, title=YAE_TITLE, area=120),
            foto(22, title=FOTO_TITLE, area=120),
            foto(23, area=999),
            ide(31, municipality="Foz"),
            yae(31, municipality="Foz"),
        ]
        expected = [g.ids for g in dup.groups(rows)]
        assert expected
        rng = random.Random(633)
        for _ in range(5):
            shuffled = rows[:]
            rng.shuffle(shuffled)
            assert [g.ids for g in dup.groups(shuffled)] == expected
            assert dup.edges(shuffled) == dup.edges(rows)


class TestAVetoedPairNeverSharesAGroup:
    REF1, REF2 = "9872023VH5797S0001WX", "9872023VH5797S0002WX"

    def trio(self):
        a = ide(93555555, cadastral_reference=self.REF1)
        b = yae(93555555, title=YAE_TITLE, area=120)
        c = foto(2, title=FOTO_TITLE, area=120, cadastral_reference=self.REF2)
        return a, b, c

    def test_the_intermediate_row_cannot_bridge_a_cadastral_veto(self):
        a, b, c = self.trio()
        assert {e.tier for e in dup.edges([a, b, c])} == {dup.CONFIRMED, dup.PROBABLE}
        refused = []
        found = dup.groups([a, b, c], refused=refused)
        assert [g.ids for g in found] == [(a.id, b.id)]
        assert [(e.a, e.b) for e in refused] == [(b.id, c.id)]
        assert c.id not in dup.folded_ids(found)

    def test_an_injected_forbidden_pair_does_the_same(self):
        a = ide(93666666)
        b = yae(93666666, title=YAE_TITLE, area=120)
        c = foto(2, title=FOTO_TITLE, area=120)
        assert [g.ids for g in dup.groups([a, b, c])] == [(a.id, b.id, c.id)]
        found = dup.groups([a, b, c], forbidden_pairs={(a.id, c.id)})
        assert [g.ids for g in found] == [(a.id, b.id)]
        assert c.id not in dup.folded_ids(found)

    def test_the_stronger_edge_wins_whatever_the_input_order(self):
        rows = list(self.trio())
        rng = random.Random(633)
        for _ in range(6):
            rng.shuffle(rows)
            assert [g.ids for g in dup.groups(rows)] == [
                tuple(sorted(r.id for r in rows if r.id != rows_c_id(rows)))
            ]


def rows_c_id(rows):
    return next(r.id for r in rows if r.cadastral_reference and r.url.endswith("/d"))


class TestNoQuadraticCrossProduct:
    def test_three_thousand_rows_that_only_share_municipality_and_price(self):
        import time

        rows = [foto(i, area=i) for i in range(1, 3001)]
        stats = {}
        started = time.perf_counter()
        found = dup.edges(rows, stats=stats)
        elapsed = time.perf_counter() - started
        assert found == []
        assert stats["evaluated"] == 0
        assert elapsed < 2.0

    def test_a_long_chain_of_joins_keeps_the_larger_component(self):
        """20,000 rows joined so that a growing component keeps meeting a
        two-member one: merging into the lower id copied ~10^8 members."""
        import time

        n = 20000
        union = dup._Union({i: foto(i, area=1) for i in range(1, n + 1)})
        pairs = [(i, n + 1 - i) for i in range(1, n // 2 + 1)]
        pairs += [(j, j + 1) for j in range(n // 2 + 1, n)]

        started = time.perf_counter()
        for left, right in pairs:
            assert union.join(left, right)
        elapsed = time.perf_counter() - started

        assert len(union.members[union.find(1)]) == n
        assert elapsed < 2.0

    def test_rows_agreeing_on_all_three_fields_are_the_only_pairs_evaluated(self):
        rows = [foto(i, area=i) for i in range(1, 301)] + [foto(9999, area=7)]
        stats = {}
        dup.edges(rows, stats=stats)
        assert stats["evaluated"] == 1

    def test_a_previous_price_indexes_the_row_under_both_values(self):
        a = foto(1, area=100, price=190000, previous_price=200000)
        b = foto(2, area=100, price=200000)
        stats = {}
        assert [e.tier for e in dup.edges([a, b], stats=stats)] == [dup.POSSIBLE]
        assert stats["evaluated"] == 1

    def test_groups_reuse_given_edges(self, monkeypatch):
        rows = [yae(1, title=YAE_TITLE, area=120), foto(2, title=FOTO_TITLE, area=120)]
        found = dup.edges(rows)
        monkeypatch.setattr(dup, "edges", lambda *a, **k: pytest.fail("recomputed"))
        assert len(dup.groups(rows, found_edges=found)) == 1


class TestRepresentativeAndFolding:
    def group_of(self, *rows):
        return dup.Group(tuple(sorted(rows, key=lambda r: r.id)), ())

    def test_favourite_wins_over_everything(self):
        fav = foto(1, is_favorite=True, listing_status="removed")
        other = ide(2, owner_verdict="interested", location_accuracy="precise")
        assert dup.representative(self.group_of(fav, other)) is fav

    def test_an_owner_decision_wins_next(self):
        decided = foto(1, owner_verdict="waiting", listing_status="removed")
        other = ide(2, location_accuracy="precise")
        assert dup.representative(self.group_of(decided, other)) is decided

    def test_live_status_beats_precision(self):
        live = foto(1)
        gone = ide(2, listing_status="sold", location_accuracy="precise")
        assert dup.representative(self.group_of(live, gone)) is live

    def test_precise_coordinates_beat_portal_order(self):
        precise = yae(1, location_accuracy="precise")
        other = ide(2)
        assert dup.representative(self.group_of(precise, other)) is precise

    def test_portal_order(self):
        rows = [
            row(url="https://www.milanuncios.com/x/a-1.htm"),
            yae(1),
            foto(2),
            ide(3),
        ]
        best = dup.representative(self.group_of(*rows))
        assert dup.source_of(best) == "idealista"
        assert dup.representative(self.group_of(*rows[:3])) is rows[2]
        assert dup.representative(self.group_of(rows[0], rows[1])) is rows[1]

    def test_lowest_id_is_the_last_tiebreak(self):
        a, b = foto(1), foto(2)
        assert dup.representative(self.group_of(b, a)) is min(
            (a, b), key=lambda r: r.id
        )

    def test_protected_rows_never_fold(self):
        keep = ide(1)
        favourite = yae(1, is_favorite=True)
        decided = yae(1, owner_verdict="rejected")
        acting = yae(1, next_action="call the agent")
        noted = yae(1)
        plain = yae(1)
        group = self.group_of(keep, favourite, decided, acting, noted, plain)
        # keep is not the representative if the favourite outranks it
        folded = dup.folded_ids([group], activity_ids={noted.id})
        assert folded == {keep.id, plain.id}
        assert dup.protection(noted, frozenset({noted.id})) == "timeline_entry"
        assert dup.protection(acting) == "outstanding_action"
        assert dup.protection(decided) == "owner_decision"
        assert dup.protection(favourite) == "favorite"

    def test_the_representative_is_never_folded(self):
        group = self.group_of(ide(1), yae(1))
        assert dup.representative(group).id not in dup.folded_ids([group])
        assert len(dup.folded_ids([group])) == 1


class TestSeedStaysInSync:
    def test_recommendation_duplicates_still_owns_the_five_pairs(self):
        from services import recommendation_duplicates as rd

        assert len(rd.CONFIRMED_ALIASES) == 5
        assert rd.CONFIRMED_ALIASES is rd._CONFIRMED_ALIASES


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _add(n, **kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", f"t:{n}"),
        search_profile_id=kw.pop("search_profile_id", None),
        **kw,
    )
    db.session.add(prop)
    db.session.flush()
    return prop


class TestRealRows:
    def test_real_property_rows_confirm_and_probable(self, app):
        a = _add(
            1,
            url="https://www.idealista.com/inmueble/93111111/",
            idealista_property_id=93111111,
            municipality="Bergondo",
            price=100000,
            area=150,
        )
        b = _add(
            2,
            url="https://www.yaencontre.com/venta/casas/inmueble-1-93111111",
            municipality="Bergondo",
            price=90000,
        )
        c = _add(
            3,
            url="https://www.yaencontre.com/venta/casas/inmueble-1-555",
            title=YAE_TITLE,
            municipality="Bergondo",
            price=250000,
            area=120,
        )
        d = _add(
            4,
            url="https://www.fotocasa.es/es/comprar/vivienda/x/777/d",
            title=FOTO_TITLE,
            municipality="Bergondo",
            price=250000,
            area=120,
        )
        found = dup.groups([a, b, c, d])
        assert sorted(g.ids for g in found) == [(a.id, b.id), (c.id, d.id)]
        assert {g.tier for g in found} == {dup.CONFIRMED, dup.PROBABLE}


class TestReportCli:
    def test_empty_subscription_prints_counts_and_exits_zero(self, app, capsys):
        assert report_duplicates.main(["--profile-id", "424242"], app=app) == 0
        out = capsys.readouterr().out
        assert "Rows read: 0" in out
        assert "confirmed: 0 groups" in out

    def test_report_counts_pairs_and_writes_nothing(self, app, capsys):
        a = _add(
            1,
            search_profile_id=None,
            url="https://www.idealista.com/inmueble/93222222/",
            idealista_property_id=93222222,
            is_favorite=True,
        )
        b = _add(2, url="https://www.yaencontre.com/venta/casas/inmueble-1-93222222")
        c = _add(3, url="https://www.yaencontre.com/venta/casas/inmueble-1-93333333")
        d = _add(4, url="https://www.idealista.com/inmueble/93333333/")
        db.session.add(PropertyActivity(property_id=d.id, kind="note", body="hi"))
        for prop in (a, b, c, d):
            prop.search_profile_id = 77
        db.session.commit()
        before = [
            (p.id, p.is_favorite, p.price, p.owner_verdict) for p in Property.query
        ]
        activity_before = PropertyActivity.query.count()

        assert (
            report_duplicates.main(
                ["--profile-id", "77", "--tier", "confirmed"], app=app
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "Rows read: 4" in out
        # a and d are protected and are also the representatives; b and c fold.
        assert "confirmed: 2 groups, 4 rows, 2 would fold" in out
        assert "protected (favorite): 1" in out
        assert "protected (timeline_entry): 1" in out
        assert "inmueble-1-93222222" in out

        db.session.expire_all()
        assert before == [
            (p.id, p.is_favorite, p.price, p.owner_verdict) for p in Property.query
        ]
        assert PropertyActivity.query.count() == activity_before

    def test_csv_format_has_a_header_and_evidence(self, app, capsys):
        a = _add(
            1,
            url="https://www.idealista.com/inmueble/93444444/",
            idealista_property_id=93444444,
            search_profile_id=5,
        )
        b = _add(
            2,
            url="https://www.yaencontre.com/venta/casas/inmueble-1-93444444",
            search_profile_id=5,
        )
        db.session.commit()
        assert (
            report_duplicates.main(
                ["--profile-id", "5", "--tier", "confirmed", "--format", "csv"], app=app
            )
            == 0
        )
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("tier,id_a,id_b")
        assert f"{a.id},{b.id}" in lines[1]
        assert "listing_id=met(93444444)" in lines[1]
