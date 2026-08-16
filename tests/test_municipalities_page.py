"""The municipality comparison page (proposal D22, owner spec 2026-08-14).

The contracts:
* municipality FACTS (INE/SEPE) and LISTING MEDIANS are separate claims and
  the page says which is which;
* a median is a median — never a minimum, which would crown a municipality
  for one lucky listing — and always carries its coverage;
* an unmatched municipality shows its medians and an explicit INE "—";
* unmeasured rows sort last in both directions;
* a hospital beyond the catalogue's five provinces is reported as outside
  coverage, not as a 700 km "nearest hospital".
"""

import json

import pytest

from app import create_app, db
from models import Property
from services import quality_of_life_service as qol_module
from services.municipality_comparison_service import (
    MunicipalityComparisonService,
)
from services.quality_of_life_service import QualityOfLifeService
from tests import setup_test_environment

INE_FIXTURE = {
    "source": {"renta": "INE ADRH (fixture)"},
    "municipalities": {
        "33041": {
            "name": "Navia",
            "province": "33",
            "renta_media_persona": 15629,
            "renta_year": 2023,
            "population": 8031,
            "population_5y_change_pct": -3.5,
            "population_year": 2025,
        }
    },
    "province_medians": {"33": {"renta_media_persona": 14811}},
}
CNH_FIXTURE = {
    "source": "CNH 2025 (fixture)",
    "hospitals": [
        {
            "name": "Hospital de Jarrio",
            "grouping": "general_acute",
            "beds": 116,
            "teaching": True,
            "high_tech_count": 1,
            "lat": 43.544,
            "lon": -6.747,
        }
    ],
}
SEPE_FIXTURE = {
    "source": "SEPE paro registrado (fixture)",
    "period": "2026-07",
    "municipalities": {"33041": {"name": "Navia", "unemployed_total": 402}},
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def reference_files(tmp_path, monkeypatch):
    for name, payload, attr in (
        ("ine_municipal.json", INE_FIXTURE, "INE_DATA_PATH"),
        ("hospitals_cnh.json", CNH_FIXTURE, "CNH_DATA_PATH"),
        ("sepe_unemployment.json", SEPE_FIXTURE, "SEPE_DATA_PATH"),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(qol_module, attr, str(path))
    return tmp_path


def _listing(municipality, **overrides):
    fields = dict(
        source_email_id=f"muni-{municipality}-{overrides.get('title', '')}",
        title=overrides.pop("title", f"L-{municipality}"),
        municipality=municipality,
        listing_status="active",
        location_lat=43.54,
        location_lon=-6.72,
        # A median of sea distances only counts rows whose coordinate is the
        # parcel: a distance measured from a locality centroid would rank the
        # municipality by where its town sits, not by its listings.
        location_accuracy="precise",
        property_category="housing",
    )
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def _with_sea(km):
    return {"sea": {"status": "ok", "distance_m": km * 1000}}


class TestMedianNotMinimum:
    def test_the_page_reports_the_median_of_the_listings(self, app, reference_files):
        for km, title in ((1.0, "a"), (5.0, "b"), (30.0, "c")):
            _listing("Navia", title=title, enrichment=_with_sea(km))
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        navia = next(r for r in rows if r["name"] == "Navia")
        assert navia["sea_km"]["median"] == 5.0, "median, never the lucky minimum"
        assert navia["sea_km"]["measured"] == 3
        assert navia["listings"] == 3

    def test_coverage_counts_the_unmeasured_listings(self, app, reference_files):
        _listing("Navia", title="a", enrichment=_with_sea(2.0))
        _listing("Navia", title="b")  # nothing measured
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        navia = next(r for r in rows if r["name"] == "Navia")
        assert navia["sea_km"] == {"median": 2.0, "measured": 1, "total": 2}

    def test_nothing_measured_is_none_not_zero(self, app, reference_files):
        _listing("Navia", title="a")
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        assert rows[0]["sea_km"]["median"] is None


class TestMunicipalityFacts:
    def test_ine_and_sepe_facts_are_joined(self, app, reference_files):
        _listing("Navia", title="a")
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        navia = rows[0]
        assert navia["ine"]["renta_media_persona"] == 15629
        assert navia["ine"]["renta_index"] == 106  # 15629 / 14811
        assert navia["unemployment"]["unemployed_total"] == 402
        assert navia["unemployment"]["proxy_pct"] == 5.0  # 402 / 8031
        assert navia["unemployment"]["period"] == "2026-07"

    def test_an_unmatched_municipality_keeps_its_medians(self, app, reference_files):
        _listing("Rojales", title="a", enrichment=_with_sea(3.0))
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        row = rows[0]
        assert row["ine"] is None, "never a guessed INE code"
        assert row["unemployment"] is None
        assert row["sea_km"]["median"] == 3.0

    def test_missing_sepe_file_reads_as_missing(self, app, tmp_path, monkeypatch):
        path = tmp_path / "ine.json"
        path.write_text(json.dumps(INE_FIXTURE), encoding="utf-8")
        monkeypatch.setattr(qol_module, "INE_DATA_PATH", str(path))
        monkeypatch.setattr(qol_module, "SEPE_DATA_PATH", str(tmp_path / "absent.json"))
        _listing("Navia", title="a")
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        assert rows[0]["unemployment"] is None


class TestHospitalCoverageBound:
    def test_a_hospital_beyond_the_catalogue_is_outside_coverage(
        self, app, reference_files
    ):
        """An Alicante listing measured 699.9 km to a Galician hospital —
        a true distance to the wrong catalogue (seen 2026-08-14)."""
        service = QualityOfLifeService()
        part = service.hospitals(38.06, -0.72)  # Rojales, Alicante
        assert part["status"] == "outside_reference_coverage"
        assert part["nearest_km"] > 150

    def test_inside_coverage_still_reports_the_nearest(self, app, reference_files):
        part = QualityOfLifeService().hospitals(43.55, -6.83)
        assert part["status"] == "ok"
        assert part["nearest"]["general_acute"]["name"] == "Hospital de Jarrio"


class TestSorting:
    def _rows(self):
        service = MunicipalityComparisonService()
        return service, service.build_rows(Property.query.all())

    def test_unmeasured_sorts_last_in_both_directions(self, app, reference_files):
        _listing("Navia", title="a", enrichment=_with_sea(2.0))
        _listing("Coaña", title="b")  # no measurement at all
        service, rows = self._rows()
        for order in (True, False):
            ordered = service.sort_rows(rows, "sea_km", descending=order)
            assert ordered[-1]["name"] == "Coaña", (
                "a municipality nobody measured must never win a distance sort"
            )

    def test_an_unknown_sort_falls_back_instead_of_raising(self, app, reference_files):
        _listing("Navia", title="a")
        service, rows = self._rows()
        assert service.sort_rows(rows, "not_a_column", descending=True)


class TestPageRender:
    def test_the_page_separates_facts_from_medians(self, app, client, reference_files):
        _listing("Navia", title="a", price=100000, area=200, enrichment=_with_sea(1.2))
        body = client.get("/municipalities").get_data(as_text=True)
        assert "Municipality facts" in body
        assert "Listing medians" in body
        assert "Navia" in body
        assert "15,629" in body  # the INE renta
        assert "not the official unemployment rate" in body

    def test_the_nav_carries_its_own_entry(self, app, client, reference_files):
        body = client.get("/municipalities").get_data(as_text=True)
        nav = body.split("</nav>", 1)[0]
        assert 'href="/municipalities"' in nav

    def test_each_municipality_links_into_the_filtered_list(
        self, app, client, reference_files
    ):
        _listing("Navia", title="a")
        body = client.get("/municipalities").get_data(as_text=True)
        assert "municipality=Navia" in body

    def test_archived_listings_are_excluded_by_default(
        self, app, client, reference_files
    ):
        _listing("Navia", title="live")
        _listing("Coaña", title="gone", listing_status="removed")
        body = client.get("/municipalities").get_data(as_text=True)
        assert "Navia" in body
        assert "Coaña" not in body
        with_archived = client.get("/municipalities?archived=on").get_data(as_text=True)
        assert "Coaña" in with_archived
