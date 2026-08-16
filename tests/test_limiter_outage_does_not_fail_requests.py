"""An unreachable rate-limit store costs the limit, never the request (#356).

`app.py` builds its `Limiter` with `storage_uri=os.environ.get("REDIS_URL",
"memory://")`. Until #356 that variable was set nowhere, so the storage was a
dict and could not fail. Introducing Redis for the coastline cache flips the
limiter too — the same switch — and flask-limiter checks the limit *inside* the
request, where `app.py` handles only `HTTPException`. So a stopped Redis did not
degrade the 15 rate-limited routes in `routes/api_routes.py`; it removed them,
and those are the AI analysis, the bulk and manual enrichment, the email ingest
and the status check: the buttons someone presses when something is already
wrong.

Measured before the fix, against a real Redis on a throwaway port: with Redis up
the route answered, and after `docker stop` the same call raised
`ConnectionError` out of the request.

This is the cache's own rule one step outside the scope it was written for.
`utils/cache.py` got the outage guard in the same PR; the limiter, flipped by
the same variable, got nothing.

No Redis is needed to pin it: a `redis://` URL pointing at a port where nothing
listens exercises the real storage path and fails the real way.
"""

from __future__ import annotations

import importlib
import os

from tests import setup_test_environment

setup_test_environment()

# A port nothing listens on. Reserved-but-unused beats a random high port: it
# cannot accidentally reach a Redis another session started.
DEAD_REDIS = "redis://127.0.0.1:1/0"

# One of the 15 rate-limited endpoints. Its own response depends on a database
# this test does not provide, so the assertions below are about *reaching* a
# response at all, and about the two storages agreeing - not about the status.
RATE_LIMITED_ROUTE = "/api/land/1/enrich"


def _client_with_storage(monkeypatch, storage_uri):
    """A fresh app whose limiter was constructed against `storage_uri`.

    The `Limiter` is built at module import from the environment, so the module
    has to be reloaded after the variable is set - importing `app` is not enough.

    `storage_uri=None` means the variable is *unset*, which is the healthy
    baseline. It cannot be spelled `memory://`: `REDIS_URL` is one switch for
    two consumers, so setting it to a non-Redis URI gives the limiter what it
    wants and hands `utils/cache.py` a Redis URL it rejects outright
    (`ValueError: Redis URL must specify one of the following schemes`). The
    first version of this helper did exactly that - the shared switch catching
    the test that exists because the switch is shared.
    """
    if storage_uri is None:
        monkeypatch.delenv("REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("REDIS_URL", storage_uri)
    import app as app_module

    importlib.reload(app_module)
    return app_module.create_app(testing=True).test_client()


def test_a_dead_rate_limit_store_does_not_take_the_route_down(monkeypatch):
    client = _client_with_storage(monkeypatch, DEAD_REDIS)

    # The assertion is that this returns rather than raising. Before the fix
    # it raised redis.exceptions.ConnectionError straight out of
    # flask_limiter's _check_request_limit.
    response = client.post(RATE_LIMITED_ROUTE)

    assert response.status_code != 429, (
        "an unreachable store must not be read as 'limit exceeded'"
    )


def test_an_outage_is_invisible_to_the_caller(monkeypatch):
    """The two storages must agree: the limiter is transparent either way."""
    working = _client_with_storage(monkeypatch, None).post(RATE_LIMITED_ROUTE)
    broken = _client_with_storage(monkeypatch, DEAD_REDIS).post(RATE_LIMITED_ROUTE)

    assert broken.status_code == working.status_code, (
        "a request served with a healthy limit store and one served with an "
        "unreachable one must look the same to the caller; the difference "
        "belongs in the log, not in the response"
    )


def test_the_limit_is_still_enforced_while_the_store_is_unreachable(monkeypatch):
    """Fallback, not removal.

    `in_memory_fallback_enabled` keeps counting in this process, so an outage
    relaxes a shared limit to a per-process one rather than lifting it. The
    route allows 10 per minute; the eleventh call must still be refused.
    """
    client = _client_with_storage(monkeypatch, DEAD_REDIS)

    statuses = [client.post(RATE_LIMITED_ROUTE).status_code for _ in range(12)]

    assert 429 in statuses, (
        "with the shared store unreachable the limit must fall back to this "
        "process, not disappear - swallow_errors alone would have removed it"
    )


def test_the_configuration_says_so(monkeypatch):
    """Both flags, not one.

    `swallow_errors` alone stops the 500 and silently removes the limit;
    `in_memory_fallback_enabled` alone still raises on a storage error in some
    paths. The pair is the fix, so the pair is asserted.
    """
    monkeypatch.setenv("REDIS_URL", DEAD_REDIS)
    import app as app_module

    importlib.reload(app_module)

    assert app_module.limiter._in_memory_fallback_enabled is True
    assert app_module.limiter._swallow_errors is True


def test_the_default_is_still_in_memory_when_redis_url_is_absent(monkeypatch):
    """The fallback must not quietly become the only behaviour."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    import app as app_module

    importlib.reload(app_module)

    assert "memory://" in str(app_module.limiter._storage_uri)


def teardown_module(_module):
    """Leave `app` imported the way the rest of the suite expects it.

    This file reloads the module with a doctored environment; a later test
    importing `app` must not inherit a limiter pointed at a dead Redis.
    """
    os.environ.pop("REDIS_URL", None)
    import app as app_module

    importlib.reload(app_module)
