"""The page must follow a job for as long as the server may work on it.

`pollJob` defaulted to 120 s while `services/property_ai_service.py` asked the
bridge for 600 s (now `config.py`'s `AI_ANALYSIS_TIMEOUT_SECONDS`, 180 s --
#206 item 3), and no caller ever overrode it. A long analysis was therefore
announced as failed while the server was still writing it — and the natural
response, pressing the button again, queued a second *paid* run of work that
was already done.

Two promises are pinned. Structurally: every call site states its own budget,
so the implicit default can never quietly govern an operation again. And
behaviourally, by running the real function under node: when the budget does
run out, the poller reports that the work continues rather than that it failed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = ROOT / "static" / "js" / "main.js"
POLL_SITES = (
    MAIN_JS,
    ROOT / "templates" / "property_detail.html",
    ROOT / "templates" / "land_detail.html",
)

# Every budget the page may use, and the server-side limit each one answers to.
EXPECTED_BUDGETS = {
    # config.py AI_ANALYSIS_TIMEOUT_SECONDS (180s) + AI_BRIDGE_SOCKET_MARGIN_
    # SECONDS (25s) + a 60s allowance for the app's own job-queueing (#206
    # item 3; see the comment on JOB_POLL_TIMEOUTS in static/js/main.js).
    "aiAnalysis": 265000,
    "enrichment": 300000,
    "listingStatus": 180000,
}


def _call_argument_lists(source: str):
    """Yield the argument text of every `pollJob(` call, brace-balanced."""
    for match in re.finditer(r"pollJob\(", source):
        if source[: match.start()].rstrip().endswith(":"):
            continue  # the definition itself: `pollJob: function(`
        depth, start = 0, match.end() - 1
        for pos in range(start, len(source)):
            if source[pos] in "([{":
                depth += 1
            elif source[pos] in ")]}":
                depth -= 1
                if depth == 0:
                    yield match.start(), source[start : pos + 1]
                    break
        else:
            raise AssertionError("unbalanced pollJob( call")


def _balanced(source: str, start: int, opener: str, closer: str) -> int:
    """Index of the `closer` matching the `opener` at or after `start`."""
    depth, first = 0, source.index(opener, start)
    for pos in range(first, len(source)):
        if source[pos] == opener:
            depth += 1
        elif source[pos] == closer:
            depth -= 1
            if depth == 0:
                return pos
    raise AssertionError(f"unbalanced {opener}{closer} from offset {start}")


def _extract_poll_job(source: str) -> str:
    """Return `function(...) {...}` for pollJob, as runnable JavaScript.

    The parameter list contains its own braces (`options = {}`), so the body
    has to be found after the parameter list closes, not from the first `{`.
    """
    start = source.index("pollJob: function(")
    params_end = _balanced(source, start, "(", ")")
    body_end = _balanced(source, params_end, "{", "}")
    return source[source.index("function(", start) : body_end + 1]


@pytest.mark.parametrize("path", POLL_SITES, ids=lambda p: p.name)
def test_every_poll_call_states_its_own_budget(path):
    source = path.read_text(encoding="utf-8")
    calls = list(_call_argument_lists(source))
    assert calls, f"no pollJob call sites found in {path.name}"
    for offset, args in calls:
        line = source[:offset].count("\n") + 1
        assert "timeoutMs" in args, (
            f"{path.name}:{line} polls on the implicit default; "
            "state the budget for that operation"
        )


def test_the_promise_helper_requires_a_budget():
    """The three callers of `_pollJobPromise` have different budgets."""
    source = (ROOT / "templates" / "property_detail.html").read_text(encoding="utf-8")
    assert "function _pollJobPromise(jobId, timeoutMs)" in source
    assert "_pollJobPromise(data.job_id)" not in source, (
        "a caller still relies on a defaulted budget"
    )


def test_budgets_are_declared_once_and_cover_the_server_limits():
    source = MAIN_JS.read_text(encoding="utf-8")
    for name, value in EXPECTED_BUDGETS.items():
        assert re.search(rf"\b{name}:\s*{value}\b", source), (
            f"{name} budget is missing or changed; it must match what the "
            "server allows that operation"
        )
    # The default stays as a backstop: a caller that states nothing is still
    # not polled for ever.
    assert "options.timeoutMs || 120000" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_timeout_reports_work_in_progress_not_failure(tmp_path):
    body = _extract_poll_job(MAIN_JS.read_text(encoding="utf-8"))

    script = tmp_path / "poll.js"
    script.write_text(
        "\n".join(
            [
                f"const pollJob = {body};",
                # The server keeps answering "running": the job never ends.
                "globalThis.window = { setTimeout, clearTimeout };",
                "globalThis.fetch = async () => ({",
                "  ok: true,",
                "  status: 200,",
                "  json: async () => ({ success: true, job: { status: 'running' } }),",
                "});",
                "pollJob('job-1', {",
                "  intervalMs: 5,",
                "  timeoutMs: 40,",
                "  onSuccess: () => { console.log(JSON.stringify({unexpected: 'success'}));"
                " process.exit(0); },",
                "  onError: (job) => { console.log(JSON.stringify(job)); process.exit(0); },",
                "});",
            ]
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    reported = json.loads(out.stdout)

    assert reported.get("status") == "timeout"
    assert reported.get("stillRunning") is True, (
        "a client-side timeout must not be presented as a finished run"
    )
    message = (reported.get("error") or "").lower()
    assert "fail" not in message and "error" not in message, (
        f"timeout message blames the run: {reported.get('error')!r}"
    )
    assert "still running" in message
