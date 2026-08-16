"""A cache that cannot answer is a miss, never an error (#356).

`utils/cache.py` fell back to `SimpleCache` whenever `REDIS_URL` was unset,
which was always — so the coastline cells `services/sea_view_service.py` asks
to keep for 30 days lived exactly as long as the interpreter that fetched them,
and every deploy restart, every `docker exec` and every `compose run` sibling
re-paid Overpass for the same cells.

Setting `REDIS_URL` fixes that and introduces a failure mode the code had never
had. `SimpleCache` cannot fail; a network backend can. And the twenty-one call
sites in `travel_time_service`, `property_travel_service` and
`enrichment_service` call `get_cached_enrichment_data` /
`cache_enrichment_data` unguarded — correctly, given the backend they were
written against. On the Google paths the *write* happens after the paid call,
so an unreachable Redis would have spent the money and then thrown the result
away; on a read it would have surfaced as a measurement that could not be taken
rather than one that was merely not cached.

So the guard belongs in the primitive, and this file pins it: a cache is an
optimisation, and failing to reach it means "fetch it", never "this could not
be measured" — #98's distinction, one layer down.
"""

from __future__ import annotations

import logging

import pytest

from utils import cache as cache_module


class _DeadBackend:
    """A cache whose every operation refuses, the way an unreachable Redis does."""

    def __init__(self):
        self.reads = 0
        self.writes = 0

    def get(self, *_args, **_kwargs):
        self.reads += 1
        raise ConnectionError("Error 61 connecting to redis:6379. Connection refused.")

    def set(self, *_args, **_kwargs):
        self.writes += 1
        raise ConnectionError("Error 61 connecting to redis:6379. Connection refused.")


@pytest.fixture
def dead_cache(monkeypatch):
    backend = _DeadBackend()
    monkeypatch.setattr(cache_module, "cache", backend)
    return backend


def test_a_read_from_a_dead_cache_is_a_miss(dead_cache, caplog):
    with caplog.at_level(logging.WARNING, logger="utils.cache"):
        result = cache_module.get_cached_enrichment_data(43.5, -5.8, "sea_view_cell")

    assert result is None, "an unreachable cache must read as a miss, not raise"
    assert dead_cache.reads == 1, "the backend should have been asked exactly once"
    assert any("Cache read failed" in r.message for r in caplog.records), (
        "a cache outage turns every hit into a paid round trip; it must be "
        "visible in the log rather than silent"
    )


def test_a_write_to_a_dead_cache_does_not_reach_the_caller(dead_cache, caplog):
    with caplog.at_level(logging.WARNING, logger="utils.cache"):
        # No assertion needed beyond "this returns": on the Google paths this
        # call happens *after* the billed request, so raising here discards a
        # measurement that has already been paid for.
        cache_module.cache_enrichment_data(43.5, -5.8, "places_hospital", {"ok": 1})

    assert dead_cache.writes == 1
    assert any("Cache write failed" in r.message for r in caplog.records)


def test_a_failed_write_is_not_reported_as_cached(dead_cache, caplog):
    """The log must not claim to have cached what it did not store."""
    with caplog.at_level(logging.DEBUG, logger="utils.cache"):
        cache_module.cache_enrichment_data(43.5, -5.8, "places_hospital", {"ok": 1})

    assert not any("Cached enrichment data" in r.message for r in caplog.records), (
        "cache_enrichment_data used to log success unconditionally, which is "
        "the same defect as a probe whose failure reads as a negative answer"
    )


def test_the_api_decorator_still_returns_the_real_result(dead_cache):
    """A dead cache must cost a recomputation, not the answer."""
    calls = {"n": 0}

    @cache_module.cache_api_response(timeout=60)
    def expensive():
        calls["n"] += 1
        return {"value": 42}

    assert expensive() == {"value": 42}
    assert expensive() == {"value": 42}
    assert calls["n"] == 2, "every call recomputes while the cache is down"
    assert dead_cache.reads == 2 and dead_cache.writes == 2


def test_a_working_cache_is_still_used(monkeypatch):
    """The guard must not turn every read into a miss when the backend is fine."""
    store = {}

    class _LiveBackend:
        def get(self, key):
            return store.get(key)

        def set(self, key, value, timeout=None):
            store[key] = value

    monkeypatch.setattr(cache_module, "cache", _LiveBackend())

    cache_module.cache_enrichment_data(43.5, -5.8, "sea_view_cell", {"points": 3})
    assert cache_module.get_cached_enrichment_data(43.5, -5.8, "sea_view_cell") == {
        "points": 3
    }
