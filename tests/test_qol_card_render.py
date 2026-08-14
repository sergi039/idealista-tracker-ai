"""The QoL card renders every status honestly (proposal D21).

A refusal reads "Not measured", an unmatched municipality says so, distances
carry the straight-line label, and a property without the block gets no card
at all — never an empty shell that reads as "nothing nearby".
"""

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils.backfill_quality_of_life import needs_quality_of_life

QOL_OK = {
    "updated_at": "2026-08-14T00:00:00+00:00",
    "municipality": {
        "status": "ok",
        "ine_code": "33041",
        "name_matched": "Navia",
        "renta_media_persona": 14200,
        "renta_year": 2023,
        "renta_province_median": 12800,
        "population": 8400,
        "population_5y_change_pct": -1.1,
        "population_year": 2026,
    },
    "supermarkets": {
        "status": "ok",
        "distance_basis": "straight_line",
        "items": [
            {
                "name": "Alimerka",
                "brand": "Alimerka",
                "shop": "supermarket",
                "lat": 43.54,
                "lon": -6.71,
                "distance_km": 0.8,
            },
            {
                "name": None,
                "brand": None,
                "shop": "convenience",
                "lat": 43.52,
                "lon": -6.7,
                "distance_km": 3.1,
            },
        ],
    },
    "hospitals": {
        "status": "ok",
        "distance_basis": "straight_line",
        "source": "CNH 2025",
        "nearest": {
            "general_acute": {
                "name": "Hospital de Jarrio",
                "municipality": "Coaña",
                "beds": 110,
                "teaching": False,
                "high_tech_count": 1,
                "lat": 43.53,
                "lon": -6.73,
                "distance_km": 9.1,
            },
            "teaching_high_tech": {
                "name": "HUCA",
                "municipality": "Oviedo",
                "beds": 1039,
                "teaching": True,
                "high_tech_count": 8,
                "lat": 43.36,
                "lon": -5.85,
                "distance_km": 72.4,
            },
        },
    },
}

QOL_DEGRADED = {
    "updated_at": "2026-08-14T00:00:00+00:00",
    "municipality": {"status": "not_matched", "queried": "Ovi..."},
    "supermarkets": {"status": "unavailable", "reason": "overpass_query_error"},
    "hospitals": {"status": "no_reference_data"},
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


def _add(qol):
    prop = Property(
        source_email_id=f"qol-render-{id(qol)}",
        title="QolRenderFixture",
        municipality="Navia",
        location_lat=43.54,
        location_lon=-6.72,
        enrichment={"quality_of_life": qol} if qol else None,
    )
    db.session.add(prop)
    db.session.commit()
    return prop.id


class TestCardStates:
    def test_measured_block_renders_the_three_parts(self, app, client):
        pid = _add(QOL_OK)
        body = client.get(f"/properties/{pid}").get_data(as_text=True)
        assert "Quality of Life" in body
        assert "14,200" in body  # renta
        assert ">111<" in body  # 14200/12800 index vs province median
        assert "Alimerka" in body and "0.8 km" in body
        assert "Unnamed shop" in body, "an unnamed village shop is still a shop"
        assert "Hospital de Jarrio" in body and "HUCA" in body
        assert "straight-line" in body, "distances must say what they are"
        assert "not an official tier" in body

    def test_degraded_block_is_honest_per_part(self, app, client):
        pid = _add(QOL_DEGRADED)
        body = client.get(f"/properties/{pid}").get_data(as_text=True)
        assert "Municipality not matched" in body
        assert "Not measured" in body  # the Overpass refusal
        assert "Reference data not imported yet" in body
        assert "Alimerka" not in body

    def test_no_block_no_card(self, app, client):
        pid = _add(None)
        body = client.get(f"/properties/{pid}").get_data(as_text=True)
        assert "Quality of Life" not in body

    def test_every_reachable_status_renders_something(self, app, client):
        """No status may render a bare section header (diff review,
        2026-08-14): an empty shell under "Supermarkets" reads as nothing
        nearby, which is the #98 mistake in card form."""
        pid = _add(
            {
                "updated_at": "2026-08-14T00:00:00+00:00",
                "municipality": {"status": "no_municipality"},
                "supermarkets": {"status": "no_coordinates"},
                "hospitals": {"status": "no_coordinates"},
            }
        )
        body = client.get(f"/properties/{pid}").get_data(as_text=True)
        assert "No municipality recorded" in body
        assert body.count("No coordinates — not measured") == 2

    def test_unknown_teaching_never_reads_as_no(self, app, client):
        """Two CNH rows carry teaching=null; the tooltip renders the unknown
        as '—', never a definite 'no' (the sea-view rule), and in English."""
        qol = {
            "updated_at": "2026-08-14T00:00:00+00:00",
            "municipality": {"status": "not_matched", "queried": "X"},
            "supermarkets": {"status": "osm_empty", "items": []},
            "hospitals": {
                "status": "ok",
                "distance_basis": "straight_line",
                "source": "CNH 2025 (fixture)",
                "nearest": {
                    "general_acute": {
                        "name": "Hospital Ribera Juan Cardona",
                        "municipality": "Ferrol",
                        "beds": 190,
                        "teaching": None,
                        "high_tech_count": 0,
                        "lat": 43.48,
                        "lon": -8.23,
                        "distance_km": 12.0,
                    }
                },
            },
        }
        pid = _add(qol)
        body = client.get(f"/properties/{pid}").get_data(as_text=True)
        assert "190 / — / 0" in body
        assert "sí" not in body
        # The vintage comes from the payload's source field, not a pinned
        # translation string.
        assert "CNH 2025 (fixture)" in body


class TestBackfillNeeds:
    def test_states(self, app):
        assert needs_quality_of_life(Property(enrichment=None)) is True
        assert needs_quality_of_life(Property(enrichment={})) is True
        # Every part refused -> retryable.
        assert (
            needs_quality_of_life(
                Property(
                    enrichment={
                        "quality_of_life": {
                            "municipality": {"status": "no_reference_data"},
                            "supermarkets": {"status": "unavailable"},
                            "hospitals": {"status": "unavailable"},
                        }
                    }
                )
            )
            is True
        )
        # One measured answer -> done; reruns are not a weekly re-query.
        assert (
            needs_quality_of_life(Property(enrichment={"quality_of_life": QOL_OK}))
            is False
        )
        assert (
            needs_quality_of_life(
                Property(enrichment={"quality_of_life": QOL_DEGRADED})
            )
            is True
        )
