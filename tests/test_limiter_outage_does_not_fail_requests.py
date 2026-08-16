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

**Why the behavioural half runs in a subprocess.** The limiter is built at
module import from the environment, so exercising a different storage means
importing `app` under a different `REDIS_URL`. The first version of this file
did that with `importlib.reload(app_module)` in-process and turned the suite
into `1873 passed, 653 errors`: reloading `app` builds a second `SQLAlchemy()`
and a second set of models while every already-imported test module still holds
the first. The pre-push gate refused the push, which is what it is for.
`test_flask_state_isolation.py` reached the same conclusion for its own reason
and runs its guarded case in a subprocess; this follows it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests import setup_test_environment

setup_test_environment()

from app import limiter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# A port nothing listens on, so the real redis storage path runs and fails the
# real way. Reserved-but-unused beats a random high port: it cannot accidentally
# reach a Redis another session started.
DEAD_REDIS = "redis://127.0.0.1:1/0"

# One of the 15 rate-limited endpoints; 10 per minute. Its own response needs a
# database the subprocess does not provide, so the assertions are about
# *reaching* a response and about the two storages agreeing — not the status.
RATE_LIMITED_ROUTE = "/api/land/1/enrich"

SUBPROCESS_TIMEOUT_S = 120

# The route is passed by environment, not formatted in: `.format()` reads the
# probe's own `{"raised": ...}` literal as a placeholder and dies at collection
# with KeyError.
_PROBE = textwrap.dedent(
    """
    import json, os, sys

    from tests import setup_test_environment

    setup_test_environment()
    if os.environ.get("PROBE_REDIS_URL"):
        os.environ["REDIS_URL"] = os.environ["PROBE_REDIS_URL"]
    else:
        os.environ.pop("REDIS_URL", None)

    from app import create_app

    client = create_app(testing=True).test_client()
    out = {"raised": None, "statuses": []}
    try:
        for _ in range(12):
            out["statuses"].append(client.post(os.environ["PROBE_ROUTE"]).status_code)
    except Exception as exc:                      # noqa: BLE001 - reporting it
        out["raised"] = type(exc).__name__
    print("PROBE " + json.dumps(out))
    """
)


def _probe(storage_uri):
    """Hit the rate-limited route twelve times under `storage_uri`."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PROBE_ROUTE": RATE_LIMITED_ROUTE,
    }
    if storage_uri is None:
        env.pop("PROBE_REDIS_URL", None)
    else:
        env["PROBE_REDIS_URL"] = storage_uri
    # `REDIS_URL` is one switch for two consumers: the healthy baseline cannot
    # be spelled `memory://`, because that gives the limiter what it wants and
    # hands utils/cache.py a Redis URL it rejects outright. Unset is the
    # baseline, and the probe honours that.
    env.pop("REDIS_URL", None)

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("PROBE ")), None
    )
    assert line, (
        f"probe produced no verdict\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(line[len("PROBE ") :])


def test_a_dead_rate_limit_store_does_not_take_the_route_down():
    out = _probe(DEAD_REDIS)

    assert out["raised"] is None, (
        "a storage error escaped the request; before the fix this was "
        f"{out['raised']} straight out of flask_limiter's _check_request_limit"
    )


def test_an_outage_is_invisible_to_the_caller():
    """The two storages must agree: the limiter is transparent either way."""
    healthy = _probe(None)
    broken = _probe(DEAD_REDIS)

    assert broken["statuses"][0] == healthy["statuses"][0], (
        "a request served with a healthy limit store and one served with an "
        "unreachable one must look the same to the caller; the difference "
        "belongs in the log, not in the response"
    )


def test_the_limit_is_still_enforced_while_the_store_is_unreachable():
    """Fallback, not removal.

    `in_memory_fallback_enabled` keeps counting in this process, so an outage
    relaxes a shared limit to a per-process one rather than lifting it. The
    route allows 10 per minute, so twelve calls must include a refusal.
    """
    out = _probe(DEAD_REDIS)

    assert 429 in out["statuses"], (
        "with the shared store unreachable the limit must fall back to this "
        "process, not disappear - swallow_errors alone would have removed it"
    )


def test_both_flags_are_set():
    """Both, not one.

    `swallow_errors` alone stops the failure and silently removes the limit;
    `in_memory_fallback_enabled` alone still lets a storage error escape. The
    pair is the fix, so the pair is asserted - on the module the suite already
    imported, with no reload.
    """
    assert limiter._in_memory_fallback_enabled is True
    assert limiter._swallow_errors is True
