"""The pre-push hook must judge the pushed SHA, not the working tree.

Five review rounds on PR #78 (issue #74) all reduced to the same gap:
any shortcut that trusts the working tree ("it looks clean", "the suite
already ran") lets a broken committed blob through, or rejects a green
commit over local-only files. These tests run the real .githooks/pre-push
against a toy repository whose committed tools/ci/local_ci.sh is a stub
with a known exit code, then desynchronize the working tree from the
commit and assert the verdict follows the commit.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".githooks" / "pre-push"
RUNNER = REPO_ROOT / "tools" / "ci" / "run_gate_on_sha.sh"
ZERO_SHA = "0" * 40


def _clean_env(**overrides):
    """Environment without inherited GIT_* variables.

    This suite itself runs inside the snapshot gate (local_ci.sh under a
    pre-push), where git exports GIT_DIR pointing at the outer repository.
    Every subprocess the tests spawn must not inherit that, or toy-repo git
    calls land in the wrong repo. Tests inject dirty GIT_* back deliberately
    via overrides when the point is to prove the runner strips them.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(overrides)
    return env


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _write_gate(repo, exit_code):
    gate = repo / "tools" / "ci" / "local_ci.sh"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    gate.chmod(0o755)
    return gate


def _make_repo(tmp_path, committed_gate_exit):
    repo = tmp_path / "toy"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hook-test@example.invalid")
    _git(repo, "config", "user.name", "hook test")
    _write_gate(repo, committed_gate_exit)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _run_hook(repo, stdin_line=None):
    if stdin_line is None:
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        stdin_line = f"refs/heads/main {sha} refs/heads/main {ZERO_SHA}\n"
    return subprocess.run(
        ["bash", str(HOOK)],
        input=stdin_line,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
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


def test_runner_verdict_follows_the_committed_gate(tmp_path):
    """merge_bot calls the runner directly: red commit fails, green passes."""
    red = _make_repo(tmp_path / "red", committed_gate_exit=1)
    sha = _git(red, "rev-parse", "HEAD").stdout.strip()
    res = subprocess.run(
        ["bash", str(RUNNER), sha],
        cwd=red,
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
    )
    assert res.returncode != 0, res.stdout + res.stderr

    green = _make_repo(tmp_path / "green", committed_gate_exit=0)
    sha = _git(green, "rev-parse", "HEAD").stdout.strip()
    res = subprocess.run(
        ["bash", str(RUNNER), sha],
        cwd=green,
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_runner_strips_inherited_git_env(tmp_path):
    """git exports GIT_DIR to hooks; inherited into the snapshot it would point
    every git call at the OUTER repository. A green toy commit must stay green
    even when the environment says the repo is somewhere else entirely."""
    repo = _make_repo(tmp_path / "toy-envtest", committed_gate_exit=0)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # what a real hook invocation carries - injected deliberately
    env = _clean_env(GIT_DIR=str(REPO_ROOT / ".git"))
    res = subprocess.run(
        ["bash", str(RUNNER), sha],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, (
        "runner must judge the toy repo, not the GIT_DIR one:\n"
        + res.stdout
        + res.stderr
    )


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
