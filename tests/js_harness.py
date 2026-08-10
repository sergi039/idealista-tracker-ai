"""Run the JavaScript that actually ships inside the detail templates.

The pages build their HTML in inline `<script>` blocks, so a test that only
greps the template proves the string is there and nothing about what it does.
These helpers extract that real script and execute it under node against a stub
DOM, which is how `tests/test_ai_metric_rendering.py` and
`tests/test_issue_23_xss_and_prompt_injection.py` already pin their behaviour.

node is not a CI dependency (the workflow provisions Python only), so a caller
without node is **skipped**, never silently passed.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

# Enough DOM for the render code to run unchanged, including a document
# fragment that behaves like one: appending it moves its children.
DOM_STUB = """
'use strict';
class FakeNode {
    constructor(tag) {
        this.tagName = tag;
        this.childNodes = [];
        this.style = {};
        this.className = '';
        this.__text = '';
        this.__isFragment = false;
    }
    appendChild(child) {
        if (child && child.__isFragment) {
            for (const c of child.childNodes) this.childNodes.push(c);
            child.childNodes = [];
        } else {
            this.childNodes.push(child);
        }
        return child;
    }
    set textContent(value) {
        if (value === '') this.childNodes = [];
        this.__text = String(value);
    }
    get textContent() { return this.__text; }
    set innerHTML(value) {
        if (value === '') this.childNodes = [];
        this.__html = String(value);
    }
    get innerHTML() { return this.__html || ''; }
}

const ELEMENTS = {};
global.window = {};
global.document = {
    addEventListener: function () {},
    getElementById: function (id) {
        if (!ELEMENTS[id]) ELEMENTS[id] = new FakeNode(id);
        return ELEMENTS[id];
    },
    createElement: function (tag) { return new FakeNode(tag); },
    createDocumentFragment: function () {
        const frag = new FakeNode('#fragment');
        frag.__isFragment = true;
        return frag;
    },
};

// Every fetch parks until the driver resolves it, so two refreshes really do
// overlap instead of running one after the other.
const PENDING = [];
global.fetch = function () {
    return new Promise(function (resolve) { PENDING.push(resolve); });
};
const respond = (index, payload) => PENDING[index]({
    json: () => Promise.resolve(payload),
});
const rowsOf = (id) => document.getElementById(id).childNodes.map(
    (tr) => tr.childNodes.map((td) => td.textContent)
);
// The label cell carries its own text plus any note appended under it.
const notesOf = (id) => document.getElementById(id).childNodes.map(
    (tr) => (tr.childNodes[0] ? tr.childNodes[0].childNodes.map(
        (child) => child.textContent) : [])
);
"""


def read_template(template_name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, template_name), "r", encoding="utf-8") as f:
        return f.read()


def extract_inline_script(template_name: str) -> str:
    """The template's single <script> block, with the Jinja-templated
    `window.X = {{ ... }}` bootstrap assignments neutralized -- the only lines
    inside it that are not valid standalone JS. A `{{ id }}` inside a JS
    template literal is legitimate JS and survives untouched."""
    html = read_template(template_name)
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)

    fixed_lines = []
    for line in html[start:end].splitlines():
        if ("{{" in line or "{%" in line) and re.match(r"^\s*window\.\w+\s*=", line):
            name = re.match(r"^\s*(window\.\w+)\s*=", line).group(1)
            fixed_lines.append(f"{name} = null;")
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)


def function_source(source: str, name: str) -> str:
    """Return one top-level `[async] function name(...) {...}` by brace matching."""
    start = source.index(f"function {name}(")
    depth = 0
    for pos in range(source.index("{", start), len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def run_node(template_name: str, driver_js: str) -> dict:
    """Evaluate the template's script, then `driver_js`, which must print
    exactly one JSON line. Returns that line, parsed."""
    if NODE is None:
        pytest.skip(
            "node executable not found; the JS-level regression is skipped "
            "(this repo's CI only provisions Python)"
        )

    harness = f"{DOM_STUB}\n{extract_inline_script(template_name)}\n{driver_js}\n"
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)

    assert proc.returncode == 0, f"{template_name}: node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])
