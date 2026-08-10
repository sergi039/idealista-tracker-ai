"""merge_bot refuses a diff the reviewer cannot survive, once (issue #182).

`rx` does not degrade on a large diff, it dies: `cx` pipes the whole codex
transcript to stderr and the coordinator kills the process group at its 256 KB
cap. That surfaces as `UNAVAILABLE`, which `merge_bot.sh` correctly refuses to
treat as a pass — and then re-requests on the next tick, for ever. PR #177
measured 94 621 bytes and failed that way twice.

So the size is measured before the attempt is spent. These pin the three things
that make the guard worth having: an oversized diff never reaches `rx`, the
refusal is said out loud on the PR and journalled so the next tick is silent,
and a diff under the ceiling is left entirely alone.

Real git repository, real `git diff` — the byte count under test is git's own,
not a number a stub agreed to report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MERGE_BOT = REPO_ROOT / "tools" / "autopilot" / "merge_bot.sh"


@pytest.fixture(autouse=True)
def _needs_tooling():
    for tool in ("git", "jq", "python3"):
        if shutil.which(tool) is None:
            pytest.skip(f"merge_bot.sh needs {tool}")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip())
    path.chmod(0o755)


def _build_repo(tmp_path: Path, payload_lines: int) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.md").write_text("# Seed\n")
    base = _commit(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)

    (repo / "services").mkdir()
    (repo / "services" / "big.py").write_text(
        "".join(
            f"CONSTANT_{n} = 'x' * 40  # padding line {n}\n"
            for n in range(payload_lines)
        )
    )
    head = _commit(repo, "pull request head")
    _git(repo, "update-ref", "refs/autopilot/pr-117", head)
    return repo, base, head


def _make_stubs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "git",
        f"""
        #!/bin/bash
        if [ "$1" = "fetch" ]; then exit 0; fi
        exec {shutil.which("git")} "$@"
        """,
    )
    _write_executable(
        bin_dir / "gh",
        """
        #!/bin/bash
        set -eu
        case "$1:${2:-}" in
            pr:list)
                printf '[{"number":117,"title":"big","isDraft":false,"mergeable":"MERGEABLE","headRefOid":"%s"}]\\n' "$TEST_HEAD_SHA"
                ;;
            pr:checks)  printf '[{"name":"pytest","state":"SUCCESS"}]\\n' ;;
            pr:merge)   : >"$TEST_MERGE_MARKER" ;;
            pr:comment) printf '%s\\n' "$*" >>"$TEST_COMMENT_LOG" ;;
            api:*)
                case "$*" in
                    *protection/enforce_admins*) printf 'true\\n' ;;
                    *protection/required_status_checks*)
                        case "$*" in
                            *.strict*) printf 'true\\n' ;;
                            *) printf 'pytest\\n' ;;
                        esac
                        ;;
                    *) printf '%s\\n' "$TEST_HEAD_SHA" ;;
                esac
                ;;
            *) exit 93 ;;
        esac
        """,
    )
    _write_executable(
        bin_dir / "rx",
        """
        #!/bin/bash
        printf '%s\\0' "$@" >"$TEST_RX_ARGS"
        exit 0
        """,
    )


def _run(tmp_path: Path, repo: Path, head: str, *, ceiling: str, journal: str = ""):
    journal_path = tmp_path / "reviews.tsv"
    journal_path.write_text(journal)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{env['PATH']}",
            "AUTOPILOT_REPO_DIR": str(repo),
            "AUTOPILOT_REPO_SLUG": "test/example",
            "AUTOPILOT_MERGE_LOCK_DIR": str(tmp_path / "merge.lock"),
            "AUTOPILOT_MERGE_LOG": str(tmp_path / "merge.log"),
            "AUTOPILOT_REVIEW_JOURNAL": str(journal_path),
            "AUTOPILOT_REVIEW_DIFF_MAX_BYTES": ceiling,
            "TEST_HEAD_SHA": head,
            "TEST_RX_ARGS": str(tmp_path / "rx-args"),
            "TEST_MERGE_MARKER": str(tmp_path / "merge-called"),
            "TEST_COMMENT_LOG": str(tmp_path / "comments.log"),
        }
    )
    result = subprocess.run(
        ["bash", str(MERGE_BOT)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=env,
    )
    return result, journal_path


def test_an_oversized_diff_never_reaches_the_reviewer(tmp_path):
    repo, base, head = _build_repo(tmp_path, payload_lines=400)
    _make_stubs(tmp_path)

    result, journal = _run(tmp_path, repo, head, ceiling="2000")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "rx-args").exists(), (
        "spent a reviewer attempt on a diff that cannot come back with a verdict"
    )
    assert not (tmp_path / "merge-called").exists()
    assert "over the 2000 the reviewer survives" in result.stdout

    # Journalled, so the next tick is silent instead of re-deciding out loud.
    assert f"{base}..{head}\tOVERSIZED" in journal.read_text()

    # And said where the work is, not only in a local log.
    comments = (tmp_path / "comments.log").read_text()
    assert "the diff is too large for the reviewer" in comments
    assert "issue #182" in comments


def test_a_diff_under_the_ceiling_is_reviewed_and_merged(tmp_path):
    repo, _base, head = _build_repo(tmp_path, payload_lines=5)
    _make_stubs(tmp_path)

    result, _journal = _run(tmp_path, repo, head, ceiling="60000")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "rx-args").exists(), "the size guard swallowed a normal PR"
    assert (tmp_path / "merge-called").exists()
    assert "MERGED #117" in result.stdout


def test_a_cached_oversized_verdict_is_not_re_decided(tmp_path):
    """The point of journalling it: one comment, not one per tick."""
    repo, base, head = _build_repo(tmp_path, payload_lines=400)
    _make_stubs(tmp_path)

    result, _journal = _run(
        tmp_path,
        repo,
        head,
        ceiling="2000",
        journal=f"{base}..{head}\tOVERSIZED\t2026-08-10T00:00:00\tpr-117\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "cached OVERSIZED" in result.stdout
    assert not (tmp_path / "comments.log").exists(), (
        "commented again on a known refusal"
    )
    assert not (tmp_path / "merge-called").exists()


@pytest.mark.parametrize("ceiling", ["0", "abc", "-1", "60_000"])
def test_a_nonsense_ceiling_refuses_rather_than_guesses(tmp_path, ceiling):
    """A cap that can be set to nonsense is not a cap.

    It refuses for every PR, not only the large one, so the typo shows up on the
    next tick rather than on the next big change. `0` matters most: it would
    otherwise compare as "no diff is ever too large".
    """
    repo, _base, head = _build_repo(tmp_path, payload_lines=5)
    _make_stubs(tmp_path)

    result, _journal = _run(tmp_path, repo, head, ceiling=ceiling)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "rx-args").exists()
    assert not (tmp_path / "merge-called").exists()
    assert "must be a positive integer" in result.stdout


def test_an_unset_ceiling_uses_the_documented_default(tmp_path):
    """`${VAR:-60000}` treats empty as unset; that is the default, not nonsense."""
    repo, _base, head = _build_repo(tmp_path, payload_lines=5)
    _make_stubs(tmp_path)

    result, _journal = _run(tmp_path, repo, head, ceiling="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "rx-args").exists()
    assert (tmp_path / "merge-called").exists()
