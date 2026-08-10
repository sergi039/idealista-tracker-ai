"""Flask state must not outlive the test that created it.

These tests pin the conftest guard (the `pytest_runtest_teardown` wrapper),
born from the #196 follow-up branch's divergence: a sea-view test that passed
file-alone failed in the full suite, because flask-caching's module-global
`Cache` retains the last app it was initialised for and serves real cached
values with no app context at all. The guard has two halves -- scrub the
retained app after every test, and fail a test that leaves an app/request
context pushed -- and each half is pinned here.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import flask
import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app  # noqa: E402
from utils.cache import (  # noqa: E402
    cache,
    cache_enrichment_data,
    get_cached_enrichment_data,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The guarded subprocess imports the app factory from a cold start.
INNER_PYTEST_TIMEOUT_S = 120


class TestTheAmbientCacheBindingIsScrubbed:
    """An order-coupled pair, deliberately: the first test arms the ambient
    cache exactly the way any test calling create_app() does, and the second
    proves the guard disarmed it before the next test began. pytest runs
    methods in definition order."""

    def test_creating_an_app_arms_the_ambient_cache(self):
        create_app(testing=True)
        assert not flask.has_app_context()

        # No app context, yet the cache round-trips: this is flask-caching
        # 2.3.1 retaining the app (`Cache._set_cache` sets `self.app`) and
        # `current_app or self.app` falling through to it. If this assertion
        # ever fails, flask-caching stopped retaining the app and the scrub
        # half of the conftest guard has lost its reason to exist.
        cache_enrichment_data(41.0, 3.0, "isolation-probe", {"armed": True})
        assert get_cached_enrichment_data(41.0, 3.0, "isolation-probe") == {
            "armed": True
        }

    def test_the_binding_did_not_survive_into_this_test(self):
        assert "app" not in vars(cache), (
            "the conftest guard did not scrub the app flask-caching retained "
            "from the previous test"
        )
        # The fresh-process behaviour the scrub restores: the backend lookup
        # raises, and cache-using code paths treat that as "no cache".
        with pytest.raises(AttributeError):
            get_cached_enrichment_data(41.0, 3.0, "isolation-probe")


class TestALeakedContextFailsLoudly:
    def test_a_test_that_leaves_a_context_pushed_is_failed_at_teardown(self, tmp_path):
        """The guard must name the offending test itself, not let the leak
        surface as an unrelated failure files later. In-suite a leak cannot be
        staged (the guard would fail this test), so run a one-test suite under
        the same conftest in a subprocess."""
        (tmp_path / "conftest.py").write_text(
            (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (tmp_path / "test_leaky.py").write_text(
            textwrap.dedent(
                """
                from tests import setup_test_environment

                setup_test_environment()

                from app import create_app


                def test_pushes_and_never_pops():
                    create_app(testing=True).app_context().push()
                """
            ),
            encoding="utf-8",
        )

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path),
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=INNER_PYTEST_TIMEOUT_S,
        )

        assert result.returncode != 0, (
            "a leaked app context passed the guarded suite:\n" + result.stdout
        )
        assert "left 1 Flask context(s) pushed" in result.stdout
        assert "test_pushes_and_never_pops" in result.stdout
