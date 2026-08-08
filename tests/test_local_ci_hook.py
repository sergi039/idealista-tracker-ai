"""The pre-push hook must judge the pushed SHA, not the working tree.

Five review rounds on PR #78 (issue #74) all reduced to the same gap:
any shortcut that trusts the working tree ("it looks clean", "the suite
already ran") lets a broken committed blob through, or rejects a green
commit over local-only files. These tests run the real .githooks/pre-push
against a toy repository whose committed tools/ci/local_ci.sh is a stub
with a known exit code, then desynchronize the working tree from the
commit and assert the verdict follows the commit.

The last two tests cover the follow-up defect: the gate must not leak
git's own environment into the checks it spawns.
"""

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".githooks" / "pre-push"
ZERO_SHA = "0" * 40

# The variables that redirect git to a different repository. git exports
# GIT_DIR to hooks whenever the push comes from a linked worktree; the rest
# can be set by a wrapping tool. .githooks/pre-push and tools/ci/local_ci.sh
# must clear all of them before running anything.
GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
)


def _clean_env(**extra):
    """os.environ without the repo-redirecting git variables.

    Used for the helpers' own git calls so that running this suite from a
    shell that happens to have GIT_DIR set cannot make `git init` below
    re-initialise the caller's repository - which is the very accident
    these last two tests are about.
    """
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_VARS}
    env.update(extra)
    return env


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _write_gate_script(repo, body):
    gate = repo / "tools" / "ci" / "local_ci.sh"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(body)
    gate.chmod(0o755)
    return gate


def _write_gate(repo, exit_code):
    return _write_gate_script(repo, f"#!/bin/bash\nexit {exit_code}\n")


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _make_repo(tmp_path, committed_gate_exit):
    repo = tmp_path / "toy"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hook-test@example.invalid")
    _git(repo, "config", "user.name", "hook test")
    _write_gate(repo, committed_gate_exit)
    _commit_all(repo, "init")
    return repo


def _run_hook(repo, stdin_line=None, cwd=None, env=None):
    if stdin_line is None:
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        stdin_line = f"refs/heads/main {sha} refs/heads/main {ZERO_SHA}\n"
    return subprocess.run(
        ["bash", str(HOOK)],
        input=stdin_line,
        cwd=str(cwd or repo),
        capture_output=True,
        text=True,
        timeout=120,
        env=env if env is not None else _clean_env(),
    )


def test_rejects_red_commit_hidden_by_green_working_tree(tmp_path):
    """The reviewer's scenario: committed blob broken, tree 'fixed' on disk."""
    repo = _make_repo(tmp_path, committed_gate_exit=1)
    _write_gate(repo, 0)  # uncommitted green cover-up
    res = _run_hook(repo)
    assert res.returncode != 0, (
        "hook must fail on the committed (red) gate, got:\n" + res.stdout + res.stderr
    )


def test_accepts_green_commit_despite_red_working_tree(tmp_path):
    """The inverse: local-only breakage must not veto a green commit."""
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate(repo, 1)  # uncommitted red noise
    res = _run_hook(repo)
    assert res.returncode == 0, res.stdout + res.stderr


def test_branch_deletion_push_skips_gate(tmp_path):
    repo = _make_repo(tmp_path, committed_gate_exit=1)
    line = f"refs/heads/gone {ZERO_SHA} refs/heads/gone {ZERO_SHA}\n"
    res = _run_hook(repo, stdin_line=line)
    assert res.returncode == 0, res.stdout + res.stderr


def test_skip_local_ci_env_bypasses(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, committed_gate_exit=1)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    res = subprocess.run(
        ["bash", str(HOOK)],
        input=f"refs/heads/main {sha} refs/heads/main {ZERO_SHA}\n",
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SKIP_LOCAL_CI": "1"},
    )
    assert res.returncode == 0, res.stdout + res.stderr


def _push_from_linked_worktree(tmp_path, gate_body):
    """Reproduce a push started from a linked worktree, with `gate_body` committed.

    git only exports GIT_DIR to the hook when the push comes from a linked
    worktree, and only a linked worktree's gitdir triggers the damage: it has
    no work tree of its own, so a `git init` inheriting it re-initialises the
    shared repository as *bare*. A main-clone gitdir keeps core.bare=false,
    so a simulation using one would be a false canary.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, gate_body)
    _commit_all(repo, "gate that shells out to git")

    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    gitdir = repo / ".git" / "worktrees" / "linked"
    assert gitdir.is_dir(), "linked worktree gitdir not where the test expects it"

    res = _run_hook(repo, cwd=linked, env=_clean_env(GIT_DIR=str(gitdir)))
    return repo, res


def test_gate_does_not_turn_the_pushing_repo_bare(tmp_path):
    """Regression: the gate must not reconfigure the repo it was invoked from.

    A check that runs git against its own throwaway repository - which is
    exactly what this file does - must not be able to reach back and
    reconfigure the repository being pushed. Before the fix this wrote
    `bare = true` into the shared .git/config and every worktree of the
    real clone started failing with "must be run in a work tree".
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    gate = f"#!/bin/bash\ngit -C {shlex.quote(str(scratch))} init -q\nexit 0\n"

    repo, res = _push_from_linked_worktree(tmp_path, gate)

    assert res.returncode == 0, res.stdout + res.stderr
    is_bare = _git(repo, "rev-parse", "--is-bare-repository").stdout.strip()
    assert is_bare == "false", (
        "the gate turned the pushed-from repository into a bare repo; "
        "every worktree of it is now broken\n" + res.stdout + res.stderr
    )
    assert (scratch / ".git").is_dir(), (
        "the gate's own `git init` silently did nothing instead of creating "
        "its repository - the git environment is still leaking"
    )


def test_gate_runs_with_a_clean_git_environment(tmp_path):
    """Regression: no git environment reaches the spawned checks.

    Blanket cover for every other way a leaked git environment corrupts a
    check: `git ls-files` reading a foreign index, a test's repository
    resolving to the wrong place, and so on.
    """
    dump = tmp_path / "gate-env.txt"
    gate = f"#!/bin/bash\nenv > {shlex.quote(str(dump))}\nexit 0\n"

    _repo, res = _push_from_linked_worktree(tmp_path, gate)

    assert res.returncode == 0, res.stdout + res.stderr
    assert dump.exists(), "the gate never ran: " + res.stdout + res.stderr
    leaked = sorted(
        line.split("=", 1)[0]
        for line in dump.read_text().splitlines()
        if line.split("=", 1)[0] in GIT_ENV_VARS
    )
    assert leaked == [], f"gate inherited git environment: {leaked}"


def test_gate_restores_mutated_shared_config_and_fails(tmp_path):
    """The canary: a config write during the gate must be loud, not fatal later.

    The environment scrub above closes the known leak, but the failure mode
    (a check writing core.bare/user.* into the real repo's shared config,
    quietly killing every worktree) is bad enough to deserve a runtime
    tripwire too: the hook snapshots the shared config before the gate and,
    if anything mutated it, restores the snapshot and fails the push.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(
        repo,
        "#!/bin/bash\ngit config user.email gate-mutant@example.invalid\nexit 0\n",
    )
    _commit_all(repo, "gate that mutates the shared config")
    config = repo / ".git" / "config"
    before = config.read_text()

    res = _run_hook(repo)

    assert res.returncode != 0, (
        "a gate run that mutates the shared config must fail the push:\n"
        + res.stdout
        + res.stderr
    )
    assert "mutated" in (res.stdout + res.stderr)
    assert config.read_text() == before, "shared config must be restored byte-for-byte"
