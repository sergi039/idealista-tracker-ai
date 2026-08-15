"""One page-check rule, one place, read by both deployers (#292).

"A deploy is healthy when a page renders, not when healthz answers" was
discovered twice on 2026-08-14 and written down twice: `AUTOPILOT_PAGE_URL` in
`tools/autopilot/deploy_watcher.sh` and `AUTO_REBUILD_RENDER_PATH` in
`.githooks/post-merge`. CLAUDE.md warned "change both or neither", which is
the shape of defect that eventually ships half-changed.

The behaviour of each consumer is pinned where that consumer lives -
`tests/test_post_merge_hook.py` moves `DEPLOY_RENDER_PATH` and requires the
hook to follow it, `tools/autopilot/deploy_inflight_test.sh` does the same to
the watcher. What is pinned here is the property those two cannot see between
them: that neither one carries its own copy of the rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_LIB = REPO_ROOT / "tools" / "autopilot" / "lib" / "render_check.sh"
CONSUMERS = (
    REPO_ROOT / ".githooks" / "post-merge",
    REPO_ROOT / "tools" / "autopilot" / "deploy_watcher.sh",
)


def code_lines(path: Path) -> list[str]:
    """The script without its comments - the incidents are narrated in those."""
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def run_lib(script: str, env: dict[str, str] | None = None) -> str:
    """Source the contract and run `script` against it."""
    return subprocess.run(
        ["bash", "-c", f'set -eu; . "{RENDER_LIB}"\n{script}'],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    ).stdout


def test_the_contract_names_the_page_once():
    assert RENDER_LIB.is_file()
    assert run_lib('printf "%s" "$DEPLOY_RENDER_PATH"') == "/properties"


@pytest.mark.parametrize("consumer", CONSUMERS, ids=lambda p: p.name)
def test_both_deployers_read_the_contract(consumer: Path):
    body = consumer.read_text()
    assert "render_check.sh" in body, (
        f"{consumer.name} does not source the shared page-check contract"
    )
    assert "deploy_render_url" in body and "deploy_render_ok" in body, (
        f"{consumer.name} sources the contract but decides the check itself"
    )


@pytest.mark.parametrize("consumer", CONSUMERS, ids=lambda p: p.name)
def test_neither_deployer_keeps_its_own_copy_of_the_page(consumer: Path):
    """The literal belongs to the contract. A second copy is the whole defect:
    two places that must move together, and one of them eventually does not."""
    offenders = [line for line in code_lines(consumer) if "/properties" in line]
    assert not offenders, (
        f"{consumer.name} hard-codes the page instead of reading "
        f"DEPLOY_RENDER_PATH:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("consumer", CONSUMERS, ids=lambda p: p.name)
def test_the_retired_names_are_not_read_any_more(consumer: Path):
    """Both old knobs are gone from the logic; only the notice may name them."""
    for retired in ("AUTOPILOT_PAGE_URL", "AUTO_REBUILD_RENDER_PATH"):
        offenders = [line for line in code_lines(consumer) if retired in line]
        assert not offenders, f"{consumer.name} still reads {retired}:\n" + "\n".join(
            offenders
        )


def test_the_url_is_the_origin_plus_the_page():
    assert (
        run_lib('deploy_render_url "http://127.0.0.1:5001"')
        == "http://127.0.0.1:5001/properties"
    )
    # A trailing slash on the origin must not double up.
    assert (
        run_lib('deploy_render_url "http://127.0.0.1:5001/"')
        == "http://127.0.0.1:5001/properties"
    )
    assert (
        run_lib(
            'deploy_render_url "http://127.0.0.1:5001"',
            {"DEPLOY_RENDER_PATH": "/dashboard"},
        )
        == "http://127.0.0.1:5001/dashboard"
    )


def test_an_empty_page_turns_the_check_off_rather_than_fetching_the_root():
    """`/` renders too, and redirects - accepting it would be a check that
    always fails, which is as bad as one that always passes."""
    assert (
        run_lib('deploy_render_url "http://127.0.0.1:5001"', {"DEPLOY_RENDER_PATH": ""})
        == ""
    )


def test_the_origin_drops_whatever_path_the_caller_had():
    """The watcher starts from a health URL, not from a bare origin."""
    assert (
        run_lib('deploy_render_origin "http://127.0.0.1:5001/api/healthz"')
        == "http://127.0.0.1:5001"
    )
    assert run_lib('deploy_render_origin "https://host/x/y"') == "https://host"


def test_a_still_set_retired_name_is_named_rather_than_ignored():
    assert run_lib("deploy_render_legacy_vars") == ""
    assert (
        run_lib("deploy_render_legacy_vars", {"AUTOPILOT_PAGE_URL": ""})
        == "AUTOPILOT_PAGE_URL\n"
    )
    assert (
        run_lib("deploy_render_legacy_vars", {"AUTO_REBUILD_RENDER_PATH": "/x"})
        == "AUTO_REBUILD_RENDER_PATH\n"
    )
