"""The map popup's autoPan padding must stay satisfiable on a narrow screen.

Leaflet applies its left-edge correction after its right-edge one, so a padding
request that cannot fit alongside the popup makes the two corrections fight and
leaves the winner undetermined. Measured on a 375px viewport, one load in four
placed the popup the full 50px from the left and left it hanging 46px past the
right edge of the map, cutting off the price, the municipality and the Google
Maps button.

These tests run the real `autoPanPaddingFor` out of `templates/map.html` — not a
copy of it — so the invariant is pinned against the code that ships.
"""

import json
import subprocess

import pytest

from tests.js_harness import NODE, extract_inline_script, function_source, read_template

# The popup's rendered width: maxWidth (300) plus the content margins (26 + 16)
# and the wrapper's own padding/border. Measured in the browser at 345.
POPUP_BOX_WIDTH = 345

# Real viewports, and the map box each leaves once the page's own margins are
# taken off. The 375 row is the one that actually broke.
VIEWPORTS = {
    "iphone-se-375": 349,
    "iphone-13-390": 364,
    "iphone-plus-414": 388,
    "tablet-768": 742,
    "desktop-1280": 1222,
}

# `#map` is `calc(100vh - 250px)` with a 500px floor, so 562 is the real height
# at 375x812. Popup height varies with content; 222 was measured for a listing
# with six travel badges.
DEFAULT_MAP_HEIGHT = 562
POPUP_BOX_HEIGHT = 280

# Measured down from the map's top edge: the zoom control ends at 74, the close
# button over the layer switcher ends at 102. A popup whose top is above that
# has a control drawn over it — at 375px the zoom covered 26px of the title.
TOP_CONTROLS_DEPTH = 102


def _run(map_widths, map_height=DEFAULT_MAP_HEIGHT):
    """Call autoPanPaddingFor() under node for each width."""
    source = extract_inline_script("map.html")
    fn = function_source(source, "autoPanPaddingFor")
    script = f"""
    {fn}
    const out = {json.dumps(map_widths)}.map((w) => {{
        const pad = autoPanPaddingFor(w, {map_height});
        return {{
            mapWidth: w, mapHeight: {map_height},
            left: pad.left, right: pad.right, top: pad.top, bottom: pad.bottom
        }};
    }});
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_padding_always_leaves_room_for_the_popup():
    """left + popup + right must fit the map, at every real viewport."""
    for row in _run(list(VIEWPORTS.values())):
        needed = row["left"] + POPUP_BOX_WIDTH + row["right"]
        assert needed <= row["mapWidth"], (
            f"padding is unsatisfiable at map width {row['mapWidth']}: "
            f"{row['left']} + {POPUP_BOX_WIDTH} + {row['right']} = {needed}. "
            "Leaflet's edge corrections then fight and the popup can hang off "
            "the right edge with its content cut off."
        )


def test_narrow_screen_gives_up_the_clearance_it_cannot_have():
    """A phone cannot clear the controls, and must not pretend it can."""
    (row,) = _run([VIEWPORTS["iphone-se-375"]])
    assert row["left"] < 50 and row["right"] < 75, (
        "375px has only 4px to spare around the popup, so the full 50/75 "
        f"clearance cannot be honoured; got {row}"
    )


def test_a_phone_puts_the_popup_below_the_top_controls_instead():
    """What it cannot get sideways it must take vertically.

    At 375px the popup opened 31px down and the zoom control covered the first
    26px of its title. Width is the binding constraint there, not height.
    """
    for name in ("iphone-se-375", "iphone-13-390", "iphone-plus-414"):
        (row,) = _run([VIEWPORTS[name]])
        assert row["top"] > TOP_CONTROLS_DEPTH, (
            f"{name}: popup would still open under the top controls "
            f"(top padding {row['top']} <= {TOP_CONTROLS_DEPTH}); the zoom "
            "control then covers the start of the title"
        )


def test_vertical_clearance_stays_affordable():
    """The lesson of the horizontal bug: never ask for room that is not there."""
    for height in (500, 562, 900):
        for row in _run(list(VIEWPORTS.values()), map_height=height):
            needed = row["top"] + POPUP_BOX_HEIGHT + row["bottom"]
            assert needed <= row["mapHeight"], (
                f"vertical padding is unsatisfiable at map height {height}: "
                f"{row['top']} + {POPUP_BOX_HEIGHT} + {row['bottom']} = {needed}"
            )


def test_a_wide_map_is_left_alone():
    """Desktop already clears the controls sideways — do not start panning it."""
    (row,) = _run([VIEWPORTS["desktop-1280"]])
    assert row["top"] == 20, (
        "a map wide enough to seat the popup beside the controls must keep its "
        f"old top padding rather than push every popup down; got {row}"
    )


def test_desktop_still_clears_the_control_corners():
    """The clearance exists for a reason — keep it where there is room."""
    (row,) = _run([VIEWPORTS["desktop-1280"]])
    assert row["left"] == 50, row
    # The top-right stack (close button over layer switcher) is ~58px wide.
    assert row["right"] == 75, row


def test_padding_never_goes_negative_on_an_absurdly_narrow_map():
    for row in _run([0, 100, 345]):
        assert row["left"] >= 0 and row["right"] >= 0, row


def test_apply_popup_autopan_writes_both_paddings_onto_every_popup():
    """Run the real wiring function, not a description of it.

    `autoPanPaddingFor` being right is worth nothing if the value never reaches
    the popup options Leaflet reads.
    """
    source = extract_inline_script("map.html")
    script = f"""
    {function_source(source, "autoPanPaddingFor")}
    {function_source(source, "applyPopupAutoPan")}

    const L = {{ point: (x, y) => ({{ x, y }}) }};
    const map = {{ getSize: () => ({{ x: 349, y: 562 }}) }};   // a 375px phone
    const popupA = {{ options: {{}} }};
    const popupB = {{ options: {{}} }};
    const markerById = new Map([
        [1, {{ marker: {{ getPopup: () => popupA }} }}],
        [2, {{ marker: {{ getPopup: () => popupB }} }}],
        [3, {{ marker: {{ getPopup: () => null }} }}],   // must not throw
    ]);

    applyPopupAutoPan();
    console.log(JSON.stringify({{ a: popupA.options, b: popupB.options }}));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    for name, opts in out.items():
        assert "autoPanPaddingTopLeft" in opts, f"popup {name} got no left padding"
        assert "autoPanPaddingBottomRight" in opts, f"popup {name} got no right padding"
        needed = (
            opts["autoPanPaddingTopLeft"]["x"]
            + POPUP_BOX_WIDTH
            + opts["autoPanPaddingBottomRight"]["x"]
        )
        assert needed <= 349, f"popup {name} received unsatisfiable padding: {opts}"
        assert opts["autoPanPaddingTopLeft"]["y"] > TOP_CONTROLS_DEPTH, (
            f"popup {name} was given a top padding that still leaves it under "
            f"the map's top controls: {opts}"
        )


def test_popup_box_width_constant_still_matches_the_template():
    """POPUP_BOX_WIDTH is derived from maxWidth; catch the two drifting apart."""
    html = read_template("map.html")
    assert "const POPUP_MAX_WIDTH = 300;" in html, (
        "the popup's maxWidth changed — POPUP_BOX_WIDTH here and inside "
        "autoPanPaddingFor() are derived from it and must be re-measured"
    )
    assert "maxWidth: POPUP_MAX_WIDTH" in html, (
        "bindPopup no longer uses the shared constant, so the padding maths "
        "can silently drift from the popup's real width"
    )
