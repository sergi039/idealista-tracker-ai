"""`whenMapSettles` — the wait the focus flow uses instead of a flat timer.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
It runs the real helper out of `templates/map.html` under node and pins its
contract. It says nothing about whether the popup ends up clear of the map's
controls, because that depends on Leaflet, on the browser's layout and on the
map's animation — none of which are here.

That distinction is the lesson of #297: a green function-level suite shipped a
fix that did not fix anything, and a second one that regressed a viewport.
Acceptance for this behaviour is a measurement on a built image, hit-testing
every popup control with `elementFromPoint` over several page loads at more
than one width. The numbers behind this change are in the PR and in #302.
"""

import json
import subprocess

import pytest

from tests.js_harness import NODE, extract_inline_script, function_source, read_template

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _drive(script_body):
    """Run whenMapSettles() under node against a stub map and a fake clock."""
    fn = function_source(extract_inline_script("map.html"), "whenMapSettles")
    script = f"""
    // A clock we control: timers only fire when the script says so.
    let now = 0;
    const pending = [];
    const setTimeout = (fn, ms) => {{ const t = {{at: now + (ms || 0), fn, dead: false}}; pending.push(t); return t; }};
    const clearTimeout = (t) => {{ if (t) t.dead = true; }};
    const tick = (ms) => {{
        const until = now + ms;
        for (;;) {{
            const due = pending.filter((t) => !t.dead && t.at <= until).sort((a, b) => a.at - b.at)[0];
            if (!due) break;
            due.dead = true;
            now = due.at;
            due.fn();
        }}
        now = until;
    }};

    const map = {{
        handlers: {{}},
        on(names, fn) {{ names.split(' ').forEach((n) => ((this.handlers[n] = this.handlers[n] || []).push(fn))); }},
        off(names, fn) {{ names.split(' ').forEach((n) => {{
            this.handlers[n] = (this.handlers[n] || []).filter((f) => f !== fn);
        }}); }},
        fire(n) {{ (this.handlers[n] || []).slice().forEach((f) => f()); }},
        listenerCount() {{ return Object.values(this.handlers).reduce((a, l) => a + l.length, 0); }},
    }};

    {fn}

    let calls = 0;
    const out = {{}};
    {script_body}
    out.calls = calls;
    out.listenersLeft = map.listenerCount();
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_map_that_never_moves_still_runs_the_callback():
    """The popup must open even when setView had nothing to do.

    A map already at the target view fires no events at all. Waiting for one
    would leave the listing without its popup — worse than the overlap.
    """
    out = _drive("whenMapSettles(map, () => calls++); tick(1000);")
    assert out["calls"] == 1, out


def test_movement_postpones_the_callback_until_things_go_quiet():
    out = _drive("""
        whenMapSettles(map, () => calls++, {quietMs: 200});
        tick(150); map.fire('move');
        tick(150); map.fire('move');
        tick(150); out.duringMovement = calls;
        tick(250);
    """)
    assert out["duringMovement"] == 0, (
        f"fired while the map was still moving: {out}. A popup opened then is "
        "positioned from a place the map is leaving."
    )
    assert out["calls"] == 1, out


def test_a_map_that_never_settles_is_not_waited_on_forever():
    """A stuck animation must not cost the user their popup."""
    out = _drive("""
        whenMapSettles(map, () => calls++, {quietMs: 200, maxWaitMs: 1000});
        for (let i = 0; i < 40; i++) { tick(100); map.fire('move'); }
    """)
    assert out["calls"] == 1, f"the timeout guard did not fire: {out}"


def test_the_callback_runs_once_and_the_listeners_are_released():
    out = _drive("""
        whenMapSettles(map, () => calls++, {quietMs: 100, maxWaitMs: 500});
        tick(100);
        map.fire('moveend'); map.fire('zoomend'); tick(2000);
    """)
    assert out["calls"] == 1, f"callback ran {out['calls']} times: {out}"
    assert out["listenersLeft"] == 0, (
        f"map listeners were left behind: {out}. Every focus load would add "
        "another set."
    )


def _drive_show_focused(body):
    """Run the real showFocusedPopup() against stubs and a controlled clock."""
    source = extract_inline_script("map.html")
    script = f"""
    let now = 0;
    const pending = [];
    const setTimeout = (fn, ms) => {{ const t = {{at: now + (ms || 0), fn, dead: false}}; pending.push(t); return t; }};
    const clearTimeout = (t) => {{ if (t) t.dead = true; }};
    const tick = (ms) => {{
        const until = now + ms;
        for (;;) {{
            const due = pending.filter((t) => !t.dead && t.at <= until).sort((a, b) => a.at - b.at)[0];
            if (!due) break;
            due.dead = true; now = due.at; due.fn();
        }}
        now = until;
    }};

    const log = [];
    const popup = {{
        open: false,
        isOpen() {{ return this.open; }},
        update() {{ log.push('update'); }},
    }};
    const focusLayer = {{
        setIcon() {{}},
        setZIndexOffset() {{}},
        openPopup() {{ popup.open = true; log.push('openPopup'); }},
        getPopup: () => popup,
    }};
    const focusEntry = {{ score: 50, marker: focusLayer }};
    const markerCluster = {{ getVisibleParent: () => null }};
    const createIcon = () => ({{}});
    const applyPopupAutoPan = () => log.push('applyPopupAutoPan');
    const map = {{
        handlers: {{}},
        on(names, fn) {{ names.split(' ').forEach((n) => ((this.handlers[n] = this.handlers[n] || []).push(fn))); }},
        off(names, fn) {{ names.split(' ').forEach((n) => {{
            this.handlers[n] = (this.handlers[n] || []).filter((f) => f !== fn);
        }}); }},
        fire(n) {{ (this.handlers[n] || []).slice().forEach((f) => f()); }},
    }};

    {function_source(source, "whenMapSettles")}
    {function_source(source, "showFocusedPopup")}

    const out = {{}};
    {body}
    out.log = log;
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_popup_is_not_re_adjusted_while_the_map_is_still_moving():
    """The whole defect, as behaviour rather than as text.

    Inlining the re-adjust straight after openPopup() — which is #297 exactly —
    reads almost the same in the file and is wrong. Only the ordering shows it:
    re-running the pan while the map is still moving computes from a position
    the map is leaving, and measured on a built image the popup then opened
    57px down with its close button under the layer switcher.
    """
    out = _drive_show_focused("""
        showFocusedPopup(map, focusLayer, focusEntry);
        out.rightAfterOpen = log.slice();
        map.fire('move');
        tick(150); map.fire('move');
        tick(150);
        out.whileMoving = log.slice();
        tick(400);
        out.afterSettling = log.slice();
    """)

    assert out["rightAfterOpen"] == ["openPopup"], (
        "the popup was re-adjusted in the same breath as opening it, before the "
        f"map could come to rest: {out['rightAfterOpen']}"
    )
    assert out["whileMoving"] == ["openPopup"], (
        f"the re-adjust ran while the map was still moving: {out['whileMoving']}"
    )
    assert out["afterSettling"] == ["openPopup", "applyPopupAutoPan", "update"], (
        "once the map is at rest the popup must have its padding refreshed and "
        f"its pan re-run, in that order; got {out['afterSettling']}"
    )


def test_a_popup_closed_before_the_map_settles_is_left_alone():
    out = _drive_show_focused("""
        showFocusedPopup(map, focusLayer, focusEntry);
        popup.open = false;          // the user closed it while the map moved
        tick(1000);
    """)
    assert out["log"] == ["openPopup"], (
        f"a closed popup must not be re-adjusted; got {out['log']}"
    )


def test_the_flat_timer_is_gone():
    """The 300ms guess is the thing this change replaces."""
    html = read_template("map.html")
    assert "}, 300);" not in html, (
        "the flat 300ms timer is back; it is a guess at how long setView takes "
        "and says nothing about the animation zoomToShowLayer starts"
    )


def test_the_cluster_wrapper_still_waits_for_the_map():
    """The outer wait, pinned separately from the inner one.

    zoomToShowLayer runs its own animation, so its callback must be handed to
    whenMapSettles rather than called straight through.
    """
    html = read_template("map.html")
    assert (
        "whenMapSettles(map, () => showFocusedPopup(map, focusLayer, focusEntry))"
        in html
    ), (
        "zoomToShowLayer's callback no longer waits for the map to settle "
        "before showing the focused popup"
    )
