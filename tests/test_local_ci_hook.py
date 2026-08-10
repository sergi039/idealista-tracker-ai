"""The pre-push hook must judge the pushed SHA, not the working tree.

Five review rounds on PR #78 (issue #74) all reduced to the same gap:
any shortcut that trusts the working tree ("it looks clean", "the suite
already ran") lets a broken committed blob through, or rejects a green
commit over local-only files. These tests run the real .githooks/pre-push
against a toy repository whose committed tools/ci/local_ci.sh is a stub
with a known exit code, then desynchronize the working tree from the
commit and assert the verdict follows the commit.

Two follow-up defects have their own tests further down: the gate must not
leak git's own environment into the checks it spawns (issue #74), and its
shared-config canary must judge keys rather than bytes, so that a parallel
session's `git push -u` neither aborts this push nor gets reverted by it
(issue #155).
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _mktemp_shim(tmp_path, failing_glob):
    """A PATH shim whose `mktemp` fails only for templates matching a glob.

    Lets a test starve one specific temp file the hook asks for while every
    other one still works, which is how the fail-closed paths are reached
    without breaking the rest of the run.
    """
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp, "mktemp must exist for this test to mean anything"
    shim_dir = tmp_path / "mktemp-shim"
    shim_dir.mkdir()
    shim = shim_dir / "mktemp"
    shim.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        f"        {failing_glob}) exit 1 ;;\n"
        "    esac\n"
        "done\n"
        f'exec {shlex.quote(real_mktemp)} "$@"\n'
    )
    shim.chmod(0o755)
    return shim_dir


def _config_value(repo, key):
    res = subprocess.run(
        ["git", "config", "--file", str(repo / ".git" / "config"), "--get", key],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return res.stdout.strip() if res.returncode == 0 else None


def _config_values(repo, key):
    res = subprocess.run(
        ["git", "config", "--file", str(repo / ".git" / "config"), "--get-all", key],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return res.stdout.splitlines() if res.returncode == 0 else []


def test_gate_restores_mutated_shared_config_and_fails(tmp_path):
    """The canary: a config write during the gate must be loud, not fatal later.

    The environment scrub above closes the known leak, but the failure mode
    (a check writing core.bare into the real repo's shared config, quietly
    killing every worktree) is bad enough to deserve a runtime tripwire too:
    the hook snapshots the shared config before the gate and, if a key that
    decides how the whole clone behaves changed, puts it back and fails the
    push. core.bare = true is the 2026-08-08 incident itself.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, "#!/bin/bash\ngit config core.bare true\nexit 0\n")
    _commit_all(repo, "gate that mutates the shared config")

    res = _run_hook(repo)

    assert res.returncode != 0, (
        "a gate run that mutates the shared config must fail the push:\n"
        + res.stdout
        + res.stderr
    )
    assert "core.bare" in (res.stdout + res.stderr), (
        "the report must name the key that changed:\n" + res.stdout + res.stderr
    )
    assert _config_value(repo, "core.bare") == "false", (
        "core.bare must be back at its pre-push value, or every worktree of "
        "this clone stays broken"
    )


def test_a_key_the_gate_cannot_be_proven_to_own_is_reported_but_not_reverted(tmp_path):
    """Reverting an unattributable key is the #155 damage in another costume.

    user.email is a per-session preference: agent harnesses set it, so the
    hook has no way to tell its own leak from a peer's legitimate write. The
    push is blocked and the key is named — but it is left exactly as found,
    because guessing wrong here is how the old canary destroyed a parallel
    session's work.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(
        repo,
        "#!/bin/bash\ngit config user.email gate-mutant@example.invalid\nexit 0\n",
    )
    _commit_all(repo, "gate that writes a key nobody can attribute")

    res = _run_hook(repo)

    assert res.returncode != 0, res.stdout + res.stderr
    assert "user.email" in (res.stdout + res.stderr), (
        "the report must name the key that changed:\n" + res.stdout + res.stderr
    )
    assert _config_value(repo, "user.email") == "gate-mutant@example.invalid", (
        "an unattributable key must be left as found, not guessed at"
    )


@pytest.mark.parametrize(
    ("seed", "gate_line", "label"),
    (
        pytest.param(
            "[gatetest]\n\tflag\n",
            'git config --file {config} gatetest.flag ""',
            "gatetest.flag",
            id="valueless-becomes-empty",
        ),
        pytest.param(
            "[gatetest]\n\tnote = a\\x1eb\n",
            "git config --file {config} gatetest.note \"$(printf 'a\\nb')\"",
            "gatetest.note",
            id="record-separator-becomes-newline",
        ),
    ),
)
def test_two_different_values_never_share_one_fingerprint(
    tmp_path, seed, gate_line, label
):
    """A value must have exactly one spelling in the key comparison.

    The comparison renders every entry as one line, and any rendering that
    maps two different configs onto the same text is a way for a real
    mutation to pass unseen — `core.bare` valueless (which reads as true)
    against `core.bare =`, or a value that already contains the separator
    used to fold newlines away.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write(seed.replace("\\x1e", "\x1e"))
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        + gate_line.format(config=shlex.quote(str(config)))
        + "\nexit 0\n",
    )
    _commit_all(repo, "gate that rewrites a value into a look-alike")

    res = _run_hook(repo)

    assert res.returncode != 0, (
        f"{label} changed and the canary did not notice:\n" + res.stdout + res.stderr
    )
    assert label in (res.stdout + res.stderr)


def test_reordering_a_multi_valued_clone_scope_key_is_caught_and_undone(tmp_path):
    """Order is content for include.path: A,B and B,A load different configs."""
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[include]\n\tpath = first.inc\n\tpath = second.inc\n")
    quoted = shlex.quote(str(config))
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {quoted} --unset-all include.path\n"
        f"git config --file {quoted} --add include.path second.inc\n"
        f"git config --file {quoted} --add include.path first.inc\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate that reorders a multi-valued key")

    res = _run_hook(repo)

    assert res.returncode != 0, (
        "reordering include.path changes what git loads and must be caught:\n"
        + res.stdout
        + res.stderr
    )
    assert _config_values(repo, "include.path") == ["first.inc", "second.inc"], (
        "the original order must be restored, not just the set of values"
    )


def test_a_branch_key_that_is_not_bookkeeping_is_still_caught(tmp_path):
    """Only the four keys a parallel session writes are ignored, not [branch].

    `git push -u` writes remote/merge, so those must not abort a push. The
    rest of the section is not bookkeeping: branch.<name>.pushRemote decides
    where future pushes go and nothing in a parallel workflow writes it, so
    ignoring the whole section would hand a gate leak a redirect.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} branch.main.pushRemote elsewhere\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate that redirects future pushes")

    res = _run_hook(repo)

    assert res.returncode != 0, (
        "branch.<name>.pushRemote is not bookkeeping and must abort:\n"
        + res.stdout
        + res.stderr
    )
    assert "pushremote" in (res.stdout + res.stderr).lower()


def test_losing_the_snapshot_fails_closed(tmp_path):
    """Without a snapshot there is no canary, and no canary means no push.

    The gate's whole job here is standing between a leaked GIT_DIR and a
    clone whose every worktree stops working. "Could not check" must not
    read as "nothing happened" — SKIP_LOCAL_CI=1 is the deliberate way past.
    """
    shim_dir = _mktemp_shim(tmp_path, "*/local-ci-gate-config.*")
    repo = _make_repo(tmp_path, committed_gate_exit=0)

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)

    assert res.returncode != 0, (
        "a canary that cannot run must not let the push through:\n"
        + res.stdout
        + res.stderr
    )
    assert "SKIP_LOCAL_CI" in (res.stdout + res.stderr), (
        "the message must name the deliberate way past it"
    )


@pytest.mark.parametrize(
    ("seed", "gate_body", "reason"),
    (
        pytest.param(
            "[extensions]\n\tgatetest\n",
            "git config extensions.gatetest true\n",
            "valueless entries cannot be re-added as valueless",
            id="valueless-cannot-be-restored",
        ),
        pytest.param(
            None,
            "git config core.bare true\ntouch {config}.lock\n",
            "the config lock is held, so the write cannot happen",
            id="config-locked-by-someone-else",
        ),
    ),
)
def test_a_revert_that_did_not_happen_is_never_reported_as_done(
    tmp_path, seed, gate_body, reason
):
    """The hook must not claim a restore it could not perform.

    A half-applied revert of core.* is worse than none, because the reader
    walks away believing the clone is intact. Whatever the cause — git cannot
    express a valueless entry through --add, or a concurrent writer holds the
    config lock — the report has to say so.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    if seed:
        with config.open("a") as handle:
            handle.write(seed)
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        + gate_body.format(config=shlex.quote(str(config)))
        + "exit 0\n",
    )
    _commit_all(repo, "gate whose damage cannot be reverted")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert "did NOT fully succeed" in output, (
        f"the hook claimed a revert it could not do ({reason}):\n" + output
    )
    assert "back at their pre-gate values" not in output


@pytest.mark.parametrize("tool", ("sort", "awk"))
def test_a_broken_comparison_never_reads_as_nothing_changed(tmp_path, tool):
    """A check that failed to run is not a check that found nothing.

    The key comparison is a shell pipeline, and a pipeline's exit status is
    its last stage: a failing `awk` or a short write leaves a truncated
    fingerprint, and two truncated fingerprints compare equal. That is a
    mutation passing because the check broke, which is the worst outcome
    this hook has.
    """
    shim_dir = tmp_path / f"{tool}-shim"
    shim_dir.mkdir()
    shim = shim_dir / tool
    shim.write_text("#!/bin/bash\nexit 1\n")
    shim.chmod(0o755)

    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, "#!/bin/bash\ngit config core.bare true\nexit 0\n")
    _commit_all(repo, "gate that mutates the config while the comparison is broken")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)

    assert res.returncode != 0, (
        f"a broken {tool} must not let a config mutation through:\n"
        + res.stdout
        + res.stderr
    )
    assert "FATAL" in (res.stdout + res.stderr)


def _git_shim(tmp_path, name, match_cases):
    """A PATH shim for `git` that short-circuits calls matching a case pattern.

    `match_cases` is the body of a `case " $* " in ... esac`, so a test can
    make one specific git invocation fail while every other one runs for
    real. Failing a single call is how the windows inside the restore — a
    snapshot read that errors, a write that cannot take the lock — are
    reproduced without waiting for a real race.
    """
    real_git = shutil.which("git")
    assert real_git, "git must exist for this test to mean anything"
    shim_dir = tmp_path / name
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        '#!/bin/bash\ncase " $* " in\n'
        + match_cases
        + f'\nesac\nexec {shlex.quote(real_git)} "$@"\n'
    )
    shim.chmod(0o755)
    return shim_dir


def _seed_include_paths(repo, *values):
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[include]\n")
        for value in values:
            handle.write(f"\tpath = {value}\n")
    return config


def test_a_snapshot_read_error_never_deletes_a_key_that_existed(tmp_path):
    """ "Key absent" and "could not read the key" are not the same answer.

    git reports both through a non-zero exit, and only the first means the
    gate invented the key. Conflating them makes the repair delete an
    include.path that was there all along — the hook destroying more than the
    gate did.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = _seed_include_paths(repo, "base.inc")
    shim_dir = _git_shim(
        tmp_path,
        "git-shim-read",
        '    *" --get-all "*)\n'
        '        case " $* " in *local-ci-gate-config.*) exit 128 ;; esac\n'
        "        ;;",
    )
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} --replace-all include.path other.inc\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate mutating a key whose snapshot cannot be read")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_values(repo, "include.path"), (
        "the hook deleted include.path because it could not read the snapshot:\n"
        + output
    )


def test_a_failed_seed_never_leaves_a_half_repaired_list(tmp_path):
    """A restore that could not start must not continue anyway.

    The first snapshot value replaces whatever the gate wrote; the rest are
    appended. If that first write fails and the appends go ahead, the key
    ends up holding the gate's value *and* part of the old one — a state
    neither the gate nor the snapshot ever had.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = _seed_include_paths(repo, "base.inc", "b.inc")
    shim_dir = _git_shim(
        tmp_path, "git-shim-write", '    *" --replace-all "*) exit 1 ;;'
    )
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} --unset-all include.path\n"
        f"git config --file {shlex.quote(str(config))} --add include.path other.inc\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate mutating a multi-valued key that cannot be reseeded")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_values(repo, "include.path") == ["other.inc"], (
        "the failed repair appended the rest of the snapshot onto the gate's "
        "value, inventing a third state:\n" + output
    )
    assert "did NOT fully succeed" in output


def test_a_valueless_check_that_failed_is_not_read_as_no(tmp_path):
    """An answer nobody could compute must not license the risky repair."""
    real_awk = shutil.which("awk")
    assert real_awk, "awk must exist for this test to mean anything"
    shim_dir = tmp_path / "awk-shim-valueless"
    shim_dir.mkdir()
    shim = shim_dir / "awk"
    shim.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        "        *'$3 == \"-\"'*) exit 1 ;;\n"
        "    esac\n"
        "done\n"
        f'exec {shlex.quote(real_awk)} "$@"\n'
    )
    shim.chmod(0o755)

    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[extensions]\n\tgatetest\n")
    _write_gate_script(
        repo, "#!/bin/bash\ngit config extensions.gatetest true\nexit 0\n"
    )
    _commit_all(repo, "gate overwriting a valueless entry the check cannot classify")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_value(repo, "extensions.gatetest") == "true", (
        "a check that failed was read as 'not valueless', and the repair "
        "flipped the entry to an empty value (false):\n" + output
    )


def test_a_backslash_in_a_subsection_name_still_matches_its_own_key(tmp_path):
    """`awk -v` decodes escapes, so a key with a backslash arrives mangled.

    `[includeIf "gitdir:C:\\new"]` is a perfectly ordinary Windows-path
    condition, and passing that key into awk with -v turns the `\\n` into a
    newline. Every per-key check then matches nothing: the entry looks not
    valueless, so the repair writes it back as an empty value — flipping true
    to false — and the verification, mangled the same way, certifies it.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    key = "includeif.gitdir:C:\\new.path"
    with config.open("a") as handle:
        handle.write('[includeIf "gitdir:C:\\\\new"]\n\tpath\n')
    assert key in _git(repo, "config", "--file", str(config), "--list").stdout, (
        "git no longer produces a backslash-bearing key, so this test is moot"
    )

    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} {shlex.quote(key)} changed\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate overwriting a valueless key whose name has a backslash")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_value(repo, key) == "changed", (
        "the per-key checks did not recognise their own key, and the repair "
        "flipped a valueless entry to an empty value:\n" + output
    )


TAB_KEY = "includeif.gitdir:a\tb.path"


def _seed_tab_bearing_key(repo, value):
    """Give the toy repo a clone-scope key with a real tab in its name.

    git accepts a tab inside a subsection name, and reports it verbatim, so
    the key really is `includeif.gitdir:a<TAB>b.path` — one key, not two.
    """
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write(f'[includeIf "gitdir:a\tb"]\n\tpath = {value}\n')
    assert TAB_KEY in _git(repo, "config", "--file", str(config), "--list").stdout, (
        "git no longer produces a tab-bearing key, so these tests are moot"
    )
    return config


def test_a_tab_bearing_key_does_not_veto_a_parallel_branch_write(tmp_path):
    """Owning such a key must not cost you every push (#155 follow-up).

    The fingerprint writes one `key<TAB>value` line per entry, so a raw tab
    in the key used to split the record in the wrong place. The first fix
    refused to judge the file at all, which meant a clone owning one legal
    key had every push aborted the moment anything — a parallel
    `git push -u` included — touched its config. Encoding the key removes
    the ambiguity instead of the push.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = _seed_tab_bearing_key(repo, "x.inc")
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} branch.other.remote origin\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate racing a branch write, with a tab-bearing key present")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode == 0, (
        "a legal tab-bearing key turned a benign branch write into an "
        "aborted push:\n" + output
    )
    assert "FATAL" not in output


def test_a_tab_bearing_key_is_repaired_under_its_own_name(tmp_path):
    """The repair must name the key git named, not a prefix of it.

    Split on the raw tab, `includeif.gitdir:a<TAB>b.path` reads as the key
    `includeif.gitdir:a` — which does not exist. The hook would then report
    that name and write *it* back, inventing a key while leaving the mutated
    one exactly as the gate left it.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = _seed_tab_bearing_key(repo, "x.inc")
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} "
        f"--replace-all {shlex.quote(TAB_KEY)} poison.inc\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate mutating a tab-bearing clone-scope key")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_value(repo, TAB_KEY) == "x.inc", (
        "the tab-bearing key was not put back at its pre-gate value:\n" + output
    )
    assert _config_value(repo, "includeif.gitdir:a") is None, (
        "the hook invented a key from the part of the name before the tab:\n" + output
    )


def test_a_key_added_to_an_empty_config_is_still_caught(tmp_path):
    """An empty "before" side must not swallow everything the gate added.

    git works fine with an empty .git/config, and the two-file awk idiom
    `FNR == NR` silently treats every row of the second file as a row of the
    first when the first is empty — so an added core.bare would come out as
    no change at all.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, "#!/bin/bash\ngit config core.bare true\nexit 0\n")
    _commit_all(repo, "gate that adds a key to an empty config")
    (repo / ".git" / "config").write_text("")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert "core.bare" in output, (
        "the added key must be named, not lost in an ordering complaint:\n" + output
    )
    assert _config_value(repo, "core.bare") is None, (
        "core.bare was absent before the gate, so the repair is to remove it"
    )


def test_a_verification_that_could_not_run_never_certifies_a_restore(tmp_path):
    """cmp on two files that failed to be produced is not a verified repair."""
    real_awk = shutil.which("awk")
    assert real_awk, "awk must exist for this test to mean anything"
    shim_dir = tmp_path / "awk-shim"
    shim_dir.mkdir()
    shim = shim_dir / "awk"
    shim.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        "        *'ENVIRON[\"CANARY_KEY\"]'*) exit 1 ;;\n"
        "    esac\n"
        "done\n"
        f'exec {shlex.quote(real_awk)} "$@"\n'
    )
    shim.chmod(0o755)

    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, "#!/bin/bash\ngit config core.bare true\nexit 0\n")
    _commit_all(repo, "gate mutating the config while per-key checks are broken")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert "back at their pre-gate values" not in output, (
        "the hook certified a repair it could not verify:\n" + output
    )


def test_a_restore_that_cannot_write_never_leaves_the_key_deleted(tmp_path):
    """A failed repair must not be worse than the damage it was repairing.

    Clearing the key first and writing the old values second means a write
    that cannot take the config lock leaves the key *gone* — the hook having
    destroyed what the gate merely changed.
    """
    real_git = shutil.which("git")
    assert real_git, "git must exist for this test to mean anything"
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[include]\n\tpath = base.inc\n")

    # Slam the config lock shut the instant anything clears a key, which is
    # the window a real concurrent writer would occupy.
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        "status=0\n"
        f'{shlex.quote(real_git)} "$@" || status=$?\n'
        'case " $* " in\n'
        f'    *" --unset-all "*) touch {shlex.quote(str(config))}.lock ;;\n'
        "esac\n"
        "exit $status\n"
    )
    shim.chmod(0o755)

    quoted = shlex.quote(str(config))
    _write_gate_script(
        repo,
        f"#!/bin/bash\ngit config --file {quoted} --replace-all include.path other.inc\nexit 0\n",
    )
    _commit_all(repo, "gate that mutates a multi-valued clone-scope key")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_values(repo, "include.path") == ["base.inc"], (
        "include.path must be back at its pre-gate value, and above all must "
        "never be left deleted by a repair that could not write:\n" + output
    )


def test_moving_an_include_block_is_caught_even_though_no_entry_changed(tmp_path):
    """git reads a config top to bottom, so where an [include] sits is content.

    `include.path` pulls its target in at the point it appears: an [include]
    moved below [core] overrides what [core] said. Every individual entry
    keeps its value, so a comparison that only looks at entries — sorted, or
    keyed — sees nothing at all and lets the push through.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[include]\n\tpath = poison.inc\n[gatetest]\n\tflag = one\n")
    quoted = shlex.quote(str(config))
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {quoted} --unset include.path\n"
        f"git config --file {quoted} --add include.path poison.inc\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate that moves the include below everything else")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert _config_value(repo, "include.path") == "poison.inc", (
        "the test must reorder the file, not change any value"
    )
    assert res.returncode != 0, (
        "the include moved below [core] and the canary saw nothing:\n" + output
    )
    assert "reorder" in output, (
        "the reader has to be told it is an ordering change:\n" + output
    )


def test_a_valueless_entry_is_left_alone_rather_than_flipped(tmp_path):
    """Putting a valueless key "back" would change its meaning, so don't.

    `[extensions] worktreeConfig` with no `=` reads as true, and git cannot
    write that form: `--add key ""` spells it `worktreeConfig =`, which reads
    as **false**. Restoring would therefore silently turn the extension off —
    the hook doing its own damage while reporting a repair.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    with config.open("a") as handle:
        handle.write("[extensions]\n\tgatetest\n")
    _write_gate_script(
        repo, "#!/bin/bash\ngit config extensions.gatetest true\nexit 0\n"
    )
    _commit_all(repo, "gate that overwrites a valueless entry")

    res = _run_hook(repo)
    output = res.stdout + res.stderr

    assert res.returncode != 0, output
    assert _config_value(repo, "extensions.gatetest") == "true", (
        "the hook rewrote a valueless entry into an empty one, which reads as "
        "false - it must leave the key alone instead:\n" + output
    )
    assert "valueless" in output, (
        "the reader has to be told why this one was not repaired:\n" + output
    )


def test_concurrent_branch_write_neither_fails_the_push_nor_is_reverted(tmp_path):
    """Issue #155: another session's `git push -u` is not this gate's leak.

    Several agent sessions share this clone, so [branch "..."] sections
    appear and vanish in the shared config while a gate run is in flight -
    `git push -u`, `git checkout -b --track`, `git worktree add -b`. A
    whole-file canary called that a gate mutation, aborted a green push, and
    then restored its snapshot over the other session's brand-new upstream.
    Neither may happen: the gate creates no branches, so branch bookkeeping
    is somebody else's and none of its business.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"git config --file {shlex.quote(str(config))} branch.other.remote origin\n"
        f"git config --file {shlex.quote(str(config))} "
        "branch.other.merge refs/heads/other\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate racing a concurrent branch write")

    res = _run_hook(repo)

    assert res.returncode == 0, (
        "a concurrent branch-upstream write must not abort a green push:\n"
        + res.stdout
        + res.stderr
    )
    assert "FATAL" not in (res.stdout + res.stderr)
    assert _config_value(repo, "branch.other.remote") == "origin", (
        "the gate reverted a branch section it did not write:\n"
        + res.stdout
        + res.stderr
    )
    assert _config_value(repo, "branch.other.merge") == "refs/heads/other"


def test_being_unable_to_work_out_which_keys_changed_fails_closed(tmp_path):
    """ "Could not check" is not a reason to let a config change through.

    Working out which keys changed needs a scratch directory. If that cannot
    be created the hook knows only that the shared config changed during its
    own run - which is precisely the state it exists to refuse.
    """
    shim_dir = _mktemp_shim(tmp_path, "*/local-ci-gate-diff.*")
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    _write_gate_script(repo, "#!/bin/bash\ngit config core.bare true\nexit 0\n")
    _commit_all(repo, "gate that mutates the config while the scratch dir fails")

    env = _clean_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
    res = _run_hook(repo, env=env)

    assert res.returncode != 0, (
        "an unjudgeable config change must abort the push:\n" + res.stdout + res.stderr
    )
    assert "FATAL" in (res.stdout + res.stderr)


def test_a_config_git_can_no_longer_parse_fails_the_push(tmp_path):
    """An unreadable config must abort, not read as "nothing changed".

    The key comparison asks git to list the config twice. If a failed listing
    were treated as an empty one, the two empty results would match and the
    canary would wave through exactly the corruption it exists to catch — the
    cheapest way to nullify this gate.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        f"printf '[core\\nnot a config line\\n' >> {shlex.quote(str(config))}\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate that corrupts the shared config")

    res = _run_hook(repo)

    assert res.returncode != 0, (
        "a config git can no longer parse must abort the push:\n"
        + res.stdout
        + res.stderr
    )
    assert "FATAL" in (res.stdout + res.stderr)


def test_reverting_a_gate_mutation_spares_a_concurrent_branch_write(tmp_path):
    """The restore is key-scoped, so it cannot damage a parallel session.

    Same run, both writers: a real leak (core.bare) and another session
    setting a branch upstream. The push must fail and the leaked key must go
    back, while the branch section stays exactly as the other session left
    it - the old wholesale `cp snapshot config` silently undid it, leaving
    that session with a branch that had lost its upstream and no clue why.
    """
    repo = _make_repo(tmp_path, committed_gate_exit=0)
    config = repo / ".git" / "config"
    _write_gate_script(
        repo,
        "#!/bin/bash\n"
        "git config core.bare true\n"
        f"git config --file {shlex.quote(str(config))} branch.other.remote origin\n"
        "exit 0\n",
    )
    _commit_all(repo, "gate that leaks while another session sets an upstream")

    res = _run_hook(repo)

    assert res.returncode != 0, res.stdout + res.stderr
    assert _config_value(repo, "core.bare") == "false", (
        "the leaked key must be back at its pre-push value"
    )
    assert _config_value(repo, "branch.other.remote") == "origin", (
        "restoring the leaked key must not undo the other session's write:\n"
        + res.stdout
        + res.stderr
    )
