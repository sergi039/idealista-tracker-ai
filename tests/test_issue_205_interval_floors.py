"""A floor on the env-configurable interval/TTL constants (issue #205,
filed as a follow-up review comment on #203/#204).

`LEASE_TTL_SECONDS`, `HEARTBEAT_INTERVAL_S` and `RECONCILE_INTERVAL_S`
(services/background_jobs.py) used to be a bare
`int(os.environ.get(NAME, default))`, with no floor -- unlike the
neighbouring `_MAX_WORKERS`, which already guarded itself with
`max(1, ...)`. A `0` or negative override does not fail loudly the way a
non-numeric one does (`int()` still raises for that, unchanged): it parses
fine and then produces silently destructive behaviour instead, three
different ways -- see `_MIN_INTERVAL_S`'s own comment in
services/background_jobs.py for exactly what each of the three used to be
(a heartbeat thread killed outright by `time.sleep(negative)`, an
APScheduler interval that only clamps `0`, not negative, and a lease that is
already expired the instant it is written).

`_int_env_with_floor` is the single parsing path all three constants go
through; these tests exercise it directly (fast, no subprocess, and no risk
of leaving the already-imported module-level constants themselves mutated
for the rest of the suite -- they are computed once at import time, so
reaching them any other way would mean reloading the module and carefully
restoring it afterwards). The second test below closes the gap a
helper-only test would leave: that the three real constants actually call
this helper, not just that the helper itself is correct in isolation -- run
in a fresh subprocess so it proves the real, once-at-import wiring rather
than anything reload-related.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services import background_jobs

_ENV_VAR = "BACKGROUND_JOBS_TEST_ONLY_INTERVAL"


@pytest.mark.parametrize("bad_value", ["0", "-1", "-3600"])
def test_int_env_with_floor_clamps_a_non_positive_override(monkeypatch, bad_value):
    monkeypatch.setenv(_ENV_VAR, bad_value)
    assert (
        background_jobs._int_env_with_floor(_ENV_VAR, "180")
        == background_jobs._MIN_INTERVAL_S
    )


def test_int_env_with_floor_passes_a_valid_override_through_unchanged(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "42")
    assert background_jobs._int_env_with_floor(_ENV_VAR, "180") == 42


def test_int_env_with_floor_uses_the_default_when_unset(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)
    assert background_jobs._int_env_with_floor(_ENV_VAR, "180") == 180


def test_int_env_with_floor_still_raises_on_a_non_numeric_override(monkeypatch):
    """The floor is not a silent fallback for a typo -- a non-numeric value
    must keep failing exactly as it did before this change (fail fast at
    import time, per CLAUDE.md's "do not add silent fallbacks" rule)."""
    monkeypatch.setenv(_ENV_VAR, "not-a-number")
    with pytest.raises(ValueError):
        background_jobs._int_env_with_floor(_ENV_VAR, "180")


def test_min_interval_s_is_itself_a_positive_floor():
    """Sanity pin: whatever the floor's value is, it must actually floor --
    a regression that set it to 0 or less would defeat every test above
    that only checks the *result* is `_MIN_INTERVAL_S`."""
    assert background_jobs._MIN_INTERVAL_S >= 1


def test_a_non_positive_env_value_is_floored_for_every_real_configured_interval():
    """End-to-end proof, in a fresh subprocess: `BACKGROUND_JOB_
    LEASE_TTL_SECONDS` / `_HEARTBEAT_INTERVAL_S` / `_RECONCILE_INTERVAL_S`
    set to a non-positive value must not reach the module's own constants
    unfloored. A subprocess (rather than `importlib.reload`) so this proves
    the real, once-at-import wiring in a process that never runs anything
    else -- and so a failure here cannot leave `services.background_jobs`'s
    already-imported constants mutated for the rest of this suite, since
    each of the three is computed exactly once, at import time, and nothing
    later in the process re-reads the environment for them.
    """
    project_root = Path(__file__).parent.parent
    for bad_value in ("0", "-30"):
        env = {
            **os.environ,
            "BACKGROUND_JOB_LEASE_TTL_SECONDS": bad_value,
            "BACKGROUND_JOB_HEARTBEAT_INTERVAL_S": bad_value,
            "BACKGROUND_JOB_RECONCILE_INTERVAL_S": bad_value,
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json\n"
                "from services.background_jobs import (\n"
                "    LEASE_TTL_SECONDS,\n"
                "    HEARTBEAT_INTERVAL_S,\n"
                "    RECONCILE_INTERVAL_S,\n"
                ")\n"
                "print(json.dumps({\n"
                "    'lease': LEASE_TTL_SECONDS,\n"
                "    'heartbeat': HEARTBEAT_INTERVAL_S,\n"
                "    'reconcile': RECONCILE_INTERVAL_S,\n"
                "}))\n",
            ],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"importing services.background_jobs failed for env value "
            f"{bad_value!r}: {result.stderr}"
        )
        values = json.loads(result.stdout.strip())
        for name, value in values.items():
            assert value >= 1, (
                f"env value {bad_value!r} produced {name}={value}, "
                "not floored to a positive interval"
            )
