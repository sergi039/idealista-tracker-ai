"""The pure decisions inside static/js/owner-decision.js, run under node (#630).

The DOM half of the script cannot run here; the two decisions that went wrong
in review can: what a row shows after a write that may or may not have landed,
and where the "next" link points while rows leave the undecided queue.
"""

import json
from pathlib import Path

from tests.js_harness import run_node_script

SCRIPT = Path(__file__).resolve().parent.parent / "static" / "js" / "owner-decision.js"


def _run(expression: str, tmp_path):
    driver = (
        f"const m = require({json.dumps(str(SCRIPT))});\n"
        f"console.log(JSON.stringify({expression}));\n"
    )
    return run_node_script(driver, tmp_path)


def test_a_timed_out_write_that_committed_reads_as_saved(tmp_path):
    result = _run(
        "m.settleAfterWrite('uncertain', null, 'interested', 'interested')", tmp_path
    )
    assert result == {"kind": "saved", "state": "interested"}


def test_a_timed_out_write_that_did_not_commit_reads_as_not_saved(tmp_path):
    result = _run(
        "m.settleAfterWrite('uncertain', null, 'undecided', 'waiting')", tmp_path
    )
    assert result == {"kind": "not_saved", "state": None}


def test_an_unreadable_server_is_unconfirmed_and_never_undecided(tmp_path):
    result = _run(
        "m.settleAfterWrite('uncertain', null, undefined, 'waiting')", tmp_path
    )
    assert result["kind"] == "unconfirmed"
    assert "state" not in result  # undefined: not null, not 'undecided'


def test_a_definite_http_error_reverts_to_the_previous_decision(tmp_path):
    result = _run(
        "m.settleAfterWrite('http_error', 'waiting', undefined, 'rejected')", tmp_path
    )
    assert result == {"kind": "failed", "state": "waiting"}


def test_an_answered_write_shows_what_the_server_says(tmp_path):
    result = _run("m.settleAfterWrite('ok', null, 'rejected', 'rejected')", tmp_path)
    assert result == {"kind": "saved", "state": "rejected"}


def test_next_points_at_the_current_page_only_while_a_row_has_left_the_queue(
    tmp_path,
):
    result = _run(
        "[m.nextLinkTarget('/p?page=2', '/p?page=1', 1),"
        " m.nextLinkTarget('/p?page=2', '/p?page=1', 0)]",
        tmp_path,
    )
    assert result == ["/p?page=1", "/p?page=2"]


def test_only_undecided_to_decided_moves_a_row_out_of_the_queue(tmp_path):
    result = _run(
        "[m.leftTheQueue(null, 'interested'),"
        " m.leftTheQueue(null, null),"
        " m.leftTheQueue('waiting', null),"
        " m.leftTheQueue('waiting', 'rejected'),"
        " m.leftTheQueue('undecided', 'waiting')]",
        tmp_path,
    )
    assert result == [True, False, False, False, True]
