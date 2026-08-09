"""Regression tests: a refused Overpass call is not "no amenities nearby".

`EnrichmentService._enrich_with_osm_data()` posted to overpass-api.de without a
User-Agent, so `requests` sent its default `python-requests/x.y.z`. Measured
against the live instance on 2026-08-09, that is answered with `406 Not
Acceptable` -- and so is any User-Agent carrying a parenthetical comment. Only
a bare product token is served. Every OSM enrichment call had therefore been
failing, and because the only check was `if response.status_code == 200`, the
refusal was silently skipped and the property kept an absent amenity list that
read exactly like "Overpass looked and found nothing".

That is the #98 defect in a second place: a refused API written out as a
computed negative.

The behaviour pinned down here:

* the request carries `User-Agent: IdealistaRank/1.0`, a bare product token;
* a non-200 answer is stored as `osm_amenities_status.state == "unavailable"`
  with a reason code, and never as an empty `osm_amenities`;
* a refusal is never written to the enrichment cache;
* an answered run with nothing nearby is still a success -- empty amenities
  with `state == "ok"`;
* the 504 that Overpass returns while both of its two per-IP slots are busy is
  retried with a backoff measured in tens of seconds, not the half-second
  default.
"""

import logging
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests

from app import create_app, db
from models import Land
from services.enrichment_service import (
    OSM_STATE_OK,
    OSM_STATE_UNAVAILABLE,
    OSM_STATUS_KEY,
    EnrichmentService,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import REASON_HTTP_ERROR, REASON_NETWORK_ERROR
from utils.http import HTTP_USER_AGENT


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
def service():
    return EnrichmentService()


@pytest.fixture
def land(app):
    with app.app_context():
        land = Land(
            source_email_id="test_overpass_ua_1",
            title="Test Land for Overpass",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("39.4699"),
            location_lon=Decimal("-0.3763"),
        )
        db.session.add(land)
        db.session.commit()
        return land.id


def _response(status_code=200, payload=None, raises=None):
    response = Mock()
    response.status_code = status_code
    if raises is not None:
        response.json.side_effect = raises
    else:
        response.json.return_value = payload if payload is not None else {}
    return response


class TestOutgoingRequest:
    """The header that decides whether overpass-api.de answers at all."""

    @patch("services.enrichment_service.request_with_retries")
    def test_sends_the_bare_product_token_user_agent(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})
            service._enrich_with_osm_data(db.session.get(Land, land))

        headers = mock_request.call_args.kwargs["headers"]
        assert headers["User-Agent"] == HTTP_USER_AGENT

    def test_user_agent_is_a_bare_token(self):
        """A parenthetical comment is refused just like the default UA."""
        assert "(" not in HTTP_USER_AGENT and ")" not in HTTP_USER_AGENT
        assert not HTTP_USER_AGENT.lower().startswith("python-requests")
        assert HTTP_USER_AGENT == "IdealistaRank/1.0"

    @patch("services.enrichment_service.request_with_retries")
    def test_retry_backoff_out_waits_a_busy_query_slot(
        self, mock_request, app, service, land
    ):
        """Overpass answers 504 while both per-IP slots are busy.

        A slot frees up in roughly a minute, so the half-second default backoff
        gives up long before the server would have answered.
        """
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})
            service._enrich_with_osm_data(db.session.get(Land, land))

        kwargs = mock_request.call_args.kwargs
        assert kwargs["backoff_base"] >= 8.0
        assert kwargs["max_attempts"] >= 3
        # Total wait across retries has to be tens of seconds, not sub-second.
        waits = sum(
            kwargs["backoff_base"] * (2**attempt)
            for attempt in range(kwargs["max_attempts"] - 1)
        )
        assert waits >= 30.0


class TestRefusalIsNotAnEmptyResult:
    """The #98 line: refused and empty must not look the same."""

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_406_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land, caplog
    ):
        with app.app_context():
            mock_request.return_value = _response(status_code=406)

            row = db.session.get(Land, land)
            with caplog.at_level(logging.ERROR):
                failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == REASON_HTTP_ERROR
            assert failure.http_status == 406

            infrastructure = row.infrastructure_extended or {}
            # The whole point: no amenity list is invented for a refusal.
            assert "osm_amenities" not in infrastructure
            status = infrastructure[OSM_STATUS_KEY]
            assert status["state"] == OSM_STATE_UNAVAILABLE
            assert status["reason"] == REASON_HTTP_ERROR
            assert status["http_status"] == 406

        # A refusal is loud, not swallowed.
        assert any("OSM amenities unavailable" in r.message for r in caplog.records)
        # And it is never cached, or the next run would reuse the refusal.
        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_504_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(status_code=504)

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.http_status == 504
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_network_error_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        with app.app_context():
            mock_request.side_effect = requests.ConnectionError("no route to host")

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            assert failure.reason == REASON_NETWORK_ERROR
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()

    @patch("services.enrichment_service.cache_enrichment_data")
    @patch("services.enrichment_service.request_with_retries")
    def test_non_json_body_is_recorded_as_unavailable(
        self, mock_request, mock_cache, app, service, land
    ):
        """A 200 carrying an HTML error page is still not an answer."""
        with app.app_context():
            mock_request.return_value = _response(raises=ValueError("not json"))

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is not None
            infrastructure = row.infrastructure_extended or {}
            assert "osm_amenities" not in infrastructure
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_UNAVAILABLE

        mock_cache.assert_not_called()


class TestAnsweredRuns:
    """An answer stays an answer, including an empty one."""

    @patch("services.enrichment_service.request_with_retries")
    def test_nothing_nearby_is_a_success_not_a_refusal(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(payload={"elements": []})

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            infrastructure = row.infrastructure_extended or {}
            # Empty *and* answered - distinguishable from the refusal above
            # only because of the status marker.
            assert infrastructure["osm_amenities"] == {}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
            assert "reason" not in infrastructure[OSM_STATUS_KEY]

    @patch("services.enrichment_service.request_with_retries")
    def test_amenities_are_counted_and_marked_ok(
        self, mock_request, app, service, land
    ):
        with app.app_context():
            mock_request.return_value = _response(
                payload={
                    "elements": [
                        {"tags": {"amenity": "supermarket"}},
                        {"tags": {"amenity": "supermarket"}},
                        {"tags": {"amenity": "school"}},
                    ]
                }
            )

            row = db.session.get(Land, land)
            failure = service._enrich_with_osm_data(row)

            assert failure is None
            infrastructure = row.infrastructure_extended or {}
            assert infrastructure["osm_amenities"] == {"supermarket": 2, "school": 1}
            assert infrastructure[OSM_STATUS_KEY]["state"] == OSM_STATE_OK
