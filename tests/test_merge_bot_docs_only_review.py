"""merge_bot.sh must send a documentation-only PR a prompt it can pass (#154).

The unit tests next door cover what `docs_review_evidence.py` resolves. This
covers the part that decides a merge: that `merge_bot.sh` picks the relaxed
prompt for a documentation-only diff, that the base excerpt actually reaches
`rx`, and - the half that matters more - that a diff touching code still gets
the strict prompt.

The repository here is real: real commits, real refs, real `git diff`. Only the
three things that would reach the network or the outside world are stubbed
(`git fetch`, `gh`, `rx`), so the branch under test is chosen by git's own
answer about what changed rather than by a stub agreeing with the assertion.

What these cannot prove, stated plainly so a green run is not over-read: the
`rx` stub records the prompt and exits 0, so nothing here shows what a reviewer
*decides* when given that prompt. It cannot - the verdict is a model's, not a
function's. These pin the half that is deterministic, which is the half this
change controls: which prompt is sent, and what evidence travels with it. The
other half was checked against the real PR that produced #154 (`b01c3ac`),
where the block resolves twelve base windows including
`services/enrichment_service.py:1056`, the `remark` refusal the issue names as
the missing proof.
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

MARKER = "MARKER_REFUSAL"
SERVICE_SOURCE = "\n".join(
    [f"# filler {n}" for n in range(1, 20)]
    + [
        "def refuse_remark_only_query(remark):",
        f"    # {MARKER} overpass-api.de refuses this outright",
        "    raise ValueError('refused upstream')",
    ]
)
MARKER_LINE = 21

STANDARD_PROMPT_MARK = "Judge correctness, security, error handling"
DOCS_PROMPT_MARK = "Every path in this diff is documentation"


@pytest.fixture(autouse=True)
def _needs_tooling():
    for tool in ("git", "jq", "python3"):
        if shutil.which(tool) is None:
            pytest.skip(f"merge_bot.sh needs {tool}")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
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


def _write(repo: Path, relative: str, body: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _write_executable(path: Path, body: str) -> None:
    # `.lstrip()` is load-bearing: these bodies open with a newline, so dedent
    # alone leaves a blank first line and the `#!/bin/bash` below it is not a
    # shebang. bash then re-execs the stub with *itself* — under pytest that is
    # Homebrew bash, which segfaults intermittently in its CoreFoundation
    # locale init. See tests/test_merge_bot_dry_run.py for the full account;
    # dropping the lstrip there cost a 6% flake rate on macOS.
    text = textwrap.dedent(body).lstrip()
    assert text.startswith("#!"), (
        "stub needs a shebang on line 1, or bash re-execs itself"
    )
    path.write_text(text)
    path.chmod(0o755)


def _build_repo(
    tmp_path: Path, head_change, *, seed: dict[str, str] | None = None
) -> tuple[Path, str, str]:
    """A repo with `refs/remotes/origin/main` at base and a PR head above it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "services/enrichment_service.py", SERVICE_SOURCE + "\n")
    _write(repo, "docs/STATE.md", "# State\n")
    for relative, body in (seed or {}).items():
        _write(repo, relative, body)
    base = _commit(repo, "base")
    # merge_bot resolves the base through `origin/main` and the head through the
    # ref it believes it fetched. Both exist for real, so `rev-parse` and
    # `merge-base` answer for themselves.
    _git(repo, "update-ref", "refs/remotes/origin/main", base)

    head_change(repo)
    head = _commit(repo, "pull request head")
    _git(repo, "update-ref", "refs/autopilot/pr-117", head)
    return repo, base, head


def _make_stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Everything except the network reaches real git, so `git diff` inside the
    # evidence script is git's own answer and not the test's.
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
                printf '[{"number":117,"title":"docs","isDraft":false,"mergeable":"MERGEABLE","headRefOid":"%s"}]\\n' "$TEST_HEAD_SHA"
                ;;
            pr:checks) printf '[{"name":"pytest","state":"SUCCESS"}]\\n' ;;
            pr:merge)  : >"$TEST_MERGE_MARKER" ;;
            api:*)
                case "$*" in
                    *protection/enforce_admins*) printf 'true\\n' ;;
                    *protection/required_status_checks*)
                        case "$*" in
                            *.strict*) printf 'true\\n' ;;
                            *) printf 'pytest\\n' ;;
                        esac
                        ;;
                    *) printf '%s\\n' "$TEST_BASE_SHA" ;;
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
    return bin_dir


def _env(
    tmp_path: Path,
    repo: Path,
    head: str,
    *,
    journal: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{env['PATH']}",
            "AUTOPILOT_REPO_DIR": str(repo),
            "AUTOPILOT_REPO_SLUG": "test/example",
            "AUTOPILOT_MERGE_LOCK_DIR": str(tmp_path / "merge.lock"),
            "AUTOPILOT_MERGE_LOG": str(tmp_path / "merge.log"),
            "AUTOPILOT_REVIEW_JOURNAL": str(tmp_path / journal),
            "TEST_HEAD_SHA": head,
            "TEST_BASE_SHA": head,
            "TEST_RX_ARGS": str(tmp_path / "rx-args"),
            "TEST_MERGE_MARKER": str(tmp_path / "merge-called"),
        }
    )
    env.update(extra or {})
    return env


def _rx_argv(tmp_path: Path) -> list[str]:
    rx_args = tmp_path / "rx-args"
    if not rx_args.exists():
        return []
    return [part for part in rx_args.read_text().split("\0") if part]


def _run_merge_bot(
    tmp_path: Path,
    repo: Path,
    head: str,
    *,
    script: Path = MERGE_BOT,
    env_extra: dict[str, str] | None = None,
):
    _make_stubs(tmp_path)
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=_env(tmp_path, repo, head, journal="reviews.tsv", extra=env_extra),
    )
    argv = _rx_argv(tmp_path)
    return result, argv, (argv[-1] if argv else "")


def _docs_change(repo: Path) -> None:
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nOverpass refuses remark-only queries\n"
        f"(`services/enrichment_service.py:{MARKER_LINE}`).\n",
    )


def test_documentation_only_pr_is_reviewed_against_the_base(tmp_path):
    repo, base, head = _build_repo(tmp_path, _docs_change)
    result, argv, prompt = _run_merge_bot(tmp_path, repo, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert DOCS_PROMPT_MARK in prompt, "a docs-only PR still got the strict prompt"
    assert "Do NOT return BLOCKER because the implementation" in prompt
    # The relaxed prompt moves where proof is expected; it does not stop
    # requiring it. Without this the gate would pass any claim the excerpts
    # merely fail to contradict.
    assert '"Unproven" still blocks' in prompt
    assert "the excerpts below do not support" in prompt
    assert MARKER in prompt, (
        "the base source behind the citation never reached the reviewer, so the "
        "claim is still unfalsifiable from the review request"
    )
    assert STANDARD_PROMPT_MARK not in prompt
    # The evidence range is untouched: the reviewer still audits base..head.
    assert f"{base}..refs/autopilot/pr-117" in argv
    assert "documentation-only diff" in result.stdout


def test_the_agent_instruction_rule_covers_removal_not_just_addition(tmp_path):
    """A guardrail is weakened by deleting it, and the rule has to say so."""

    def drop_a_guardrail(repo: Path) -> None:
        _write(repo, "CLAUDE.md", "# Rules\n\nRun the suite.\n")

    repo, _base, head = _build_repo(
        tmp_path,
        drop_a_guardrail,
        seed={"CLAUDE.md": "# Rules\n\nNever read or echo .env.\nRun the suite.\n"},
    )
    result, argv, prompt = _run_merge_bot(tmp_path, repo, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert DOCS_PROMPT_MARK in prompt
    assert "AGENT INSTRUCTIONS" in prompt, "the instruction file was not flagged"
    assert "added OR REMOVED" in prompt, (
        "the rule is written around added lines, so striking a guardrail out "
        "matches no objection at all"
    )


def test_a_pr_that_touches_code_still_gets_the_strict_prompt(tmp_path):
    def code_change(repo: Path) -> None:
        _write(repo, "docs/STATE.md", "# State\n\nDocumented.\n")
        _write(repo, "services/enrichment_service.py", SERVICE_SOURCE + "\n# new\n")

    repo, _base, head = _build_repo(tmp_path, code_change)
    result, argv, prompt = _run_merge_bot(tmp_path, repo, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt, (
        "a diff carrying executable code was reviewed under the relaxed rules"
    )
    assert DOCS_PROMPT_MARK not in prompt
    assert "documentation-only diff" not in result.stdout


def _checkout_with_helper(tmp_path: Path, body: str, name: str) -> Path:
    """A copy of tools/ whose evidence helper is replaced by *body*."""
    checkout = tmp_path / name
    shutil.copytree(REPO_ROOT / "tools", checkout / "tools")
    (checkout / "tools" / "autopilot" / "docs_review_evidence.py").write_text(body)
    return checkout / "tools" / "autopilot" / "merge_bot.sh"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("exits-nonzero", "raise SystemExit(2)\n"),
        # The dangerous one. An exit status is not evidence that a
        # classification happened: a helper truncated to this exits clean with
        # nothing to show, and trusting the status alone would hand the relaxed
        # prompt to every PR, app.py included.
        ("exits-zero-silently", "raise SystemExit(0)\n"),
        ("exits-zero-with-junk", "print('looks like evidence')\n"),
        ("claims-the-wrong-base", "print('DOCS-ONLY-EVIDENCE ' + '0' * 40)\n"),
    ],
)
def test_a_helper_that_does_not_prove_itself_falls_back_to_the_strict_prompt(
    tmp_path, name, body
):
    """Failing open onto the *relaxed* prompt would be the dangerous direction."""
    repo, _base, head = _build_repo(tmp_path, _docs_change)
    script = _checkout_with_helper(tmp_path, body, f"checkout-{name}")

    result, argv, prompt = _run_merge_bot(tmp_path, repo, head, script=script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt
    assert DOCS_PROMPT_MARK not in prompt


def test_a_helper_emitting_only_the_sentinel_falls_back_to_the_strict_prompt(tmp_path):
    """The subtle one: command substitution strips the trailing newline.

    A helper that prints the correct sentinel and nothing else leaves a
    one-line string, so a first-line test passes and the strip that follows has
    no newline to cut - and the sentinel itself would be sent as the evidence.
    """
    repo, base, head = _build_repo(tmp_path, _docs_change)
    script = _checkout_with_helper(
        tmp_path,
        f"print('DOCS-ONLY-EVIDENCE {base}')\n",
        "checkout-sentinel-only",
    )

    result, argv, prompt = _run_merge_bot(tmp_path, repo, head, script=script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt
    assert DOCS_PROMPT_MARK not in prompt


def test_a_helper_that_lies_about_a_code_diff_is_overruled_by_git(tmp_path):
    """The relaxed prompt needs two agreeing answers, not just the helper's.

    A helper that emits a complete, correctly-attested block for a diff that
    changes `services/enrichment_service.py` must not carry the day: merge_bot
    asks git the same question itself and refuses on the disagreement.
    """

    def code_change(repo: Path) -> None:
        _write(repo, "services/enrichment_service.py", SERVICE_SOURCE + "\n# new\n")

    repo, base, head = _build_repo(tmp_path, code_change)
    body = (
        f"print('DOCS-ONLY-EVIDENCE {base}')\n"
        f"print('Documentation-only diff against base {base}.')\n"
        "print('Every path this diff touches is documentation:')\n"
    )
    script = _checkout_with_helper(tmp_path, body, "checkout-lying-helper")

    result, argv, prompt = _run_merge_bot(tmp_path, repo, head, script=script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt
    assert DOCS_PROMPT_MARK not in prompt


# An *empty* value is not in this list: `${VAR:-60}` treats it as unset and the
# 60-second default applies, which is the intended behaviour rather than a hole.
@pytest.mark.parametrize("value", ["0", "abc", "-5", "9" * 48, "601", "1000"])
def test_an_out_of_range_timeout_does_not_reach_the_relaxed_prompt(tmp_path, value):
    """`timeout 0` means *no limit*, and a 48-digit value overflows `[ -gt ]`."""
    repo, _base, head = _build_repo(tmp_path, _docs_change)
    script = _checkout_with_helper(
        tmp_path,
        "import time\ntime.sleep(600)\n",
        f"checkout-timeout-{len(value)}{value[:3]}",
    )

    result, argv, prompt = _run_merge_bot(
        tmp_path,
        repo,
        head,
        script=script,
        env_extra={"AUTOPILOT_DOCS_EVIDENCE_TIMEOUT": value},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt
    assert DOCS_PROMPT_MARK not in prompt
    assert "must be 1..600 seconds" in result.stdout


def test_a_hanging_evidence_helper_is_killed_and_does_not_relax_the_prompt(tmp_path):
    """A hang would hold the merge lock until someone noticed."""
    repo, _base, head = _build_repo(tmp_path, _docs_change)
    script = _checkout_with_helper(
        tmp_path, "import time\ntime.sleep(600)\n", "checkout-hangs"
    )

    env_extra = {"AUTOPILOT_DOCS_EVIDENCE_TIMEOUT": "2"}
    result, argv, prompt = _run_merge_bot(
        tmp_path, repo, head, script=script, env_extra=env_extra
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt
    assert DOCS_PROMPT_MARK not in prompt
