"""The shared template cache of the test session (#687) keeps its promises.

The cache exists because every test builds its own Flask application, and with
it a Jinja environment that compiled every template from nothing
(tests/jinja_cache.py). A cache that serves a stale template, or code compiled
for another environment's settings, would make a test pass against a page it
did not render, so the three promises are pinned here: the second environment
does not compile, an edited template does, and a differently configured
environment never meets the other's code.
"""

from unittest.mock import patch

import jinja2
import pytest

from tests.jinja_cache import SharedBytecodeCache


def _env(source: dict[str, str], cache: SharedBytecodeCache, **options):
    return jinja2.Environment(
        loader=jinja2.DictLoader(source), bytecode_cache=cache, **options
    )


@pytest.fixture
def cache():
    return SharedBytecodeCache()


def _compiles(env: jinja2.Environment, name: str) -> int:
    with patch.object(env, "_generate", wraps=env._generate) as generate:
        env.get_template(name)
    return generate.call_count


def test_a_second_environment_does_not_compile_the_same_template(cache):
    templates = {"page.html": "<p>{{ x }}</p>"}
    first = _env(templates, cache)
    second = _env(templates, cache)

    assert _compiles(first, "page.html") == 1
    assert _compiles(second, "page.html") == 0
    assert second.get_template("page.html").render(x=3) == "<p>3</p>"


def test_an_edited_template_is_compiled_again_and_renders_the_edit(cache):
    _env({"page.html": "old {{ x }}"}, cache).get_template("page.html")
    edited = _env({"page.html": "new {{ x }}"}, cache)

    assert _compiles(edited, "page.html") == 1
    assert edited.get_template("page.html").render(x=1) == "new 1"


def test_environments_that_compile_differently_do_not_share_code(cache):
    templates = {"page.html": "{{ x }}"}
    plain = _env(templates, cache, autoescape=False)
    escaping = _env(templates, cache, autoescape=True)

    assert plain.get_template("page.html").render(x="<b>") == "<b>"
    # The escaping environment must compile its own copy, not reuse the one
    # above, or it would render the unescaped text.
    assert _compiles(escaping, "page.html") == 1
    assert escaping.get_template("page.html").render(x="<b>") == "&lt;b&gt;"
