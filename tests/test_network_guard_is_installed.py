"""The guard cannot be switched off by accident and stay green (issue #307).

`tests/test_network_guard.py` asserts on refusals, so every test in it is
gated by `skipif(not network_guard.installed())` -- correct on its own terms,
and the reason this file exists. That gate reads *installed at all* as
*deliberately switched off*, so dropping the `network_guard.install()` call in
`tests/conftest.py` -- a plausible casualty of a refactor there, since three
independent guards share that file -- turns all 29 of them into skips. Nothing
else in the suite reaches the live internet today, so the run stays green and
exits 0; measured on `main` at 12dee8c, removing that one line gave
`29 skipped in 0.03s`, exit 0. The only trace is a skip count that neither
`.github/workflows/ci.yml` nor `tools/ci/local_ci.sh` looks at.

That is the defect #307 exists to prevent, reproduced inside the mechanism
meant to catch it: a green run over code nobody exercised. So this one check
is *not* gated on `installed()` -- being uninstalled is exactly what it is
here to catch -- and it lives outside `tests/conftest.py` on purpose, because
a check sitting beside the call it guards disappears in the same edit.

The escape hatch stays honest: with `PYTEST_ALLOW_NETWORK` set the guard is
off by request, and this check skips like the rest.
"""

import os

import pytest

from tests import network_guard


def test_the_guard_is_installed_unless_switched_off_on_purpose():
    if os.environ.get(network_guard.DISABLE_ENV):
        pytest.skip(f"the guard is off by request ({network_guard.DISABLE_ENV})")

    assert network_guard.installed(), (
        "The network guard is not installed, and `PYTEST_ALLOW_NETWORK` is not "
        "set -- so this run is not refusing the live internet, and the tests in "
        "tests/test_network_guard.py are skipping rather than failing. Check "
        "that `pytest_configure` in tests/conftest.py still calls "
        "`network_guard.install()`."
    )
