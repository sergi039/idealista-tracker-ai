"""CACHE_TYPE names a backend class, not a deprecated factory function.

`utils/cache.py` used to pass the bare names "redis" / "simple". flask-caching
resolves a dotless CACHE_TYPE to a module-level factory *function* in
`flask_caching.backends` and warns that doing so is deprecated - which is where
~1000 of the suite's 1101 warnings came from, one per `create_app()`.

The cost is not the noise. The lookup happens inside `init_cache()`, which
`create_app()` calls unguarded, so the day the library drops those functions the
app stops starting. Deleting them locally turns this file's own app-building
tests into errors rather than failures, which is what a Dependabot bump would
show as a red required `pytest` check.

These tests fail if the short names come back, and if `init_cache` ever emits a
DeprecationWarning again.

The second half covers where that name is *read* from. Both reporting helpers
used to read `cache.config` - the `Cache()` constructor argument, which this
module never sets - so `get_cache_stats()` always answered "unknown" and
`clear_cache_pattern()` always took its unsupported branch, reporting success at
clearing nothing on a Redis deployment.
"""

from __future__ import annotations

import warnings

import pytest
from flask import Flask

from utils import cache as cache_module
from utils.cache import (
    REDIS_CACHE_BACKEND,
    SIMPLE_CACHE_BACKEND,
    _backend_name,
    active_backend_name,
    cache,
    clear_cache_pattern,
    get_cache_stats,
    init_cache,
)


class _FakeRedisClient:
    """The two attributes the Redis branches actually use."""

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.deleted = []

    def scan_iter(self, match=None):
        self.match = match
        return iter(self.keys)

    def delete(self, key):
        self.deleted.append(key)

    def info(self):
        return {
            "used_memory_human": "1.5M",
            "connected_clients": 3,
            "total_commands_processed": 42,
        }


def _app_with_redis_backend(monkeypatch, client):
    """An app whose configured cache object looks like the Redis backend."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    app = Flask(__name__)
    init_cache(app)

    fake_backend = type("FakeRedisCache", (), {"_write_client": client})()
    app.extensions["cache"][cache] = fake_backend
    return app


def _init_and_collect_warnings(monkeypatch, redis_url):
    """Run init_cache on a throwaway app; return (app, deprecation warnings)."""
    if redis_url is None:
        monkeypatch.delenv("REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("REDIS_URL", redis_url)

    app = Flask(__name__)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        init_cache(app)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    return app, deprecations


class TestBackendIsNamedByClassPath:
    def test_the_in_memory_backend_is_a_class_path(self, monkeypatch):
        app, _ = _init_and_collect_warnings(monkeypatch, None)

        assert app.config["CACHE_TYPE"] == SIMPLE_CACHE_BACKEND
        assert "." in app.config["CACHE_TYPE"], (
            "a dotless CACHE_TYPE resolves to the deprecated factory function"
        )

    def test_the_redis_backend_is_a_class_path(self, monkeypatch):
        app, _ = _init_and_collect_warnings(monkeypatch, "redis://127.0.0.1:6379/0")

        assert app.config["CACHE_TYPE"] == REDIS_CACHE_BACKEND
        assert app.config["CACHE_REDIS_URL"] == "redis://127.0.0.1:6379/0"

    def test_both_class_paths_import_to_real_backend_classes(self):
        from flask_caching.backends import RedisCache, SimpleCache
        from werkzeug.utils import import_string

        assert import_string(REDIS_CACHE_BACKEND) is RedisCache
        assert import_string(SIMPLE_CACHE_BACKEND) is SimpleCache


class TestInitCacheIsWarningFree:
    @pytest.mark.parametrize(
        "redis_url", [None, "redis://127.0.0.1:6379/0"], ids=["simple", "redis"]
    )
    def test_it_emits_no_deprecation_warning(self, monkeypatch, redis_url):
        _, deprecations = _init_and_collect_warnings(monkeypatch, redis_url)

        assert deprecations == [], (
            "init_cache emitted "
            f"{[str(w.message) for w in deprecations]} - the CACHE_TYPE value is "
            "resolving to a deprecated factory function again"
        )


class TestBackendNameReadsEitherSpelling:
    @pytest.mark.parametrize(
        ("cache_type", "expected"),
        [
            (REDIS_CACHE_BACKEND, "redis"),
            (SIMPLE_CACHE_BACKEND, "simple"),
            # A legacy short name in someone's config still reads as itself, so
            # the "redis" branches below keep working across the change.
            ("redis", "redis"),
            ("simple", "simple"),
            # Anything unrecognised is passed through rather than guessed at.
            ("flask_caching.backends.NullCache", "flask_caching.backends.NullCache"),
            (None, "unknown"),
            ("", "unknown"),
        ],
    )
    def test_it_maps_a_cache_type_to_a_short_name(self, cache_type, expected):
        assert _backend_name(cache_type) == expected

    def test_the_module_exposes_the_two_backends_it_configures(self):
        assert cache_module._BACKEND_SHORT_NAMES == {
            REDIS_CACHE_BACKEND: "redis",
            SIMPLE_CACHE_BACKEND: "simple",
        }


class TestTheBackendIsReadFromTheRunningApp:
    """`cache.config` is the constructor argument and is always None here."""

    def test_it_reads_the_app_config_not_the_cache_config(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        app = Flask(__name__)
        init_cache(app)

        assert cache.config is None, (
            "if flask-caching starts populating this, revisit active_backend_name"
        )
        with app.app_context():
            assert active_backend_name() == "simple"

    def test_a_redis_app_reports_redis(self, monkeypatch):
        app = _app_with_redis_backend(monkeypatch, _FakeRedisClient())

        with app.app_context():
            assert active_backend_name() == "redis"

    def test_outside_an_application_context_it_answers_unknown(self):
        assert active_backend_name() == "unknown"


class TestCacheStatsReportTheRealBackend:
    def test_the_in_memory_backend_is_named(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        app = Flask(__name__)
        init_cache(app)

        with app.app_context():
            assert get_cache_stats() == {"backend": "simple", "available": True}

    def test_redis_stats_are_collected(self, monkeypatch):
        client = _FakeRedisClient()
        app = _app_with_redis_backend(monkeypatch, client)

        with app.app_context():
            stats = get_cache_stats()

        assert stats["backend"] == "redis"
        assert stats["used_memory"] == "1.5M"
        assert stats["connected_clients"] == 3
        assert stats["total_commands"] == 42


class TestPatternClearingReachesRedis:
    def test_it_deletes_every_matching_key(self, monkeypatch):
        client = _FakeRedisClient(keys=["api:a", "api:b"])
        app = _app_with_redis_backend(monkeypatch, client)

        with app.app_context():
            clear_cache_pattern("api:*")

        assert client.match == "api:*"
        assert client.deleted == ["api:a", "api:b"], (
            "the Redis branch was never reached - this is the CACHE-002 defect"
        )

    def test_the_in_memory_backend_clears_nothing_and_says_so(
        self, monkeypatch, caplog
    ):
        monkeypatch.delenv("REDIS_URL", raising=False)
        app = Flask(__name__)
        init_cache(app)

        with app.app_context(), caplog.at_level("WARNING"):
            clear_cache_pattern("api:*")

        assert "not supported" in caplog.text
