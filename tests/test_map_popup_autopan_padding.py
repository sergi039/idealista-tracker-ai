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


def _run(map_widths):
    """Call autoPanPaddingFor() under node for each width."""
    source = extract_inline_script("map.html")
    fn = function_source(source, "autoPanPaddingFor")
    script = f"""
    {fn}
    const out = {json.dumps(map_widths)}.map((w) => {{
        const pad = autoPanPaddingFor(w);
        return {{ mapWidth: w, left: pad.left, right: pad.right }};
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
    const map = {{ getSize: () => ({{ x: 349 }}) }};      // a 375px phone
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
