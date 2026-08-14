"""Regression coverage for merge_bot reviewer-attempt handling."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MERGE_BOT = REPO_ROOT / "tools" / "autopilot" / "merge_bot.sh"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
MERGE_SHA = "3" * 40


def _write_executable(path: Path, body: str) -> None:
    # `.lstrip()` is load-bearing, and its absence is a segfault on macOS
    # (2026-08-14). These bodies open with a newline, so dedent alone leaves a
    # blank first line and the `#!/bin/bash` below it is not a shebang. execve
    # then fails ENOEXEC and bash re-execs the stub with *itself* — which under
    # pytest is Homebrew bash 5.3.15, linked against gettext's libintl and
    # CoreFoundation. Its startup locale init (set_default_lang ->
    # libintl_setlocale -> CFLocaleCopyPreferredLanguages) reaches CFPreferences,
    # which is not fork-safe and intermittently takes SIGSEGV; merge_bot.sh then
    # correctly aborts the pass and the test reports the non-zero exit. With the
    # shebang at byte 0 the kernel runs /bin/bash (Apple 3.2), which links
    # neither library and never enters that path. The sibling merge_bot test
    # files already lstrip for this reason.
    text = textwrap.dedent(body).lstrip()
    assert text.startswith("#!"), (
        "stub needs a shebang on line 1, or bash re-execs itself"
    )
    path.write_text(text)
    path.chmod(0o755)


def _run_merge_bot(tmp_path: Path, *args: str, journal: str = ""):
    if shutil.which("jq") is None:
        pytest.skip("merge_bot.sh needs jq")
    if shutil.which("python3") is None:
        pytest.skip("merge_bot.sh needs python3 for locking")

    repo = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    repo.mkdir()
    bin_dir.mkdir()

    rx_marker = tmp_path / "rx-called"
    merge_marker = tmp_path / "merge-called"
    review_journal = tmp_path / "reviews.tsv"
    review_journal.write_text(journal)

    _write_executable(
        bin_dir / "git",
        """
        #!/bin/bash
        set -eu
        case "$1" in
            fetch) exit 0 ;;
            rev-parse)
                case "${2:-}" in
                    origin/main) printf '%s\n' "$TEST_BASE_SHA" ;;
                    refs/autopilot/*) printf '%s\n' "$TEST_HEAD_SHA" ;;
                    *) exit 91 ;;
                esac
                ;;
            merge-base) exit 0 ;;
            # merge_bot measures the diff before spending a reviewer attempt on
            # it (issue #182). This stub answers with something small, so these
            # tests stay about the attempt bookkeeping rather than about size.
            diff) printf 'diff --git a/x b/x\n+one line\n' ;;
            ls-remote)
                printf '%s\trefs/heads/main\n' "$TEST_BASE_SHA"
                ;;
            *) exit 92 ;;
        esac
        """,
    )
    _write_executable(
        bin_dir / "gh",
        """
        #!/bin/bash
        set -eu
        case "$1:${2:-}" in
            pr:list)
                printf '[{"number":117,"title":"Dry run","isDraft":false,"mergeable":"MERGEABLE","headRefOid":"%s"}]\n' "$TEST_HEAD_SHA"
                ;;
            pr:checks)
                printf '[{"name":"pytest","state":"SUCCESS"},{"name":"no-source-bundles","state":"SUCCESS"}]\n'
                ;;
            pr:merge)
                : >"$TEST_MERGE_MARKER"
                ;;
            pr:view)
                printf '%s\n' "$TEST_MERGE_SHA"
                ;;
            api:*)
                # Branch protection is what the bot trusts instead of
                # re-implementing the rules, so the stub has to answer as a
                # correctly protected branch: strict on, binding admins, with
                # the same required checks `pr:checks` reports green above.
                # Anything else still gets the base SHA, as before.
                case "$*" in
                    *protection/enforce_admins*)
                        printf 'true\n'
                        ;;
                    *protection/required_status_checks*)
                        case "$*" in
                            *.strict*) printf 'true\n' ;;
                            *) printf 'pytest\nno-source-bundles\n' ;;
                        esac
                        ;;
                    *)
                        printf '%s\n' "$TEST_BASE_SHA"
                        ;;
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
        : >"$TEST_RX_MARKER"
        exit 0
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AUTOPILOT_REPO_DIR": str(repo),
            "AUTOPILOT_REPO_SLUG": "test/example",
            "AUTOPILOT_MERGE_LOCK_DIR": str(tmp_path / "merge.lock"),
            "AUTOPILOT_MERGE_LOG": str(tmp_path / "merge.log"),
            "AUTOPILOT_REVIEW_JOURNAL": str(review_journal),
            "TEST_BASE_SHA": BASE_SHA,
            "TEST_HEAD_SHA": HEAD_SHA,
            "TEST_MERGE_SHA": MERGE_SHA,
            "TEST_RX_MARKER": str(rx_marker),
            "TEST_MERGE_MARKER": str(merge_marker),
        }
    )
    result = subprocess.run(
        ["bash", str(MERGE_BOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
        env=env,
    )
    return result, rx_marker, merge_marker


def test_dry_run_does_not_spend_a_reviewer_attempt(tmp_path):
    result, rx_marker, merge_marker = _run_merge_bot(tmp_path, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not rx_marker.exists(), "--dry-run invoked rx and spent a reviewer attempt"
    assert not merge_marker.exists(), "--dry-run invoked gh pr merge"
    assert f"would request review of {BASE_SHA}..{HEAD_SHA}" in result.stdout
    assert "no cached verdict, so would not merge" in result.stdout


def test_dry_run_reports_cached_pass_as_would_merge(tmp_path):
    cached_pass = f"{BASE_SHA}..{HEAD_SHA}\tPASS\t2026-08-09T00:00:00\tpr-117\n"
    result, rx_marker, merge_marker = _run_merge_bot(
        tmp_path, "--dry-run", journal=cached_pass
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not rx_marker.exists(), "a cached dry-run verdict must not invoke rx"
    assert not merge_marker.exists(), "--dry-run invoked gh pr merge"
    assert "cached PASS" in result.stdout
    assert "WOULD MERGE #117 (dry run)" in result.stdout


def test_normal_run_still_reviews_and_merges(tmp_path):
    result, rx_marker, merge_marker = _run_merge_bot(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert rx_marker.exists(), "normal mode did not request an uncached review"
    assert merge_marker.exists(), "normal mode did not merge after review PASS"
    assert "review PASS" in result.stdout
    assert "MERGED #117" in result.stdout
