"""One compiled copy of every template for the whole test session.

Why this exists. Nearly every test file builds its own application
(`create_app()` in a fixture), and a Flask application owns its own Jinja
environment, whose compiled-template cache starts empty. A test that renders
`/properties` therefore parsed, generated and byte-compiled `properties.html`
and its includes again, from nothing, every time: measured with cProfile on
`tests/test_status_line.py`, 15.1 s of the 19.4 s spent inside the 30 requests
was `jinja2.Environment.compile` (75 compiles, 0.2 s each). CI's `pytest` job
had grown from about 6 minutes (2026-08-20) to 19.5 against a 20-minute limit
for that reason, with no single slow file to blame (#687).

What it does. A bytecode cache, shared by every environment the session
creates, keeps the compiled code keyed by the template's name, file name and a
checksum of its source (Jinja's own bucket check), plus the environment
settings that change what the compiler emits. A template edited between two
tests -- or two environments that compile differently -- never meets the other's
code. Nothing here is imported by the application: production builds one
environment per process and gains nothing from it.

Scope. `Environment.compile` only; template rendering, globals, filters and
context processors are still per application, so a test still renders against
its own app. `render_template_string` and `from_string` do not go through the
loader and are not cached.
"""

from __future__ import annotations

import hashlib
import io

import flask
from jinja2 import BytecodeCache
from jinja2.bccache import Bucket


class SharedBytecodeCache(BytecodeCache):
    """Compiled templates held in memory, shared by every test's environment."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def get_bucket(self, environment, name, filename, source):  # noqa: ANN001
        # The base class keys on name + filename and checks the source's
        # checksum; add what else the compiler's output depends on so two
        # environments configured differently cannot share code.
        bucket = Bucket(
            environment,
            self._key(environment, name, filename),
            self.get_source_checksum(source),
        )
        self.load_bytecode(bucket)
        return bucket

    @staticmethod
    def _signature(environment) -> str:  # noqa: ANN001
        return "|".join(
            (
                repr(sorted(environment.extensions)),
                repr(
                    environment.autoescape
                    if not callable(environment.autoescape)
                    else "callable"
                ),
                repr(environment.trim_blocks),
                repr(environment.lstrip_blocks),
                repr(environment.keep_trailing_newline),
                repr(environment.newline_sequence),
                repr(environment.optimized),
                repr(environment.block_start_string),
                repr(environment.variable_start_string),
                repr(environment.comment_start_string),
            )
        )

    def _key(self, environment, name, filename) -> str:  # noqa: ANN001
        parts = [self._signature(environment), name]
        if filename is not None:
            parts.append(filename)
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def load_bytecode(self, bucket: Bucket) -> None:
        data = self._store.get(bucket.key)
        if data is not None:
            bucket.load_bytecode(io.BytesIO(data))

    def dump_bytecode(self, bucket: Bucket) -> None:
        buf = io.BytesIO()
        bucket.write_bytecode(buf)
        self._store[bucket.key] = buf.getvalue()


SHARED = SharedBytecodeCache()

_original = None


def install() -> None:
    """Give every Flask application created from now on the shared cache."""

    global _original
    if _original is not None:
        return
    _original = flask.Flask.create_jinja_environment

    def create_jinja_environment(self):
        environment = _original(self)
        environment.bytecode_cache = SHARED
        return environment

    flask.Flask.create_jinja_environment = create_jinja_environment  # type: ignore[method-assign]


def uninstall() -> None:
    global _original
    if _original is not None:
        flask.Flask.create_jinja_environment = _original  # type: ignore[method-assign]
        _original = None
