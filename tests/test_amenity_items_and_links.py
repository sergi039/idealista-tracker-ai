"""Named amenities with both links, and both links on every place card.

The owner's ask of 2026-08-24: the Extended Infrastructure card said
"Restaurants: 2 nearby" with no way to ask which two, and the place rows on
the other cards carried one link each -- a pin or a route, never both. The
Overpass amenity query has always fetched full elements (`out center`), so
the names and the points were in the response and thrown away at aggregation.

Pinned here:

* `_fetch_osm_amenities` keeps the named items beside the counts: a label
  with brand/operator fallback (two of the three fuel stations at property
  360's own coordinate are nameless ways), the node's point or the
  way/relation centre, the OSM ref, and straight-line metres from the
  queried point -- nearest first, capped at OSM_AMENITY_ITEMS_MAX;
* an element with no readable point still counts and yields no item -- the
  count claims presence, the item claims *where*;
* the week-long cache entry carries the items (key bumped to v3), a cache
  hit returns them, and a v2-shaped entry without items is a miss rather
  than an answer missing half its shape;
* `enrich_osm_amenities` stores them under `osm_amenity_items` beside the
  counts, and a refusal leaves the previous items exactly as it leaves the
  previous counts (#98/#144);
* the backfill's `--only-missing` re-queries a row whose last run predates
  the items and skips a row that carries them;
* the page renders each item with BOTH links -- the name pins the place
  (maps/search), the icon routes to it by car (maps/dir with the property
  as origin) -- and the new key never falls through to the generic branch
  as a raw dict;
* the other place cards -- travel hubs, beaches, QoL supermarkets, QoL
  hospitals, pool -- each carry both links per row (owner: "ссылки обе").

The page assertions check status 200 *and* content by value, because
`routes/main_routes.py` turns a template error into a redirect and a page
that did not render also does not contain the defect being looked for.
"""

from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from models import Property
from services.enrichment_service import (
    OSM_AMENITY_ITEMS_KEY,
    OSM_AMENITY_ITEMS_MAX,
    OSM_CACHE_KEY,
    OSM_STATE_OK,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
    EnrichmentService,
)
from tests import setup_test_environment
from utils.backfill_osm_amenities import backfill
from utils.cache import cache
from utils.maps_urls import maps_directions_url, maps_place_url

ORIGIN = (43.0, -6.0)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        cache.clear()
        db.create_all()
        yield app
        db.drop_all()
        cache.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def service():
    return EnrichmentService()


def _response(status_code=200, payload=None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


def _listing(key, lat=ORIGIN[0], lon=ORIGIN[1], enrichment=None, travel=None):
    prop = Property(
        source_email_id=f"amenity-items-{key}",
        title=f"AmenityItemsFixture {key}",
        municipality="Villaviciosa",
        location_lat=lat,
        location_lon=lon,
        location_accuracy="precise",
    )
    if enrichment is not None:
        prop.enrichment = enrichment
    if travel is not None:
        prop.travel = travel
    db.session.add(prop)
    db.session.commit()
    return prop


# Around ORIGIN: 0.001 deg of latitude is ~111 m, which keeps the expected
# ordering readable without a second haversine in the test.
ELEMENTS = [
    # Farther restaurant first, so the sort is proven rather than inherited
    # from response order.
    {
        "type": "node",
        "id": 2,
        "lat": 43.003,
        "lon": -6.0,
        "tags": {"amenity": "restaurant"},
    },
    {
        "type": "node",
        "id": 1,
        "lat": 43.001,
        "lon": -6.0,
        "tags": {"amenity": "restaurant", "name": "Rego"},
    },
    # A way has no lat/lon of its own; `out center` supplies the centroid.
    # No name -- the label falls back to the brand.
    {
        "type": "way",
        "id": 3,
        "center": {"lat": 43.002, "lon": -6.0},
        "tags": {"amenity": "fuel", "brand": "Repsol"},
    },
    # Nameless, brandless, operatorless: a real shape (two of the three fuel
    # stations at property 360's coordinate) -- the item survives with no name.
    {
        "type": "way",
        "id": 4,
        "center": {"lat": 43.004, "lon": -6.0},
        "tags": {"amenity": "fuel", "building": "yes"},
    },
    # No readable point at all: counts, but cannot be linked.
    {"type": "node", "id": 5, "tags": {"amenity": "restaurant"}},
]


class TestItemsExtraction:
    @patch("services.enrichment_service.request_with_retries")
    def test_items_carry_name_point_ref_and_distance(self, mock_request, app, service):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": ELEMENTS})

            reading = service._fetch_osm_amenities(*ORIGIN)

            assert reading.failure is None
            # The pointless element still counts -- presence and place are
            # separate claims.
            assert reading.counts == {"restaurant": 3, "fuel": 2}

            restaurants = reading.items["restaurant"]
            assert [item["name"] for item in restaurants] == ["Rego", None]
            assert restaurants[0] == {
                "name": "Rego",
                "lat": 43.001,
                "lon": -6.0,
                "osm_type": "node",
                "osm_id": 1,
                "distance_m": restaurants[0]["distance_m"],
            }
            assert 100 <= restaurants[0]["distance_m"] <= 125
            assert restaurants[0]["distance_m"] < restaurants[1]["distance_m"]

            fuels = reading.items["fuel"]
            assert [item["name"] for item in fuels] == ["Repsol", None]
            assert fuels[0]["lat"] == 43.002 and fuels[0]["osm_type"] == "way"

    @patch("services.enrichment_service.request_with_retries")
    def test_items_are_capped_nearest_first(self, mock_request, app, service):
        with app.app_context():
            elements = [
                {
                    "type": "node",
                    "id": index,
                    "lat": 43.0 + index * 0.001,
                    "lon": -6.0,
                    "tags": {"amenity": "restaurant", "name": f"R{index}"},
                }
                # Reverse order, so a cap applied before the sort would keep
                # the farthest instead of the nearest.
                for index in range(12, 0, -1)
            ]
            mock_request.return_value = _response(payload={"elements": elements})

            reading = service._fetch_osm_amenities(*ORIGIN)

            assert reading.counts == {"restaurant": 12}
            items = reading.items["restaurant"]
            assert len(items) == OSM_AMENITY_ITEMS_MAX
            assert [item["name"] for item in items] == [
                f"R{index}" for index in range(1, OSM_AMENITY_ITEMS_MAX + 1)
            ]


class TestCacheCarriesItems:
    @patch("services.enrichment_service.request_with_retries")
    def test_a_second_lookup_answers_items_from_the_cache(
        self, mock_request, app, service
    ):
        with app.app_context():
            assert OSM_CACHE_KEY == "osm_amenities_v3", (
                "the items changed the cached shape; the key must say so"
            )
            mock_request.return_value = _response(payload={"elements": ELEMENTS})

            first = service._fetch_osm_amenities(*ORIGIN)
            second = service._fetch_osm_amenities(*ORIGIN)

            assert mock_request.call_count == 1, "the second call must be a cache hit"
            assert second.counts == first.counts
            assert second.items == first.items
            assert second.items["restaurant"][0]["name"] == "Rego"

    @patch("services.enrichment_service.request_with_retries")
    def test_an_entry_without_items_is_a_miss_not_half_an_answer(
        self, mock_request, app, service
    ):
        with app.app_context():
            from utils.cache import cache_enrichment_data

            # A v2-shaped entry: counts alone. The versioned key makes this
            # unreachable in production; the shape guard is what answers a
            # hand-edited or partially-written entry.
            cache_enrichment_data(
                *ORIGIN,
                OSM_CACHE_KEY,
                {"counts": {"restaurant": 2}, "measured_at": "2026-08-01T00:00:00Z"},
            )
            mock_request.return_value = _response(payload={"elements": ELEMENTS})

            reading = service._fetch_osm_amenities(*ORIGIN)

            assert mock_request.call_count == 1, "counts without items must re-fetch"
            assert isinstance(reading.items, dict)
            assert reading.items["restaurant"][0]["name"] == "Rego"


class TestStoredBesideTheCounts:
    @patch("services.enrichment_service.request_with_retries")
    def test_items_survive_a_commit_and_reload(self, mock_request, app, service):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": ELEMENTS})
            prop = _listing("store")

            failure = service.enrich_osm_amenities(prop)

            assert failure is None
            db.session.expire_all()
            stored = db.session.get(Property, prop.id)
            infrastructure = stored.infrastructure_extended or {}
            assert infrastructure["osm_amenities"] == {"restaurant": 3, "fuel": 2}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
            items = infrastructure[OSM_AMENITY_ITEMS_KEY]
            assert [item["name"] for item in items["restaurant"]] == ["Rego", None]
            assert items["fuel"][0]["name"] == "Repsol"

    @patch("services.enrichment_service.request_with_retries")
    def test_a_refusal_keeps_the_previous_items(self, mock_request, app, service):
        with app.app_context():
            previous_items = {
                "restaurant": [
                    {
                        "name": "Rego",
                        "lat": 43.001,
                        "lon": -6.0,
                        "osm_type": "node",
                        "osm_id": 1,
                        "distance_m": 111,
                    }
                ]
            }
            prop = _listing(
                "refusal",
                enrichment={
                    "infrastructure_extended": {
                        "osm_amenities": {"restaurant": 1},
                        OSM_AMENITY_ITEMS_KEY: previous_items,
                        OSM_STATUS_KEY: {
                            "state": OSM_STATE_OK,
                            "checked_at": "2026-08-01T00:00:00+00:00",
                            "measured_at": "2026-08-01T00:00:00+00:00",
                        },
                    }
                },
            )
            mock_request.return_value = _response(status_code=406)

            failure = service.enrich_osm_amenities(prop)

            assert failure is not None
            db.session.expire_all()
            stored = db.session.get(Property, prop.id)
            infrastructure = stored.infrastructure_extended or {}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE
            # The refusal must not eat the answer somebody has -- counts or
            # items alike.
            assert infrastructure["osm_amenities"] == {"restaurant": 1}
            assert infrastructure[OSM_AMENITY_ITEMS_KEY] == previous_items


class TestBackfillOnlyMissing:
    def _service_stub(self):
        stub = Mock()
        stub.enrich_osm_amenities.return_value = None
        return stub

    def test_counts_without_items_are_missing(self):
        prop = Property(
            enrichment={
                "infrastructure_extended": {
                    "osm_amenities": {"restaurant": 2},
                    OSM_STATUS_KEY: {"state": OSM_STATE_OK},
                }
            }
        )
        stub = self._service_stub()

        outcome = backfill([prop], stub, only_missing=True)

        assert outcome["measured"] == 1
        stub.enrich_osm_amenities.assert_called_once()

    def test_a_row_carrying_items_is_skipped(self):
        prop = Property(
            enrichment={
                "infrastructure_extended": {
                    "osm_amenities": {"restaurant": 2},
                    OSM_AMENITY_ITEMS_KEY: {"restaurant": []},
                    OSM_STATUS_KEY: {"state": OSM_STATE_OK},
                }
            }
        )
        stub = self._service_stub()

        outcome = backfill([prop], stub, only_missing=True)

        assert outcome["skipped"] == 1
        stub.enrich_osm_amenities.assert_not_called()


def _escaped(url):
    assert url is not None
    return url.replace("&", "&amp;")


class TestAmenityCardLinks:
    def _page(self, client, prop):
        response = client.get(f"/properties/{prop.id}")
        # A template error becomes a redirect; a 200 is part of the assertion.
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def test_each_item_renders_with_pin_and_route(self, app, client):
        with app.app_context():
            prop = _listing(
                "card",
                lat=43.541544,
                lon=-6.864162,
                enrichment={
                    "infrastructure_extended": {
                        "osm_amenities": {"restaurant": 2, "fuel": 1},
                        OSM_AMENITY_ITEMS_KEY: {
                            "restaurant": [
                                {
                                    "name": "Mesón El Fornello",
                                    "lat": 43.5605947,
                                    "lon": -6.8727094,
                                    "osm_type": "node",
                                    "osm_id": 4143056891,
                                    "distance_m": 2200,
                                },
                                {
                                    "name": None,
                                    "lat": 43.5587131,
                                    "lon": -6.8618675,
                                    "osm_type": "node",
                                    "osm_id": 99,
                                    "distance_m": 1900,
                                },
                            ],
                            "fuel": [
                                {
                                    "name": "Repsol",
                                    "lat": 43.5553984,
                                    "lon": -6.8453367,
                                    "osm_type": "node",
                                    "osm_id": 287012034,
                                    "distance_m": 2100,
                                }
                            ],
                        },
                        OSM_STATUS_KEY: {
                            "state": OSM_STATE_OK,
                            "checked_at": "2026-08-24T00:00:00+00:00",
                            "measured_at": "2026-08-24T00:00:00+00:00",
                        },
                    }
                },
            )
            body = self._page(client, prop)

            assert "Mesón El Fornello" in body and "Repsol" in body
            # One literal, so a broken builder cannot vouch for itself; the
            # rest through the same builders test_maps_urls.py pins by value.
            assert (
                "https://www.google.com/maps/search/?api=1&amp;"
                "query=43.560595%2C-6.872709" in body
            )
            assert (
                _escaped(
                    maps_directions_url(43.541544, -6.864162, 43.5605947, -6.8727094)
                )
                in body
            )
            assert _escaped(maps_place_url(43.5553984, -6.8453367)) in body
            assert (
                _escaped(
                    maps_directions_url(43.541544, -6.864162, 43.5553984, -6.8453367)
                )
                in body
            )
            # The unnamed second restaurant still renders, as a generic row.
            assert "Unnamed" in body
            # The sibling key must not fall through to the generic renderer,
            # whose label would be the title-cased key. The raw key itself is
            # a legal sight: the page embeds the enrichment JSON for its own
            # script.
            assert "Osm Amenity Items" not in body

    def test_a_row_without_items_renders_counts_alone(self, app, client):
        with app.app_context():
            prop = _listing(
                "counts-only",
                enrichment={
                    "infrastructure_extended": {
                        "osm_amenities": {"restaurant": 2},
                        OSM_STATUS_KEY: {
                            "state": OSM_STATE_OK,
                            "checked_at": "2026-08-24T00:00:00+00:00",
                            "measured_at": "2026-08-24T00:00:00+00:00",
                        },
                    }
                },
            )
            body = self._page(client, prop)
            assert "2 nearby" in body


class TestBothLinksOnPlaceCards:
    """Every place row carries the pin and the route (owner ask, 2026-08-24)."""

    def _page(self, client, prop):
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def test_beach_rows_carry_both(self, app, client):
        with app.app_context():
            prop = _listing(
                "beach",
                lat=43.53,
                lon=-5.39,
                travel={
                    "targets": {},
                    "beaches": {
                        "status": "ok",
                        "max_drive_min": 20,
                        "items": [
                            {
                                "name": "Playa de Rodiles",
                                "place_id": "PLACE_RODILES",
                                "lat": 43.531,
                                "lon": -5.383,
                                "duration_min": 7,
                                "distance_km": 5.2,
                            }
                        ],
                    },
                },
            )
            body = self._page(client, prop)
            assert "Playa de Rodiles" in body
            assert _escaped(maps_place_url(43.531, -5.383, "PLACE_RODILES")) in body
            assert (
                _escaped(
                    maps_directions_url(43.53, -5.39, 43.531, -5.383, "PLACE_RODILES")
                )
                in body
            )

    def test_travel_hub_rows_carry_both(self, app, client):
        with app.app_context():
            prop = _listing(
                "hub",
                lat=43.53,
                lon=-5.39,
                travel={
                    "targets": {
                        "supermarket": {
                            "kind": "preset",
                            "status": "ok",
                            "duration_min": 11,
                            "distance_km": 6.9,
                            "place": {
                                "name": "Comercial Alonso",
                                "lat": 43.55,
                                "lon": -5.41,
                            },
                        }
                    }
                },
            )
            body = self._page(client, prop)
            assert "Comercial Alonso" in body
            assert _escaped(maps_place_url(43.55, -5.41)) in body
            assert _escaped(maps_directions_url(43.53, -5.39, 43.55, -5.41)) in body

    def test_qol_shop_and_hospital_rows_carry_both(self, app, client):
        with app.app_context():
            prop = _listing(
                "qol",
                lat=43.54,
                lon=-6.72,
                enrichment={
                    "quality_of_life": {
                        "updated_at": "2026-08-24T00:00:00+00:00",
                        "municipality": {"status": "unavailable"},
                        "supermarkets": {
                            "status": "ok",
                            "distance_basis": "straight_line",
                            "items": [
                                {
                                    "name": "Alimerka",
                                    "shop": "supermarket",
                                    "lat": 43.545,
                                    "lon": -6.71,
                                    "distance_km": 0.8,
                                }
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
                                }
                            },
                        },
                    }
                },
            )
            body = self._page(client, prop)
            assert "Alimerka" in body
            assert _escaped(maps_place_url(43.545, -6.71)) in body
            assert _escaped(maps_directions_url(43.54, -6.72, 43.545, -6.71)) in body
            assert "Hospital de Jarrio" in body
            assert _escaped(maps_place_url(43.53, -6.73)) in body
            assert _escaped(maps_directions_url(43.54, -6.72, 43.53, -6.73)) in body

    def test_pool_rows_carry_both(self, app, client):
        with app.app_context():
            prop = _listing(
                "pool",
                lat=43.54,
                lon=-6.72,
                enrichment={
                    "pool": {
                        "status": "ok",
                        "candidates": [
                            {
                                "name": "Piscina Marina",
                                "lat": 43.55,
                                "lon": -6.70,
                                "straight_km": 1.9,
                            }
                        ],
                    }
                },
            )
            body = self._page(client, prop)
            assert "Piscina Marina" in body
            assert _escaped(maps_place_url(43.55, -6.70)) in body
            assert _escaped(maps_directions_url(43.54, -6.72, 43.55, -6.70)) in body
