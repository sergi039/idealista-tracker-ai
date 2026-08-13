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
    init_cache,
)


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
