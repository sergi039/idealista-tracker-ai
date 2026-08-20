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
        assert "could not be collected" in out
        assert "nothing in those files ran" in out

    def test_a_collection_error_does_not_stop_the_rest_of_the_run(self, repo):
        """One unimportable file used to take the whole session with it.

        pytest interrupts on a collection error by default: `1 error`, and
        **no test runs, in any file**. Measured on the first external diff
        this check saw (#427) -- four test files touched, one of them new and
        importing symbols the revert removes, so the verdict rested entirely
        on that import while the other three were never exercised. The note
        said it masked "every other test in the same file", which understated
        what actually happened by three files.
        """
        (repo / "tests" / "test_other.py").write_text(
            "def test_unrelated():\n    assert True\n"
        )
        _commit(repo, "a second test file")

        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef shout():\n    return 'HELLO'\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import shout\n\n\n"
            "def test_shouts():\n"
            "    assert shout() == 'HELLO'\n"
        )
        (repo / "tests" / "test_other.py").write_text(
            "def test_unrelated():\n    assert True\n\n\ndef test_also_unrelated():\n    assert True\n"
        )
        _commit(repo, "add shout, touch both test files")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        # The other file ran: without --continue-on-collection-errors the
        # summary is a bare "1 error" and nothing else executed.
        assert "passed" in out, out
        assert "could not be collected" in out

    def test_the_masking_note_rides_on_a_red_verdict_too(self, repo):
        """The shape #427 actually is, and the one the first fix missed.

        A diff can break one file's import *and* redden a touched test in
        another. The verdict is then CAUGHT on the red one, and the file that
        never ran is withheld information all the same -- reported, not
        dropped because something else already earned the green.
        """
        (repo / "helper.py").write_text("VALUE = 1\n")
        (repo / "tests" / "test_other.py").write_text(
            "import sys\n\n\ndef test_value():\n"
            "    sys.path.insert(0, '.')\n"
            "    from helper import VALUE\n\n"
            "    assert VALUE == 1\n"
        )
        _commit(repo, "a second module and its test")

        (repo / "app.py").write_text(
            "def greet():\n    return 'hello'\n\n\ndef shout():\n    return 'HELLO'\n"
        )
        (repo / "helper.py").write_text("VALUE = 2\n")
        (repo / "tests" / "test_app.py").write_text(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from app import shout\n\n\n"
            "def test_shouts():\n"
            "    assert shout() == 'HELLO'\n"
        )
        (repo / "tests" / "test_other.py").write_text(
            "import sys\n\n\ndef test_value():\n"
            "    sys.path.insert(0, '.')\n"
            "    from helper import VALUE\n\n"
            "    assert VALUE == 2\n"
        )
        _commit(repo, "change both, break one import")

        code, out = _run(repo)

        assert code == 0, out
        assert "CAUGHT" in out
        assert "test_value" in out, "the red touched test is what earns the green"
        assert "could not be collected" in out, (
            "and the file that never ran is still disclosed"
        )

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


class TestTheShellHarnesses:
    """MUT-002 in #265: the check could not see this repository's other tests.

    Three shell harnesses cover the deploy watcher -- the one part of the tree
    pytest does not reach -- and `classify()` counted them as production. A
    diff that added five scenarios to one of them was reported as "no test file
    was touched, so there is nothing to re-run", every clause defensible
    against the tool's own definitions and the sentence false about the diff.

    The synthetic harness below is the real ones' shape and nothing more: a
    `fail` that prints `FAIL:` and exits, and scenarios that read the watcher
    beside them through `BASH_SOURCE`. That last part is what makes running one
    inside the check's disposable worktree work at all.
    """

    HARNESS = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "fail() { printf 'FAIL: %s\\n' \"$*\" >&2; exit 1; }\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'WATCHER="${SCRIPT_DIR}/deploy_watcher.sh"\n'
    )

    def _shell_repo(self, root, scenarios, watcher_body):
        (root / "tools" / "autopilot").mkdir(parents=True, exist_ok=True)
        (root / "tools" / "autopilot" / "deploy_watcher.sh").write_text(
            "#!/bin/bash\n" + watcher_body
        )
        (root / "tools" / "autopilot" / "deploy_probe_test.sh").write_text(
            self.HARNESS + scenarios
        )

    def test_a_scenario_the_diff_wrote_going_red_is_caught(self, repo):
        """The verdict MUT-002 exists to make reachable."""
        self._shell_repo(repo, "printf 'OK\\n'\n", "echo old\n")
        _commit(repo, "harness base")
        self._shell_repo(
            repo,
            'grep -q "brand new behaviour" "$WATCHER" \\\n'
            '    || fail "the watcher lost its brand new behaviour"\n'
            "printf 'OK\\n'\n",
            "echo old\n# brand new behaviour\n",
        )
        _commit(repo, "add behaviour and a scenario for it")

        code, out = _run(repo)
        assert "CAUGHT" in out, out
        assert "deploy_probe_test.sh went red on a scenario this diff wrote" in out
        assert "brand new behaviour" in out
        assert code == 0

    def test_a_scenario_that_survives_the_revert_escapes(self, repo):
        self._shell_repo(repo, "printf 'OK\\n'\n", "echo old\n")
        _commit(repo, "harness base")
        self._shell_repo(
            repo,
            'grep -q "echo old" "$WATCHER" \\\n'
            '    || fail "a scenario asserting what was already true"\n'
            "printf 'OK\\n'\n",
            "echo old\n# something new nobody checks\n",
        )
        _commit(repo, "a scenario that does not depend on the change")

        code, out = _run(repo)
        assert "ESCAPED" in out, out
        assert "stayed green" in out
        assert "Mutation-Waiver" in out
        assert code == 1

    def test_a_harness_that_stops_early_is_unproven_not_escaped(self, repo):
        """`fail` exits, so a harness stops at its FIRST red scenario.

        If that one is not the diff's own, the diff's scenarios never ran. That
        is neither a catch nor an escape, and calling it either would be this
        check's own defect class: a probe that did not run, read as an answer.
        """
        self._shell_repo(
            repo,
            'grep -q "token=one" "$WATCHER" \\\n'
            '    || fail "the token scenario"\n'
            "printf 'OK\\n'\n",
            "token=one\n",
        )
        _commit(repo, "harness base")
        # The first scenario's `grep` line changes and its `fail` line does
        # not, which is what keeps it a scenario the diff does not own -- the
        # real harnesses put the assertion on its own continuation line, and
        # that is the shape this depends on. It now fails first, before the
        # scenario the diff wrote.
        self._shell_repo(
            repo,
            'grep -q "token=two" "$WATCHER" \\\n'
            '    || fail "the token scenario"\n'
            'grep -q "extra line" "$WATCHER" \\\n'
            '    || fail "the scenario this diff actually wrote"\n'
            "printf 'OK\\n'\n",
            "token=two\nextra line\n",
        )
        _commit(repo, "rename the token and add a scenario")

        code, out = _run(repo)
        assert "WARN" in out, out
        assert "stopped at a scenario this diff did not write" in out
        assert "never ran" in out
        assert "ESCAPED" not in out
        assert "CAUGHT" not in out
        assert code == 0

    def test_a_watcher_change_alone_still_says_what_it_looked_for(self, repo):
        """The residual WARN names both kinds of test, not "no test file"."""
        self._shell_repo(repo, "printf 'OK\\n'\n", "echo old\n")
        _commit(repo, "harness base")
        self._shell_repo(repo, "printf 'OK\\n'\n", "echo old\n# unchecked\n")
        _commit(repo, "change the watcher and nothing else")

        code, out = _run(repo)
        assert "WARN" in out, out
        assert "pytest module under tests/" in out
        assert "shell harness under tools/autopilot/" in out
        assert "no test file was touched" not in out
        assert code == 0

    def test_the_watcher_itself_is_production_and_is_reverted(self, repo):
        """Only `*_test.sh` under tools/autopilot is a test.

        If `deploy_watcher.sh` were classified as one it would never be
        reverted, and every diff touching it would report that its tests
        survived a mutation that never happened.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("mutation_check", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        production, tests, shell = module.classify(
            [
                "tools/autopilot/deploy_watcher.sh",
                "tools/autopilot/lib/render_check.sh",
                "tools/autopilot/deploy_inflight_test.sh",
                "tests/test_app.py",
            ]
        )
        assert shell == ["tools/autopilot/deploy_inflight_test.sh"]
        assert tests == ["tests/test_app.py"]
        assert production == [
            "tools/autopilot/deploy_watcher.sh",
            "tools/autopilot/lib/render_check.sh",
        ]
