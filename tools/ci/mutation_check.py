#!/usr/bin/env python3
"""Re-run a diff's own tests against the diff reverted, and fail if they pass.

MUT-001 in #265. The 2026-08-15 retrospective (#314) found five defects in one
day that survived a green suite because the test meant to catch each one could
not fail. #315 shipped the half that is a mechanism -- `tests/skip_guard.py` --
and the half that is not: a CLAUDE.md rule asking whoever changes a test to
undo the fix by hand and paste which tests go red.

That rule is a habit, and habits are what the retrospective was about. Measured
on 2026-08-19 across five merged PRs, an agent following it deliberately still
produced **three** hand-run mutations that read as green without having tested
anything:

* a text substitution stopped matching because `ruff format` had rewrapped the
  line after the edit, so nothing was mutated and the run printed `26 passed`;
* a flag was flipped on a code path the assertion never reached, so the feature
  was "verified" through a different road entirely (`59 passed`);
* a measurement harness split a multi-line variable under zsh, which does not
  word-split, so the captured patch was empty and "the revert applied, tests
  green" was a sentence about nothing.

All three are the same shape: **a mutation that did not happen is
indistinguishable, in the tail of a pytest run, from a test that survived one.**
Two of the three disappear here by construction, because the revert is git
moving real hunks rather than a string being matched, and because it happens in
a worktree this script creates and destroys itself. The third does not
disappear -- a test can execute a mutated line without asserting on its effect,
which is mutation testing's equivalent-mutant problem and is not solved by
anyone. What changes is that it stops being invisible: a diff whose own tests
all survive its own revert is `ESCAPED` and red, and saying "that is fine here"
costs a `Mutation-Waiver:` trailer a reviewer reads next to the diff.

Verdicts, and the exit code each carries:

    NOOP           0   no production hunks -- a docs- or tests-only diff
    WARN           0   production hunks, no test file touched (a coverage
                       question this ticket does not decide)
    CAUGHT         0   at least one of the diff's own tests went red
    WAIVED         0   ESCAPED, and the branch says why
    ESCAPED        1   every test the diff touched survived the diff's removal
    TOOLING-ERROR  2   the check could not run; it is not a pass

`TOOLING-ERROR` is deliberately neither 0 nor 1. A probe that failed is a third
state, and folding it into either of the other two is the defect family this
repository keeps rediscovering (`tools/backfill_status.sh`: "unknown blocks
exactly like busy, because every defect in this family began with a failed
probe reading as a negative answer").

Usage, from anywhere in the repository:

    uv run python tools/ci/mutation_check.py --base <sha> --head <sha>

It creates its own detached worktree, so it can never write to the tree it was
invoked from. That is not caution for its own sake: the third near-miss above
was `git checkout -- <path>` deleting an uncommitted fix in the shared
checkout, and CLAUDE.md treats that class as a production incident.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

#: Reads a path's content at the head commit.
SourceReader = Callable[[str], str]

# A test file whose tests this check may re-run. Anything else in `tests/` --
# a fixture module, a guard, `conftest.py` -- is production code as far as this
# check is concerned: reverting it should break something.
TEST_FILE = re.compile(r"^tests/test_[^/]+\.py$")

# Files whose reversion proves nothing about a test.
DOC_SUFFIXES = (".md", ".txt", ".rst")

# The trailer that turns a red ESCAPED into a green WAIVED. Read from every
# commit in the range rather than from the tip: a branch is squash-merged here,
# but CI runs before the squash, so the reason may sit on any of its commits.
WAIVER = re.compile(r"^Mutation-Waiver:\s*(\S.*)$", re.MULTILINE)

# Which file a hunk belongs to, and which lines it wrote at head.
FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
HUNK_RANGE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def pytest_command() -> List[str]:
    """How to run pytest. `uv run pytest` unless the environment says otherwise.

    `MUTATION_CHECK_PYTEST` exists so this tool can be tested against synthetic
    repositories that are not uv projects -- `tests/test_mutation_check.py`
    points it at the interpreter already running the suite. It is a seam for
    the tests, not a way to make a red check green: whatever it names still has
    to run the diff's own tests and still has to fail.
    """
    override = os.environ.get("MUTATION_CHECK_PYTEST", "").strip()
    return shlex.split(override) if override else ["uv", "run", "pytest"]


class ToolingError(Exception):
    """The check could not run. Never a verdict about the diff."""


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        # No git, or an unreadable `--repo`. Without this the OSError escapes
        # to the interpreter, which exits 1 -- the ESCAPED code -- and prints
        # no verdict line at all, so a broken environment would read as a
        # judgement about the diff.
        raise ToolingError(f"could not run git {' '.join(args)}: {error}") from error
    if result.returncode != 0:
        raise ToolingError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _parse_name_status(raw: str) -> List[Tuple[str, str]]:
    """`[(kind, path)]` from `git diff --name-status`, one entry per real path.

    `kind` is `"A"` for a path the diff created (which a revert must delete),
    `"D"` for one it removed, and `"M"` for everything else (which a revert
    restores from the base).

    A rename arrives as one line, `R100<tab>old<tab>new`, and needs both
    halves: the worktree at head holds `new` and not `old`, so reverting means
    deleting one and restoring the other. Reading only `fields[-1]`, as this
    did first, left every rename PR reporting TOOLING-ERROR -- `git checkout
    <base> -- <new path>` does not know a path the base never had.
    """
    parsed: List[Tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        letter = fields[0][:1]
        if letter in ("R", "C") and len(fields) >= 3:
            parsed.append(("D", fields[1]))
            parsed.append(("A", fields[2]))
        elif letter == "A":
            parsed.append(("A", fields[-1]))
        elif letter == "D":
            parsed.append(("D", fields[-1]))
        else:
            parsed.append(("M", fields[-1]))
    return parsed


def classify(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split the diff's files into what is reverted and what is re-run.

    A file under `tests/` that is not itself a `test_*.py` module counts as
    production: `conftest.py`, `skip_guard.py` and the fixture modules are
    machinery, and a diff that changes them has to be provable the same way.
    """
    production: List[str] = []
    tests: List[str] = []
    for path in paths:
        if TEST_FILE.match(path):
            tests.append(path)
        elif path.endswith(DOC_SUFFIXES):
            continue
        else:
            production.append(path)
    return production, tests


def changed_lines(diff: str) -> List[Tuple[str, int, int]]:
    """`[(path, first, last)]` -- the line ranges the diff wrote, at head."""
    ranges: List[Tuple[str, int, int]] = []
    path: Optional[str] = None
    for line in diff.splitlines():
        header = FILE_HEADER.match(line)
        if header:
            path = header.group(1)
            continue
        hunk = HUNK_RANGE.match(line)
        if hunk and path:
            start = int(hunk.group(1))
            count = int(hunk.group(2) or 1)
            if count:
                ranges.append((path, start, start + count - 1))
            else:
                # A pure deletion: git names the line the removed text sat
                # after, so the function that lost it is the one spanning
                # either side of that point.
                ranges.append((path, max(1, start), start + 1))
    return ranges


def touched_tests(diff: str, source_of: "SourceReader") -> List[str]:
    """Names of the test functions the diff added, removed or edited.

    The check is about *this diff's* tests. A file it touched can hold two
    hundred others, and one of those going red under the revert says nothing
    about whether the new one can fail -- measured on this repository:
    reverting #421 left 38 of 66 tests in its touched files green, and
    reverting #422 left 19 of 26.

    Read from the parsed file rather than from git's hunk headers. Git names
    the enclosing function with a language-agnostic heuristic -- the nearest
    preceding line starting at column zero -- which for a test method inside a
    class is the `class` line, not the `def`. Measured on #424, whose diff
    edits two assertions inside one method: the header approach named nothing
    and the check fell back to "any test in the file", which is exactly the
    looseness this function exists to remove.
    """
    names: List[str] = []
    for path, first, last in changed_lines(diff):
        try:
            tree = ast.parse(source_of(path))
        except (SyntaxError, ToolingError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = getattr(node, "end_lineno", node.lineno)
            if start <= last and first <= end and node.name not in names:
                names.append(node.name)
    return names


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(output: str) -> str:
    """pytest colourises when `PY_COLORS`/`FORCE_COLOR` is set in the ambient
    environment, and every regex below anchors on literal text. Strip once,
    at the door: a `FAILED` that arrives as `\x1b[31mFAILED\x1b[0m` matched
    nothing, and a genuinely caught diff was reported ESCAPED under a summary
    line that said `1 failed` in the same sentence."""
    return ANSI.sub("", output)


def _pytest_summary(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\b\d+ (passed|failed|error)", line):
            return line.strip()
    return output.strip().splitlines()[-1] if output.strip() else "(no output)"


def _failed_nodeids(output: str) -> Tuple[List[str], List[str]]:
    """`(failed test nodeids, files that failed to collect)`.

    pytest writes both under `FAILED`/`ERROR` in its short summary, and the
    two mean different things: a nodeid carries `::`, a collection error names
    the file alone. Reading them as one list made the collection branch
    unreachable -- the bare path never matches a test name, so a file that
    could not even import was reported as ESCAPED, which is the strongest
    possible evidence of dependence read as its opposite.
    """
    failed: List[str] = []
    collect: List[str] = []
    for kind, token in re.findall(r"^(FAILED|ERROR)\s+(\S+)", output, re.MULTILINE):
        (failed if "::" in token else collect).append(token)
    return failed, collect


def _function_of(nodeid: str) -> str:
    """The test function's name in a nodeid, without its parametrisation.

    Compared for equality, never containment: `::test_price` is a substring of
    `::test_price_after_discount`, and this repository names tests in exactly
    that shape (`test_score` beside `test_score_with_pool`), so an unrelated
    neighbour failing for its own reasons could be reported as the diff's own
    test going red.
    """
    tail = nodeid.split("::")[-1]
    return tail.split("[", 1)[0]


def run(base: str, head: str, repo: Path) -> Tuple[str, str, int]:
    """Returns `(verdict, message, exit code)`."""
    # `--name-status`, not `--name-only`: a diff that *adds* a production file
    # cannot be reverted by checking that path out of the base, which does not
    # have it. Found by this check on its own third real commit -- #421 adds
    # `services/population.py` -- and found loudly, as a TOOLING-ERROR rather
    # than as a pass, which is the whole reason that state exists.
    # Three-dot semantics, via an explicit merge base. `base..head` on a
    # branch that is behind `main` includes every commit `main` gained in the
    # meantime, so this PR would be asked to answer for other people's hunks.
    base = _git("merge-base", base, head, cwd=repo).strip() or base
    status = _parse_name_status(
        _git("diff", "--name-status", f"{base}..{head}", cwd=repo)
    )
    # A rename arrives as `R100<old><new>` and needs both halves: the new path
    # is deleted and the old one restored. A deleted *test* file must not reach
    # pytest at all -- it does not exist at head, and pytest answers a missing
    # path with a usage error that parses as no tests at all.
    production, tests = classify([path for _, path in status])
    added = {path for kind, path in status if kind == "A"}
    # A test file the diff deleted must not reach pytest: it does not exist at
    # head, and pytest answers a missing path with a usage error that parses as
    # "no tests ran" -- which was then reported as ESCAPED, under a summary
    # line quoting the usage error as if it were a pytest result.
    removed_tests = {path for kind, path in status if kind == "D"}
    tests = [path for path in tests if path not in removed_tests]

    if not production:
        return "NOOP", f"no production hunks in {base}..{head}", 0
    if not tests:
        return (
            "WARN",
            f"{len(production)} production file(s) changed and no test file was "
            "touched, so there is nothing to re-run. This check does not decide "
            "whether that is acceptable; it reports that the diff was not "
            "verified by mutation.",
            0,
        )

    # `-U0`: with git's default three lines of context the hunk header's range
    # runs past the change and into whatever sits above it, so a test appended
    # under `test_price_after_discount` named that function too -- and an
    # unrelated neighbour going red then counted as the diff's own test.
    test_diff = _git("diff", "-U0", f"{base}..{head}", "--", *tests, cwd=repo)

    def source_of(path: str) -> str:
        return _git("show", f"{head}:{path}", cwd=repo)

    wanted = touched_tests(test_diff, source_of)

    # Everything below happens in a worktree this process owns.
    holder = Path(tempfile.mkdtemp(prefix="mutation-check."))
    worktree = holder / "wt"
    try:
        # A run killed mid-flight leaves its worktree registered but gone from
        # disk, and git then refuses the next `worktree add` under a name it
        # thinks is taken. Pruning first costs milliseconds and keeps one
        # interrupted run from blocking every later one.
        _git("worktree", "prune", cwd=repo)
        _git("worktree", "add", "--detach", "--quiet", str(worktree), head, cwd=repo)
        # `git checkout <base> -- <paths>` rather than a captured patch applied
        # in reverse: it cannot half-apply, cannot fuzz, and cannot silently
        # match nothing. The command that deleted an uncommitted fix on
        # 2026-08-19 was `git checkout -- <path>` in the *shared* checkout,
        # with no commit named; naming a commit inside a worktree this function
        # created is a different act, and the guard above is what keeps it one.
        restore = [path for path in production if path not in added]
        if restore:
            _git("checkout", base, "--", *restore, cwd=worktree)
        for path in production:
            if path not in added:
                continue
            # Reverting an added file means removing it. A test that imports
            # it then fails at collection, which the classifier below reports
            # as CAUGHT *and* names as masking. `is_file` rather than a bare
            # unlink: a gitlink or a directory raises, and an unhandled
            # exception here would exit 1 -- the ESCAPED code -- with no
            # verdict line at all.
            target = worktree / path
            if target.is_file() or target.is_symlink():
                target.unlink()
            elif target.exists():
                raise ToolingError(
                    f"cannot revert the addition of {path}: it is not a regular file"
                )
        reverted = [
            line[3:]
            for line in _git("status", "--porcelain", cwd=worktree).splitlines()
        ]
        if not reverted:
            raise ToolingError(
                "reverting the production files changed nothing in the "
                "worktree -- the diff and the checkout disagree, and a revert "
                "that reverts nothing must never read as a test that survived"
            )

        try:
            result = subprocess.run(
                [
                    *pytest_command(),
                    *tests,
                    "-q",
                    "--no-header",
                    "--color=no",
                    "-p",
                    "no:randomly",
                ],
                cwd=str(worktree),
                capture_output=True,
                text=True,
            )
        except OSError as error:
            # A pytest that will not spawn is a probe that did not run. Without
            # this the OSError escapes to the interpreter, which exits 1 -- the
            # ESCAPED code -- and prints no verdict at all.
            raise ToolingError(f"could not run pytest: {error}") from error
        output = _plain(result.stdout + result.stderr)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(repo),
            capture_output=True,
        )
        shutil.rmtree(holder, ignore_errors=True)

    summary = _pytest_summary(output)
    failed, collect_errors = _failed_nodeids(output)

    if collect_errors:
        return (
            "CAUGHT",
            f"{summary} -- {', '.join(collect_errors)} failed to collect once "
            "the diff was removed, which is the strongest kind of dependence "
            "on it. Note that it masks every other test in the same file: they "
            "could not run at all.",
            0,
        )

    if wanted:
        names = set(wanted)
        red = [node for node in failed if _function_of(node) in names]
        if red:
            return (
                "CAUGHT",
                f"{summary} -- of the tests this diff touched, "
                f"{len(red)} went red: {', '.join(sorted(set(red))[:6])}",
                0,
            )
        if failed:
            return (
                "ESCAPED",
                f"{summary}, but not one of the {len(wanted)} test(s) this diff "
                f"touched ({', '.join(wanted[:6])}). The red ones "
                f"({', '.join(sorted(set(failed))[:6])}) were already in those "
                "files and cover something else, so this diff's own tests were "
                "not shown to depend on it.",
                1,
            )
    elif failed:
        return (
            "CAUGHT",
            f"{summary} -- no test function could be named from the diff "
            "(a fixture or a helper changed, most likely), so this fell back "
            "to 'any test in the touched files'.",
            0,
        )

    return (
        "ESCAPED",
        f"{summary}. Removing {', '.join(production[:6])} left every test in "
        f"{', '.join(tests)} green, so nothing there was shown to depend on "
        "this diff. If that is right -- a refactor, a revert, a test written "
        "for behaviour that already existed -- say so with a "
        "'Mutation-Waiver: <reason>' trailer on any commit in the branch.",
        1,
    )


def waiver_in(base: str, head: str, repo: Path) -> Optional[str]:
    messages = _git("log", "--format=%B", f"{base}..{head}", cwd=repo)
    match = WAIVER.search(messages)
    return match.group(1).strip() if match else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="Commit the diff starts from.")
    parser.add_argument("--head", required=True, help="Commit the diff ends at.")
    parser.add_argument(
        "--repo", default=".", help="Any path inside the repository (default: cwd)."
    )
    args = parser.parse_args(argv)

    try:
        repo = Path(_git("rev-parse", "--show-toplevel", cwd=Path(args.repo)).strip())
        verdict, message, code = run(args.base, args.head, repo)
        if verdict == "ESCAPED":
            reason = waiver_in(args.base, args.head, repo)
            if reason:
                verdict, message, code = "WAIVED", f"{reason} (was: {message})", 0
    except ToolingError as error:
        print(f"MUTATION-CHECK: TOOLING-ERROR ({error})")
        print(
            "  This diff was NOT verified by mutation. A probe that failed is "
            "not a probe that answered."
        )
        return 2

    print(f"MUTATION-CHECK: {verdict} ({message})")
    return code


if __name__ == "__main__":
    sys.exit(main())
