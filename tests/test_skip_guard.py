"""The skip guard's own contract (issue #314).

These tests drive `tests/skip_guard.py` and the two hooks in
`tests/conftest.py` directly, rather than through a nested pytest run. The
nested-run alternative was rejected on the lesson these tests exist for: a
subprocess pytest would need its own conftest, and a harness that does not
share this one proves something about the harness, not about the guard the
suite actually installs. `from tests import conftest` is that same conftest --
pytest imports it as `tests.conftest`, because tests/ is a package -- so the
hooks driven below are the ones the session really runs.

Driving the hooks matters because *neither hook body executes in a green run*.
Both return at `if not report.skipped`, and CI has no skips at all to get past
it (measured on 7c96493: 2298 passed, 0 skipped). Wiring that only runs when
something is already wrong, and is therefore never exercised, is the shape this
whole retrospective is about.
"""

from __future__ import annotations

import pytest

from tests import conftest, skip_guard


@pytest.fixture(autouse=True)
def _clean_record():
    """Keep these tests out of the live record the session will be failed on."""
    before = skip_guard.offenses()
    skip_guard.reset()
    yield
    skip_guard.reset()
    for nodeid, path, reason in before:
        skip_guard.note_skip(nodeid, path, reason)


class _Report:
    """A stand-in for the report objects the two hooks in conftest read.

    `fspath` is derived from `nodeid` the way pytest's own `BaseReport` derives
    it -- `nodeid.split("::")[0]` -- so these tests cannot pass by handing the
    hook a module path pytest would never produce.
    """

    def __init__(self, nodeid, *, skipped=True, longrepr=None, when="call"):
        self.nodeid = nodeid
        self.skipped = skipped
        self.longrepr = longrepr
        self.when = when

    @property
    def fspath(self):
        return self.nodeid.split("::")[0]


class _Session:
    """The one attribute `pytest_sessionfinish` writes to decide the exit code."""

    def __init__(self):
        self.exitstatus = pytest.ExitCode.OK


class TestWhatIsAccountedFor:
    def test_a_pinned_module_may_skip_for_its_pinned_reason(self):
        skip_guard.note_skip(
            "tests/test_postgres_migrations.py::test_x",
            "tests/test_postgres_migrations.py",
            "Skipped: set TEST_DATABASE_URL_POSTGRES to a throwaway PostgreSQL "
            "server to run the migration tests (see this module's docstring)",
        )
        assert skip_guard.offenses() == []

    def test_the_reason_is_matched_as_a_substring(self):
        # The recorded message carries line numbers and interpolated paths that
        # nobody should have to repeat in ALLOWED.
        assert skip_guard.is_allowed(
            "tests/test_inflight_marker.py",
            "no /proc here: liveness is all this platform can answer",
        )


class TestWhatIsRefused:
    def test_a_module_nobody_pinned_fails_the_run(self):
        skip_guard.note_skip(
            "tests/test_scoring_service.py::test_weights",
            "tests/test_scoring_service.py",
            "Skipped: needs the thing",
        )
        assert [o[1] for o in skip_guard.offenses()] == [
            "tests/test_scoring_service.py"
        ]

    def test_a_pinned_module_skipping_for_a_new_reason_fails_the_run(self):
        # The module is allowed to skip when Postgres is absent. It is not
        # allowed to start skipping because an import quietly failed.
        skip_guard.note_skip(
            "tests/test_postgres_migrations.py::test_x",
            "tests/test_postgres_migrations.py",
            "Skipped: could not import psycopg2",
        )
        assert len(skip_guard.offenses()) == 1

    def test_a_refusal_names_the_test_and_says_what_to_do(self):
        skip_guard.note_skip(
            "tests/test_scoring_service.py::test_weights",
            "tests/test_scoring_service.py",
            "Skipped: needs the thing",
        )
        text = "\n".join(skip_guard.summary_lines())
        assert "tests/test_scoring_service.py::test_weights" in text
        assert "needs the thing" in text
        assert "ALLOWED in tests/skip_guard.py" in text


class TestTheCaseThisGuardWasWrittenFor:
    def test_a_whole_module_turning_into_skips_is_refused(self):
        """#310's shape: a mechanism switched off, reported as success.

        Removing `network_guard.install()` from `pytest_configure` turned all
        29 tests in tests/test_network_guard.py into skips -- `29 skipped in
        0.03s`, exit 0. Those skips carry that module's own approved reason, so
        this guard is not what catches *that* one (tests/
        test_network_guard_is_installed.py is). What it catches is the same
        shape in any module nobody has pinned: the mechanism goes quiet and the
        run stops being about it.
        """
        for i in range(29):
            skip_guard.note_skip(
                f"tests/test_some_mechanism.py::test_{i}",
                "tests/test_some_mechanism.py",
                "Skipped: the mechanism is switched off",
            )
        assert len(skip_guard.offenses()) == 29
        assert skip_guard.summary_lines()

    def test_a_whole_module_vanishing_at_collection_is_refused(self):
        """The same shape one stage earlier, where no test report exists.

        `pytest.skip(..., allow_module_level=True)` and `importorskip` are
        refused during collection: no test in the file is run, and pytest emits
        a collect report that `pytest_runtest_logreport` never sees. Before
        `pytest_collectreport` was wired, a module could leave the session
        whole and the guard written for that shape would not have noticed.
        """
        conftest.pytest_collectreport(
            _Report(
                "tests/test_some_mechanism.py",
                when="collect",
                longrepr=(
                    "tests/test_some_mechanism.py",
                    12,
                    "Skipped: the optional dependency is missing",
                ),
            )
        )
        assert [o[1] for o in skip_guard.offenses()] == ["tests/test_some_mechanism.py"]
        assert "optional dependency" in "\n".join(skip_guard.summary_lines())

    def test_the_two_guards_divide_the_work_as_documented(self):
        """The network guard's own skip reason stays approved on purpose.

        `skipif` cannot tell a deliberate `PYTEST_ALLOW_NETWORK` from an
        install that never ran -- the message is the same -- so pinning it here
        would either fail every legitimate escape-hatch run or catch nothing.
        The unconditional check in tests/test_network_guard_is_installed.py is
        what answers that question.
        """
        assert skip_guard.is_allowed(
            "tests/test_network_guard.py",
            "the network guard is switched off (PYTEST_ALLOW_NETWORK)",
        )


class TestTheWiringInConftest:
    """The hooks that feed the guard, driven with the reports pytest builds."""

    def test_a_skipped_test_arrives_with_its_module_and_reason(self):
        conftest.pytest_runtest_logreport(
            _Report(
                "tests/test_scoring_service.py::test_weights",
                when="setup",
                longrepr=("tests/test_scoring_service.py", 8, "Skipped: no fixture"),
            )
        )
        assert skip_guard.offenses() == [
            (
                "tests/test_scoring_service.py::test_weights",
                "tests/test_scoring_service.py",
                "no fixture",
            )
        ]

    def test_a_test_that_ran_is_not_recorded(self):
        conftest.pytest_runtest_logreport(
            _Report("tests/test_scoring_service.py::test_weights", skipped=False)
        )
        assert skip_guard.offenses() == []

    def test_a_skip_is_counted_once_per_test(self):
        """setup and call are read; teardown is not.

        A skip is reported in exactly one of the first two phases -- `skipif`
        in setup, `pytest.skip()` in call -- and reading teardown as well would
        double-count a run this guard is meant to describe precisely.
        """
        for when in ("setup", "call", "teardown"):
            conftest.pytest_runtest_logreport(
                _Report(
                    "tests/test_scoring_service.py::test_weights",
                    when=when,
                    longrepr=("tests/test_scoring_service.py", 8, "Skipped: no"),
                )
            )
        assert len(skip_guard.offenses()) == 2

    def test_a_reason_pytest_did_not_record_still_fails_the_run(self):
        """No reason is not a pass. It is a skip nobody can account for."""
        conftest.pytest_runtest_logreport(
            _Report(
                "tests/test_scoring_service.py::test_weights",
                when="call",
                longrepr="a shape this hook does not read",
            )
        )
        assert len(skip_guard.offenses()) == 1
        assert "(none given)" in "\n".join(skip_guard.summary_lines())


class TestTheRunActuallyFails:
    """Recording an offence is not the same as refusing the run.

    This class exists because the guard could be switched off in silence, which
    is the defect it was written against. Measured on this tree before these
    two assertions existed: deleting `and not skip_guard.offenses()` from
    `pytest_sessionfinish` left all twelve other tests here green, still
    printed the "skip guard: FAILED" banner, and exited **0** -- an unapproved
    skip reported loudly and passed anyway. Everything above pins what the
    guard *notices*; only this pins what it *does about it*.

    `pytest_sessionfinish` also recomputes the config-mutation problems into a
    module global, so each test restores it: this file must not decide what a
    different guard reports about the real session.
    """

    def _finish(self, session):
        problems_before = conftest._problems
        try:
            conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)
        finally:
            conftest._problems = problems_before

    def test_an_unapproved_skip_makes_the_process_exit_non_zero(self):
        skip_guard.note_skip(
            "tests/test_scoring_service.py::test_weights",
            "tests/test_scoring_service.py",
            "Skipped: needs the thing",
        )
        session = _Session()
        self._finish(session)
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_a_session_with_nothing_to_report_is_left_alone(self):
        """A guard that fails every run is not a guard, it is a broken suite."""
        session = _Session()
        self._finish(session)
        assert session.exitstatus == pytest.ExitCode.OK
