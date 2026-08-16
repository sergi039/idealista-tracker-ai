"""The 30-day coastline cache needs a backend that outlives a process (#356).

`services/sea_view_service.py` asks for 30 days; `utils/cache.py` delivers
`SimpleCache` unless `REDIS_URL` is set, and it was set nowhere — no `redis`
service in `docker-compose.yml`, no variable in the app container. So the
intent was honoured for exactly as long as the interpreter that filled it, and
the backfill re-paid Overpass for the same 0.1° cells after every deploy kill,
every `docker exec` and every `compose run`.

These assertions are on the compose file as text, the way
`tests/test_isolation_rules.py` reads it: the deployment is what was wrong, so
the deployment is what has to be pinned. pyyaml is not a dependency of this
project and adding one to assert on four lines would be the larger change.
"""

from __future__ import annotations

from pathlib import Path

COMPOSE = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
    encoding="utf-8"
)


def _service_block(name: str) -> str:
    """The lines of one service, up to the next top-level key.

    Matched on the exact two-space indent a service key carries, not on a
    stripped line: `redis:` also appears at six spaces inside the app's
    `depends_on`, and matching that returned the app's block for every question
    asked about redis. The first version of this helper did exactly that.

    Comments are stripped, and that is load bearing rather than tidy. The
    first version of this file asserted `"volatile-lru" in redis` while the
    block also carried the comment *"volatile-lru, not allkeys-lru"* — so the
    assertion matched the prose and passed with the setting changed to
    `allkeys-lru`. A mutation caught it; reading the test did not. The same
    trap was one word away from `ports:`, whose neighbouring comment explains
    why there is no published port.
    """
    lines = COMPOSE.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    out = []
    for line in lines[start + 1 :]:
        if line and not line.startswith("    ") and not line.startswith("\t"):
            break
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_a_redis_service_exists():
    assert "\n  redis:\n" in COMPOSE, (
        "without a redis service REDIS_URL points at nothing and utils/cache.py "
        "silently falls back to SimpleCache - the defect this closes"
    )


def test_the_app_is_given_redis_url():
    app = _service_block("app")
    assert "REDIS_URL=" in app, (
        "the service existing is not the same as the app being told about it; "
        "utils/cache.py switches on REDIS_URL and nothing else"
    )
    assert "redis://redis:6379" in app


def test_the_app_waits_for_redis_to_be_healthy():
    app = _service_block("app")
    assert "redis:\n        condition: service_healthy" in app, (
        "an app that starts before redis answers configures a Redis cache "
        "against a socket that is not listening yet"
    )


def test_redis_publishes_no_host_port():
    redis = _service_block("redis")
    assert "ports:" not in redis, (
        "this stack has no authentication and is safe only because nothing is "
        "reachable off the host; the app, a docker exec and a compose run "
        "sibling all reach redis over the compose network"
    )


def test_the_cache_survives_a_redis_restart():
    redis = _service_block("redis")
    assert "--appendonly" in redis, (
        "the ticket is about surviving a process; a cache that dies with its "
        "own container re-pays Overpass for every cell exactly as SimpleCache did"
    )
    assert "redisdata:/data" in redis


def test_memory_is_bounded_and_only_expiring_keys_are_evicted():
    redis = _service_block("redis")
    assert "--maxmemory" in redis, (
        "an unbounded cache on a shared machine is a memory leak with a TTL"
    )
    assert "volatile-lru" in redis, (
        "allkeys-lru would evict a key that carries no TTL, which in this app "
        "would mean a bug is discarded instead of noticed"
    )


def test_every_global_name_is_prefixed():
    """A branch build must not inherit - or poison - a 30-day production cache."""
    assert "${COMPOSE_CONTAINER_PREFIX:-idealista}-redis\n" in COMPOSE
    assert "${COMPOSE_CONTAINER_PREFIX:-idealista}-redis-data" in COMPOSE
