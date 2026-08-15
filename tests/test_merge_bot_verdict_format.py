"""The review prompt has to say what shape a verdict takes (2026-08-15).

`rx` reads the reviewer's first line and only the first line: the bare keyword,
optionally wrapped in Markdown emphasis, and everything else is UNAVAILABLE.
`merge_bot.sh` correctly refuses to treat UNAVAILABLE as a pass, so an answer it
cannot parse costs a bounded reviewer attempt and leaves the PR unmergeable --
not because the change is wrong, but because of how the answer was typeset.

Measured on PR #312 with `RX_PROVIDER_POLICY=codex-only`: the reviewer returned
two well-argued BLOCKER findings under an opening line of prose, and rx reported
`outcome=UNAVAILABLE reason="verdict not recognised"`. Two real findings thrown
away. No prompt in `merge_bot.sh` had ever stated the requirement.

What these tests pin is deterministic and complete on this side of the line:
that the rule reaches `rx` with both prompts, that it is the last thing either
one says, and that `merge_bot.sh` keeps exactly one copy of it. What they cannot
pin, stated plainly so a green run is not over-read, is compliance -- whether a
given model obeys the paragraph is a model's behaviour, not a function's. That
half was measured by hand against codex, which complied on the first try, twice
in a row, and is the reason the wording below is pinned verbatim rather than by
theme: it is the wording that was tried.
"""

from __future__ import annotations

import shutil

import pytest

from tests.test_merge_bot_docs_only_review import (
    DOCS_PROMPT_MARK,
    MARKER,
    MERGE_BOT,
    STANDARD_PROMPT_MARK,
    _build_repo,
    _docs_change,
    _run_merge_bot,
    _write,
)

# The paragraph as it was measured, newlines included. Pinned as one literal
# because "says something about the first line" is not what made codex comply.
FORMAT_RULE = (
    "FORMAT: your very first line must be exactly 'PASS' or 'BLOCKER', with no\n"
    "Markdown heading, no prefix and nothing else on that line. Reasoning and any\n"
    "findings follow on the lines after it."
)


@pytest.fixture(autouse=True)
def _needs_tooling():
    for tool in ("git", "jq", "python3"):
        if shutil.which(tool) is None:
            pytest.skip(f"merge_bot.sh needs {tool}")


def _code_change(repo) -> None:
    _write(repo, "services/enrichment_service.py", "# rewritten\n")


def test_the_standard_prompt_ends_with_the_verdict_format_rule(tmp_path):
    repo, _base, head = _build_repo(tmp_path, _code_change)
    result, argv, prompt = _run_merge_bot(tmp_path, repo, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert STANDARD_PROMPT_MARK in prompt, "this is meant to be the strict prompt"
    assert FORMAT_RULE in prompt, (
        "the reviewer was not told what shape its verdict must take, so a "
        "well-argued answer can still come back UNAVAILABLE"
    )
    # Last, because it is the instruction the reviewer has to obey at the moment
    # it starts writing.
    assert prompt.rstrip("\n").endswith(FORMAT_RULE)


def test_the_docs_only_prompt_ends_with_the_verdict_format_rule(tmp_path):
    """The relaxed prompt needs it more: the evidence block runs to thousands of
    lines, and the rule has to sit after that, not before it."""
    repo, _base, head = _build_repo(tmp_path, _docs_change)
    result, argv, prompt = _run_merge_bot(tmp_path, repo, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv, "rx was never called"
    assert DOCS_PROMPT_MARK in prompt, "this is meant to be the docs-only prompt"
    assert FORMAT_RULE in prompt
    assert prompt.rstrip("\n").endswith(FORMAT_RULE)
    assert prompt.index(MARKER) < prompt.index(FORMAT_RULE), (
        "the rule is buried above the base excerpts instead of closing the prompt"
    )


def test_neither_prompt_states_the_rule_twice(tmp_path):
    """Repeating it would be harmless; the point is that there is one source."""
    for change in (_code_change, _docs_change):
        workspace = tmp_path / change.__name__
        workspace.mkdir()
        repo, _base, head = _build_repo(workspace, change)
        _result, _argv, prompt = _run_merge_bot(workspace, repo, head)
        assert prompt.count(FORMAT_RULE) == 1


def test_merge_bot_keeps_one_copy_of_the_rule():
    """The property neither prompt test can see between them (cf. #292).

    Both prompts carrying the paragraph is satisfied just as well by two
    literals, and two literals is how a rule ships half-changed.
    """
    source = MERGE_BOT.read_text()
    assert source.count("FORMAT: your very first line") == 1, (
        "the format rule is written down more than once in merge_bot.sh"
    )
    assert source.count("verdict_format_rule") >= 3, (
        "expected the shared helper plus a call from each of the two prompts"
    )
