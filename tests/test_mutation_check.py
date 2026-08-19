"""The check that a diff's own tests can fail, checked against diffs.

MUT-001 in #265. `tools/ci/mutation_check.py` removes a diff's production
hunks and re-runs the tests that diff touched: if they all still pass, the
diff was never shown to be load bearing. What is pinned here is every verdict
it can reach, because the failure this tool exists to prevent -- a mutation
that did not happen reading exactly like a test that survived one -- is a
failure of *reporting*, and a tool that reports it wrongly is worse than no
tool.

Each case is a synthetic repository built from scratch, so the assertions are
about the tool rather than about whichever commit happens to be at the tip of
`main` today. `MUTATION_CHECK_PYTEST` points the inner run at the interpreter
already running this suite -- a synthetic repo is not a uv project, and
installing one per case would trade a second of runtime for a network call the
suite forbids.
"""

import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "ci" / "mutation_check.py"


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result.stdout


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A repository with one production module and one test file."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "tests").mkdir()
    (root / "app.py").write_text("def greet():\n    return 'hello'\n")
    (root / "tests" / "test_app.py").write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from app import greet\n\n\n"
        "def test_existing():\n"
        "    assert greet()\n"
    )
    _commit(root, "base")
    return root


def _run(repo, base="HEAD~1", head="HEAD", env=None):
    import os

    result = subprocess.run(
        [sys.executable, str(TOOL), "--base", base, "--head", head],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MUTATION_CHECK_PYTEST": f"{sys.executable} -m pytest",
            **(env or {}),
        },
    )
    return result.returncode, result.stdout + result.stderr


class TestTheVerdicts:
    def test_a_diff_whose_test_depends_on_it_is_caught(self, repo):
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_shouts():\n    assert greet() == 'HELLO'\n"
        )
        _commit(repo, "shout")

        code, out = _run(repo)

        assert code == 0
        assert "CAUGHT" in out
        assert "test_shouts" in out

    def test_a_diff_whose_test_survives_its_removal_escapes(self, repo):
        """The defect this tool exists for: a test that cannot fail."""
        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef spare():\n    return 1\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_asserts_nothing_about_the_change():\n    assert greet()\n"
        )
        _commit(repo, "add a function and a test that never reaches it")

        code, out = _run(repo)

        assert code == 1, out
        assert "ESCAPED" in out
        assert "Mutation-Waiver" in out, "the way out has to be in the message"

    def test_a_waiver_on_any_commit_in_the_branch_turns_it_green(self, repo):
        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef spare():\n    return 1\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_asserts_nothing():\n    assert greet()\n"
        )
        _commit(repo, "refactor only\n\nMutation-Waiver: pure move, covered elsewhere")

        code, out = _run(repo)

        assert code == 0, out
        assert "WAIVED" in out
        assert "pure move" in out

    def test_a_docs_only_diff_is_a_noop(self, repo):
        (repo / "README.md").write_text("# hello\n")
        _commit(repo, "docs")

        code, out = _run(repo)

        assert code == 0
        assert "NOOP" in out

    def test_production_with_no_test_touched_is_reported_not_failed(self, repo):
        (repo / "app.py").write_text("def greet():\n    return 'hi'\n")
        _commit(repo, "change with no test")

        code, out = _run(repo)

        assert code == 0
        assert "WARN" in out
        assert "not verified by mutation" in out or "was not verified" in out

    def test_a_bad_revision_is_a_third_state_not_a_pass(self, repo):
        """`TOOLING-ERROR` is 2, deliberately neither 0 nor 1.

        A probe that failed is not a probe that answered -- the rule
        `tools/backfill_status.sh` already applies to `unknown`.
        """
        code, out = _run(repo, base="nope-not-a-sha")

        assert code == 2, out
        assert "TOOLING-ERROR" in out
        assert "NOT verified" in out


class TestItNamesTheDiffsOwnTests:
    def test_it_names_only_the_tests_the_diff_wrote(self):
        """The precision the whole check rests on, asserted where it lives.

        Reverting a diff often reddens tests that were already in the file and
        cover something else -- measured on this repository, reverting #421
        left 38 of 66 green and reddened 28. Counting those as proof would let
        a diff's own new test go untested while the run reported CAUGHT.

        This is a unit assertion rather than an end-to-end one, and the reason
        is worth writing down: a test the diff did **not** touch cannot be
        green on the branch and red under the revert, because it would then
        have been red before the diff too and could not have been on `main`.
        The scenario the precision guards against is therefore unbuildable as a
        repository, and pretending otherwise would be a fixture that proves
        the harness rather than the rule.
        """
        sys.path.insert(0, str(TOOL.parent))
        from mutation_check import touched_tests

        diff = (
            "--- a/tests/test_x.py\n"
            "+++ b/tests/test_x.py\n"
            "@@ -1,3 +1,8 @@\n"
            " def test_old(self):\n"
            "     pass\n"
            "+\n"
            "+\n"
            "+def test_new():\n"
            "+    assert True\n"
        )
        source = (
            "def test_old():\n"
            "    pass\n"
            "\n"
            "\n"
            "def test_new():\n"
            "    assert True\n"
            "\n"
            "\n"
            "def test_untouched():\n"
            "    assert True\n"
        )

        named = touched_tests(diff, lambda path: source)

        assert "test_new" in named
        assert "test_untouched" not in named, (
            "a test the diff never wrote must not stand in for one it did"
        )

    def test_a_test_edited_in_place_is_still_named(self, repo):
        """Git's hunk header names the enclosing *class* for a method, so the
        names come from parsing the file rather than from the header -- #424's
        diff edits two assertions inside one method and named nothing this way.
        """
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import greet\n\n\n"
            "class TestGreeting:\n"
            "    def test_case(self):\n"
            "        assert greet() == 'HELLO'\n"
        )
        _commit(repo, "shout")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        assert "test_case" in out
        assert "no test function could be named" not in out


class TestTheThingsAnAdversarialReviewFound:
    """Every one of these reported a verdict that was not true of the diff.

    Five of them turned a genuinely CAUGHT diff into ESCAPED, which is the
    worse direction: it blocks an honest PR and tells its author their test
    does not test their code.
    """

    def test_a_module_level_import_is_caught_not_escaped(self, repo):
        """The common import style, and it read as ESCAPED.

        pytest reports a file that cannot be collected as `ERROR <path>`, with
        no `::`. Read into the same list as `FAILED <nodeid>`, it never matched
        a test name, so the branch for collection errors was unreachable and
        the run fell through to "none of your tests went red" -- about a file
        that could not even import without the diff.

        The tool's first test for a reverted addition imported inside the test
        body, which produces an ordinary per-test failure; the top-of-file form
        that nearly every module here uses was untested and broken.
        """
        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef shout():\n    return greet().upper()\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import greet, shout\n\n\n"
            "def test_existing():\n"
            "    assert greet()\n\n\n"
            "def test_shouts():\n"
            "    assert shout() == 'HELLO'\n"
        )
        _commit(repo, "add shout")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        assert "failed to collect" in out
        assert "masks every other test" in out

    def test_a_name_that_is_a_prefix_of_another_is_not_confused_with_it(self, repo):
        """`::test_price` is a substring of `::test_price_after_discount`.

        This repository names tests in exactly that shape, so an unrelated
        neighbour failing for its own reasons could be reported as the diff's
        own test going red -- a green check over a production change nothing
        covers.
        """
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import greet\n\n\n"
            "def test_price_after_discount():\n"
            "    raise AssertionError('always red, for its own reasons')\n"
        )
        _commit(repo, "a neighbour that always fails")

        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef spare():\n    return 1\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_price():\n    assert greet()\n"
        )
        _commit(repo, "an untested function and a test that never reaches it")

        code, out = _run(repo)

        assert code == 1, out
        assert "ESCAPED" in out

    def test_colour_in_pytest_output_does_not_flip_the_verdict(self, repo):
        """`FAILED` arriving as `\x1b[31mFAILED\x1b[0m` matched nothing."""
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import greet\n\n\n"
            "def test_shouts():\n"
            "    assert greet() == 'HELLO'\n"
        )
        _commit(repo, "shout")

        code, out = _run(repo, env={"PY_COLORS": "1", "FORCE_COLOR": "1"})

        assert code == 0, out
        assert "CAUGHT" in out
        assert "\x1b[" not in out

    def test_the_stripper_is_pinned_where_the_flag_cannot_reach(self):
        """`--color=no` on the pytest command is the first defence, and it
        hides the second from an end-to-end test. Both exist because the flag
        only governs the runs this tool starts, while `_plain` also covers a
        `MUTATION_CHECK_PYTEST` that wraps pytest in something colourising."""
        sys.path.insert(0, str(TOOL.parent))
        from mutation_check import _failed_nodeids, _plain

        coloured = "\x1b[31mFAILED\x1b[0m tests/t.py::\x1b[1mtest_x\x1b[0m - boom"
        failed, collect = _failed_nodeids(_plain(coloured))

        assert failed == ["tests/t.py::test_x"]
        assert collect == []

    def test_a_deleted_test_file_is_not_handed_to_pytest(self, repo):
        """It does not exist at head, and pytest answers a missing path with a
        usage error that parsed as "no tests ran" -- reported as ESCAPED under
        a summary quoting the usage error as if it were a result."""
        (repo / "tests" / "test_old.py").write_text(
            "def test_obsolete():\n    assert True\n"
        )
        _commit(repo, "an obsolete file")

        (repo / "tests" / "test_old.py").unlink()
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import greet\n\n\n"
            "def test_shouts():\n"
            "    assert greet() == 'HELLO'\n"
        )
        _commit(repo, "fix, new test, and drop the obsolete file")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        assert "not found" not in out

    def test_a_rename_is_reverted_rather_than_refused(self, repo):
        """`git checkout <base> -- <new path>` does not know a path the base
        never had, so every rename PR reported TOOLING-ERROR -- and a
        `Mutation-Waiver:` cannot reach that state, because it is not a
        verdict about the diff."""
        (repo / "renamed.py").write_text((repo / "app.py").read_text())
        (repo / "app.py").unlink()
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from renamed import greet\n\n\n"
            "def test_after_the_rename():\n"
            "    assert greet() == 'hello'\n"
        )
        _commit(repo, "rename app.py")

        code, out = _run(repo)

        assert "TOOLING-ERROR" not in out, out
        assert code in (0, 1)

    def test_the_range_is_measured_from_the_merge_base(self, repo):
        """A branch behind `main` would otherwise be asked to answer for hunks
        it never wrote."""
        start = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        branch_tip = _commit(repo, "the branch, touching one production file")

        _git(repo, "checkout", "-q", start)
        (repo / "unrelated.py").write_text("OTHER = 1\n")
        main_tip = _commit(repo, "what main gained meanwhile")

        # No test file was touched, so the verdict is WARN -- and WARN counts
        # the production files, which is where a wrong range shows. Two-dot
        # would see `unrelated.py` as a deletion this branch made and count 2.
        code, out = _run(repo, base=main_tip, head=branch_tip)

        assert code == 0, out
        assert "WARN" in out
        assert "1 production file(s)" in out, out


class TestItRevertsWhatTheDiffDid:
    def test_a_newly_added_production_file_is_reverted_by_removing_it(self, repo):
        """Found by the tool on its own third real commit: #421 adds
        `services/population.py`, and `git checkout <base> -- <new file>` does
        not know that path. It failed loudly, as TOOLING-ERROR, which is the
        reason that state exists."""
        (repo / "extra.py").write_text("VALUE = 7\n")
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_extra():\n    from extra import VALUE\n\n    assert VALUE == 7\n"
        )
        _commit(repo, "add a module")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        assert "TOOLING-ERROR" not in out

    def test_it_never_writes_to_the_tree_it_was_invoked_from(self, repo):
        (repo / "app.py").write_text("def greet():\n    return 'HELLO'\n")
        (repo / "tests" / "test_app.py").write_text(
            (repo / "tests" / "test_app.py").read_text()
            + "\n\ndef test_shouts():\n    assert greet() == 'HELLO'\n"
        )
        _commit(repo, "shout")
        before = (repo / "app.py").read_text()

        code, out = _run(repo)

        # Assert it *ran* before asserting it left no trace. Without this line
        # the test passes when the tool is missing entirely -- nothing runs,
        # so nothing is written -- which is exactly the shape this whole file
        # exists to catch, and it was caught by running the check against its
        # own commit and reading which single test survived.
        assert code in (0, 1), out
        assert "MUTATION-CHECK" in out
        assert (repo / "app.py").read_text() == before
        assert _git(repo, "status", "--porcelain").strip() == ""
        assert _git(repo, "worktree", "list").count("\n") == 1, (
            "the worktree it made must be gone"
        )
