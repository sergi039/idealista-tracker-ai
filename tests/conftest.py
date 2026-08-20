"""Guards against per-process state that escapes a test.

Four independent guards live here. The `pytest_runtest_teardown` wrapper
(per-test) fails any test that leaves a Flask app or request context pushed,
and scrubs the application reference flask-caching retains on the
module-global `utils.cache.cache` -- see its docstring for why that retention
makes a test behave differently alone and in the full run. The socket guard in
tests/network_guard.py, installed and reported from the hooks below, refuses
every connect that leaves this machine (issue #307); its own module docstring
carries the reasoning. The skip guard in tests/skip_guard.py fails the run on
a skip nobody approved, because a skipped test reports success and that is how
the network guard itself could be switched off silently (#310). The rest of
the module is the session-scoped Config-mutation guard below.

`Config` (config.py) keeps several mutable containers as *class* attributes -
`DEFAULT_SCORING_WEIGHTS`, `SCORING_PROFILES`, `COMBINED_MIX` and friends. They
are shared by the whole process, so anything that binds one by reference and
mutates it in place pollutes every later consumer. #44 was exactly that bug:
`ScoringService.__init__` did `self.weights = Config.DEFAULT_SCORING_WEIGHTS`
and then `.update()`d it, permanently injecting the combined-mix rows
(`investment` / `lifestyle`) into the criterion set.

Nothing in the suite watched for it. `TestConfigWeightsNotMutated`
(tests/test_scoring_service.py) takes its `before` snapshot *inside* each test,
which correctly pins the source-level invariant but compares dirty against
dirty once an earlier module has already polluted the dict.

This guard snapshots the containers in `pytest_configure`, before any test body
runs, and compares in `pytest_sessionfinish`. Deliberately session-scoped, not
per-test: tests/test_security_and_scoring.py legitimately runs
`patch.object(Config, "SCORING_PROFILES", ...)` / `"COMBINED_MIX"` inside a
context manager that restores itself, and a per-test guard would flag that. Only
mutation that *escapes* a test is the failure mode worth catching.

Scope is deliberately mutable containers only. Scalar class attributes are
excluded: several modules assign `Config.AUTO_TRAVEL_ENRICHMENT = False`,
`Config.SALE_ONLY = True` and similar without restoring them, and those
rebindings are not the aliasing bug this guards.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

import flask
import pytest
from flask import globals as flask_globals

from config import Config
from tests import network_guard, skip_guard
from utils.cache import cache as flask_cache


def pytest_runtest_setup(item) -> None:
    """Close the listing-status refusal breaker before every test.

    `ListingStatusService.breakers` is process-global on purpose: it counts
    each host's refusals across calls so the next press of the check button
    does not walk into the same wall (services/listing_status_service.py). That
    is exactly the state this file exists to keep out of other tests. Three
    refusal tests in a row open it, and every later test in the process then
    gets `backing_off` -- including the ones asserting that a live listing page
    reads as active, which fail with no reference to the file that armed it.
    Measured: 21 such failures across three modules, all from the first module
    that exercised a captcha.

    `reset()` drops **every** host's breaker, not just idealista's. It became a
    per-host registry when fotocasa listings arrived, and a reset that cleared
    one host would leak exactly the state this hook was written to stop, minus
    the one case anybody would think to check.

    A hook rather than an autouse fixture, for the ordering reason in the
    teardown wrapper below. Before rather than after, so a test that arms it
    deliberately can still read it in its own teardown.
    """
    from services.listing_status_service import ListingStatusService

    ListingStatusService.breakers.reset()

    # Ingestion may not reach a billed Google API unless a test says so.
    #
    # `AUTO_GEOCODING` defaults to *true* in production (config.py, 2026-08-17):
    # it is the one paid call the free enrichers cannot do without, and at
    # $0.005 a listing it is 1.4% of what the travel step it replaced cost. In
    # this suite that default is wrong twice over. Nine modules drive
    # `run_ingestion()` to assert something else entirely -- the UID cursor,
    # profile dedup, municipality truncation, sale-only filtering -- and none
    # of them mocks a geocoder, because until this flag existed "paid
    # enrichment off" was one switch and turning travel off turned geocoding
    # off with it. Left on, all nine reached live Nominatim (the fallback in
    # `utils/geocoding.py`) and tests/network_guard.py failed the run, which is
    # exactly what it is for.
    #
    # Mocking the geocoder in each of them is the answer that does not hold:
    # the tenth ingestion test would not know it had to, and would reach the
    # network again. So the default here is off, and the two tests that are
    # *about* this flag turn it on for themselves
    # (tests/test_paid_google_is_on_request.py). That file also reads the real
    # production defaults out of a clean interpreter, so switching them off
    # here cannot make a wrong default look right.
    from config import Config

    Config.AUTO_GEOCODING = False

    # Overpass answers "nothing here" unless a test says otherwise, and it is
    # reset *per test* for the same reason the breaker above is: a test that
    # points this at a refusal (tests/test_issue_98_...) would otherwise leave
    # it pointing there for every test that follows, which is how one edit
    # turned three failures into six.
    #
    # The travel presets are answered from OpenStreetMap since 2026-08-18
    # (services/osm_places.py). Suites written against the Places path mock
    # Google and nothing else, so the moment a preset started asking Overpass
    # instead, six of them reached the live internet and
    # `tests/network_guard.py` failed the run -- which is what it is for.
    #
    # The stub answers "Overpass replied and there is nothing of that type
    # here", never a refusal: a refusal is the state that must not be
    # invented, and a test that wants one sets it up.
    # `services/property_travel_service.py` imports the module rather than the
    # function so that this one patch point reaches it, and this is
    # deliberately not a stub on `EnrichmentService._overpass_elements`, which
    # the amenity, pool and quality-of-life suites patch underneath.
    import services.osm_places as _osm_places

    _osm_places.lookup_candidates = lambda service, specs, lat, lon: ({}, None)

    # The hazard scan (#437) is the second Overpass consumer to run on the
    # *free* pass, so it reaches the network from every suite that exercises
    # ingestion or the Enrich flow -- the same six that #323 caught, plus the
    # ingestion ones. Its network seam is `_elements`, which is where the
    # cache and the transport meet; stubbing it here rather than
    # `EnrichmentService._overpass_elements` leaves the amenity, pool and
    # quality-of-life suites free to patch that underneath, exactly as the
    # preset stub above does.
    #
    # `[]` is "Overpass replied and there is nothing here", never a refusal.
    import services.hazard_service as _hazard_service

    _hazard_service.fetch_elements = lambda service, lat, lon: ([], None)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail a test that leaks Flask state, and scrub what it leaked.

    A hook wrapper, deliberately not an autouse fixture: pytest-flask's own
    autouse `_monkeypatch_response_class` instantiates any fixture named
    `app` via `getfixturevalue` before a conftest autouse fixture gets its
    turn, so a fixture-based guard finalises FIRST and pops the app context
    out from under every `with app.app_context(): yield` fixture --
    measured as 857 teardown errors across the suite. The wrapper's
    post-yield code runs after `teardown_exact`, i.e. after every fixture
    has finalised, which is the only point where "still pushed" means
    "leaked".

    Two channels, both chased while the #196 follow-up branch's new sea-view
    test passed file-alone and failed in the full suite (2026-08-10):

    * A pushed-and-never-popped app or request context. One leaked context
      would make `flask.current_app` resolve in every later test on the
      thread, silently changing what hundreds of them actually exercise --
      most visibly, cache reads that are supposed to no-op outside an app
      context start returning real values. Measured over the whole suite with
      a drain-and-report plugin, no test does this today; the guard exists so
      the first one to start is named at its own teardown instead of being
      diagnosed from a symptom three files later. The drain below is so one
      offender does not also fail every test that runs after it.

    * flask-caching's retained application. `Cache._set_cache` keeps the last
      app it was initialised for (`self.app`, flask-caching 2.3.1 -- despite
      the comment in `init_app` saying it must not), and its backend lookup
      is `current_app or self.app`. `current_app` is falsy outside a context
      rather than raising (werkzeug's `__bool__` fallback), so once ANY
      earlier test has called create_app(), the module-global
      `utils.cache.cache` serves real cached values with no app context at
      all. That was the actual mechanism behind the #196 symptom: a coastline
      cell cached by one test was served to the next, whose transport mock
      then never fired -- in the full suite only, because file-alone no app
      had ever been created. Deleting the binding restores the fresh-process
      state (`self.app` missing, lookup raises AttributeError, callers
      no-op), so a test without the `app` fixture behaves the same alone and
      in the full run.
    """
    teardown_error: BaseException | None = None
    result = None
    try:
        result = yield
    except BaseException as exc:
        teardown_error = exc

    leaked = 0
    # Request contexts first: popping one also pops the app context it
    # carried in with it, and an app context cannot pop from under one.
    while flask.has_request_context():
        flask_globals.request_ctx._get_current_object().pop()
        leaked += 1
    while flask.has_app_context():
        flask_globals.app_ctx._get_current_object().pop()
        leaked += 1

    vars(flask_cache).pop("app", None)

    # A teardown that failed on its own outranks the leak report: re-raising
    # it keeps the original diagnosis, and the drain above has already run.
    if teardown_error is not None:
        raise teardown_error

    if leaked:
        pytest.fail(
            f"this test left {leaked} Flask context(s) pushed. Enter contexts "
            "with `with app.app_context():` (or pop in teardown what setup "
            "pushed); a leaked context changes what every later test in the "
            "process actually tests."
        )
    return result


# In-place mutation of one of these is the failure mode. Tuples/frozensets and
# scalars cannot be aliased and then mutated, so they are out of scope.
MUTABLE_TYPES: tuple[type, ...] = (dict, list, set)

# Values are pasted into the failure message; keep single lines readable.
MAX_VALUE_REPR = 120

_snapshot: dict[str, Any] | None = None
_problems: list[str] = []
_reported = False
_network_reported = False
_skip_reported = False


def _mutable_config_attributes() -> dict[str, Any]:
    """Every mutable container Config owns, by attribute name.

    Deliberately not a hard-coded list: `SCORING_PROFILES` and `COMBINED_MIX`
    get the same protection as `DEFAULT_SCORING_WEIGHTS`, and so does any
    container added to config.py later.
    """
    return {
        name: value
        for name, value in vars(Config).items()
        if not name.startswith("__") and isinstance(value, MUTABLE_TYPES)
    }


def _fmt(value: Any) -> str:
    text = repr(value)
    if len(text) > MAX_VALUE_REPR:
        text = text[: MAX_VALUE_REPR - 3] + "..."
    return text


def _join(entries: Iterable[str]) -> str:
    return ", ".join(entries)


def _dict_changes(
    before: dict[Any, Any], after: dict[Any, Any], prefix: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Added / removed / changed keys, recursing into nested dicts.

    Nested mappings (SCORING_PROFILES) report dotted paths such as
    `investment.environment` so the message names the key that actually moved
    rather than dumping two whole sub-dicts.
    """
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []

    for key, value in after.items():
        if key not in before:
            added.append(f"{prefix}{key}={_fmt(value)}")

    for key, value in before.items():
        if key not in after:
            removed.append(f"{prefix}{key}={_fmt(value)}")
            continue
        new_value = after[key]
        if isinstance(value, dict) and isinstance(new_value, dict):
            sub_added, sub_removed, sub_changed = _dict_changes(
                value, new_value, prefix=f"{prefix}{key}."
            )
            added.extend(sub_added)
            removed.extend(sub_removed)
            changed.extend(sub_changed)
        elif value != new_value:
            changed.append(f"{prefix}{key}: {_fmt(value)} -> {_fmt(new_value)}")

    return added, removed, changed


def _describe(name: str, before: Any, after: Any) -> list[str]:
    """Human-readable detail lines for one mutated attribute."""
    lines = [f"Config.{name} was mutated during the test session:"]

    if isinstance(before, dict) and isinstance(after, dict):
        added, removed, changed = _dict_changes(before, after)
        if added:
            lines.append(f"  added keys:   {_join(added)}")
        if removed:
            lines.append(f"  removed keys: {_join(removed)}")
        if changed:
            lines.append(f"  changed keys: {_join(changed)}")
    elif isinstance(before, set) and isinstance(after, set):
        added_items = sorted(_fmt(item) for item in after - before)
        removed_items = sorted(_fmt(item) for item in before - after)
        if added_items:
            lines.append(f"  added items:   {_join(added_items)}")
        if removed_items:
            lines.append(f"  removed items: {_join(removed_items)}")
    else:
        lines.append(f"  before: {_fmt(before)}")
        lines.append(f"  after:  {_fmt(after)}")

    return lines


def _detect_mutations() -> list[str]:
    """Compare live Config containers against the snapshot."""
    if _snapshot is None:  # pragma: no cover - configure always runs first
        return []

    current = _mutable_config_attributes()
    problems: list[str] = []

    for name, before in _snapshot.items():
        if name not in current:
            problems.append(
                f"Config.{name} disappeared during the test session "
                f"(was {_fmt(before)})."
            )
            continue
        after = current[name]
        if before != after:
            problems.extend(_describe(name, before, after))

    for name in current:
        if name not in _snapshot:
            problems.append(
                f"Config.{name} appeared during the test session "
                f"(now {_fmt(current[name])})."
            )

    return problems


def _message() -> list[str]:
    return _problems + [
        "",
        "A test left a Config class attribute mutated. Copy the container "
        "before mutating it (see #44/#45), or restore it in the test.",
    ]


def pytest_configure(config: pytest.Config) -> None:
    """Snapshot the mutable Config containers before any test body runs, and
    close the network off before anything can reach it.

    Both happen here rather than in a fixture because collection itself runs
    code: a test module's import time is inside the session and outside every
    fixture.
    """

    global _snapshot
    if _snapshot is None:
        _snapshot = {
            name: copy.deepcopy(value)
            for name, value in _mutable_config_attributes().items()
        }
    network_guard.install()
    skip_guard.reset()


def _skip_reason(report: Any) -> str:
    """The message a skip was raised with, as pytest recorded it.

    `longrepr` for a skipped report is the (path, lineno, message) triple, for
    a collected module and a run test alike. Anything else yields no reason
    rather than a guessed one: an unapproved skip is refused either way, and
    the guard says "(none given)" instead of inventing text to match against.
    """
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return ""


def pytest_runtest_logreport(report: Any) -> None:
    """Hand every skip to the skip guard.

    The module path comes from `report.fspath` -- `nodeid` up to the first
    `::` -- so an entry in `ALLOWED` is keyed by the file a reader would open.
    """
    if not report.skipped or report.when not in ("setup", "call"):
        return
    skip_guard.note_skip(report.nodeid, report.fspath, _skip_reason(report))


def pytest_collectreport(report: Any) -> None:
    """A module skipped at import never reaches the hook above at all.

    `pytest.skip(..., allow_module_level=True)` and `importorskip` are refused
    during collection, so pytest emits a *collect* report and no test in that
    file is ever run or reported. The file disappears from the session and the
    only trace is the same skip count nothing reads -- the #310 shape, one
    stage earlier and covering a whole module at once.

    Nothing in this repository skips that way today (measured on 7c96493: no
    `allow_module_level`, no `importorskip` anywhere under tests/). That is the
    reason to wire it now rather than the reason to leave it: the first such
    skip would otherwise be invisible to the guard written to catch exactly it.
    """
    if not report.skipped:
        return
    skip_guard.note_skip(report.nodeid, report.fspath, _skip_reason(report))


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    """Attribute the connects made from here on to this test."""
    network_guard.note_test(nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run when a mutation, a live network call, or an unapproved
    skip escaped a test."""
    global _problems
    _problems = _detect_mutations()
    if not _problems and not network_guard.attempts() and not skip_guard.offenses():
        return

    # Raising here does not fail the run: wrap_session() has already computed
    # the exit status by the time this hook is called, and returns
    # session.exitstatus after it. Setting that attribute is what makes the
    # process exit non-zero. Never downgrade an already-failing status.
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print the diagnosis next to the final stats line.

    Runs from inside the terminal reporter's own pytest_sessionfinish wrapper,
    i.e. after the warnings summary, so the message is the last thing before
    the pass/fail counts rather than buried above them.

    The network report is printed even though each refusal already raised: the
    callers catch `Exception` and degrade, so a refused call can leave a green
    test and no trace anywhere else in the output.
    """
    global _reported, _network_reported, _skip_reported
    skip_lines = skip_guard.summary_lines()
    if skip_lines:
        terminalreporter.write_sep("=", "skip guard: FAILED", red=True, bold=True)
        for line in skip_lines:
            terminalreporter.write_line(line, red=True)
        _skip_reported = True
    network_lines = network_guard.summary_lines()
    if network_lines:
        terminalreporter.write_sep("=", "network guard: FAILED", red=True, bold=True)
        for line in network_lines:
            terminalreporter.write_line(line, red=True)
        _network_reported = True
    if not _problems:
        return
    terminalreporter.write_sep(
        "=", "config mutation guard: FAILED", red=True, bold=True
    )
    for line in _message():
        terminalreporter.write_line(line, red=True)
    _reported = True


def pytest_unconfigure(config: pytest.Config) -> None:
    """Last resort when there is no terminal summary to write into.

    A run failed by either guard has to say why: the exit status is set in
    pytest_sessionfinish whether or not anything printed, and a non-zero exit
    with no diagnosis is the worst of both.
    """
    if _problems and not _reported:  # pragma: no cover - needs -p no:terminal
        print("\n".join(["config mutation guard: FAILED", *_message()]))
    network_lines = network_guard.summary_lines()
    if network_lines and not _network_reported:  # pragma: no cover - as above
        print("\n".join(["network guard: FAILED", *network_lines]))
    skip_lines = skip_guard.summary_lines()
    if skip_lines and not _skip_reported:  # pragma: no cover - as above
        print("\n".join(["skip guard: FAILED", *skip_lines]))
    network_guard.uninstall()
