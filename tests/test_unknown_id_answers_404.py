"""An unknown id must answer 404, not 500.

Nearly every handler in `routes/` looks its row up with `db.get_or_404(...)`
inside a `try:` whose `except Exception:` returns a generic 500. Werkzeug's
`NotFound` is an `HTTPException`, and an `HTTPException` is an `Exception`, so
the blanket handler caught the very answer Flask was raising: `POST
/api/property/999999/check-status` reported "An internal error occurred" with
status 500, and the page routes flashed an error and bounced to the listing
surface instead of showing a not-found page.

Issue #136 fixed that one endpoint by re-raising `HTTPException` ahead of the
blanket handler. The same shape was in twenty-five other handlers across
`routes/api_routes.py` and `routes/main_routes.py`, all of them fixed the same
way. This module pins both halves of the contract: the response every affected
route gives for an id that does not exist, and — structurally — that a handler
added later cannot reintroduce the swallow.
"""

import ast
from pathlib import Path

import pytest

from app import create_app, db
from tests import setup_test_environment

# Nothing is seeded: every id below is absent from the database.
UNKNOWN_ID = 999999

# Every route in routes/ that looks a row up inside a try/except Exception.
# GET pages included: a missing land used to redirect with a flash, which
# tells a crawler (and a bookmark) that the page is fine.
AFFECTED_ROUTES = [
    ("POST", f"/api/land/{UNKNOWN_ID}/enrich"),
    ("POST", f"/api/analyze/property/{UNKNOWN_ID}/structured"),
    ("POST", f"/api/property/{UNKNOWN_ID}/analyze/structured"),
    ("POST", f"/api/analysis/generate/{UNKNOWN_ID}/openai"),
    ("GET", f"/api/analysis/compare/{UNKNOWN_ID}"),
    ("GET", f"/api/property/{UNKNOWN_ID}/analysis/compare"),
    ("POST", f"/api/enhance/description/{UNKNOWN_ID}"),
    ("POST", f"/api/land/{UNKNOWN_ID}/environment"),
    ("POST", f"/api/property/{UNKNOWN_ID}/environment"),
    ("POST", f"/api/analyze/property/{UNKNOWN_ID}"),
    ("POST", f"/api/land/{UNKNOWN_ID}/favorite"),
    ("POST", f"/api/property/{UNKNOWN_ID}/favorite"),
    ("POST", f"/api/property/{UNKNOWN_ID}/enrich"),
    ("POST", f"/api/land/{UNKNOWN_ID}/set-status"),
    ("POST", f"/api/property/{UNKNOWN_ID}/set-status"),
    ("POST", f"/api/land/{UNKNOWN_ID}/check-status"),
    ("GET", f"/api/land/{UNKNOWN_ID}/history"),
    ("GET", f"/properties/{UNKNOWN_ID}"),
    ("POST", f"/profiles/{UNKNOWN_ID}/travel/recalculate"),
    ("POST", f"/profiles/{UNKNOWN_ID}/score/recalculate"),
    ("POST", f"/profiles/{UNKNOWN_ID}/classification/recalculate"),
    ("POST", f"/properties/{UNKNOWN_ID}/set-status"),
    ("GET", f"/lands/{UNKNOWN_ID}"),
    ("GET", f"/land/{UNKNOWN_ID}/edit-environment"),
    ("POST", f"/land/{UNKNOWN_ID}/update-score"),
]


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize(
    "method,path", AFFECTED_ROUTES, ids=[f"{m} {p}" for m, p in AFFECTED_ROUTES]
)
def test_unknown_id_answers_404(client, method, path):
    """Every affected route: an id that does not exist is a 404."""
    response = client.open(path, method=method)

    assert response.status_code == 404, (
        f"{method} {path} answered {response.status_code}. An unknown id is a "
        "404: a blanket `except Exception` around get_or_404 must re-raise "
        "HTTPException instead of reporting a server fault."
    )


class TestPreviouslyBrokenEndpoints:
    """The two named in the report, asserted on their own.

    `check_property_status` (issue #136) is not here: it arrives with the PR
    that adds it, carrying its own coverage.
    """

    def test_set_property_status_unknown_id(self, client):
        response = client.post(
            f"/api/property/{UNKNOWN_ID}/set-status", json={"status": "removed"}
        )
        assert response.status_code == 404

    def test_check_land_status_unknown_id(self, client):
        response = client.post(f"/api/land/{UNKNOWN_ID}/check-status")
        assert response.status_code == 404


# --- structural guard -------------------------------------------------------
#
# The HTTP tests above only reach the routes that exist today. This walks the
# source so the next handler written in the same shape fails here rather than
# in production.

ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"

# Calls that raise an HTTPException rather than returning a value.
ABORTING_CALLS = {"abort", "get_or_404", "first_or_404", "one_or_404"}

# Catching either of these ahead of the blanket handler is the fix.
PASSTHROUGH_EXCEPTIONS = {"HTTPException", "NotFound"}

BROAD_EXCEPTIONS = {"Exception", "BaseException"}


def _call_name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _can_abort(nodes) -> bool:
    """True when this code can raise an HTTPException."""
    return any(
        isinstance(sub, ast.Call) and _call_name(sub.func) in ABORTING_CALLS
        for node in nodes
        for sub in ast.walk(node)
    )


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}  # a bare except catches everything
    caught = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return {_call_name(name) for name in caught}


def _swallowing_handlers(tree: ast.AST, source_name: str) -> list[str]:
    """Report every blanket handler that would turn an abort into a 500."""
    problems: list[str] = []

    def scan(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            elif isinstance(child, ast.Try) and _can_abort(child.body):
                for handler in child.handlers:
                    caught = _caught_names(handler)
                    if caught & PASSTHROUGH_EXCEPTIONS:
                        break  # re-raised before the blanket handler runs
                    if caught & BROAD_EXCEPTIONS:
                        problems.append(
                            f"{source_name}:{handler.lineno} in {function}(): "
                            f"except {'/'.join(sorted(caught))} swallows the "
                            f"HTTPException raised inside try (line {child.lineno})"
                        )
            scan(child, name)

    scan(tree, "<module>")
    return problems


def test_no_route_handler_swallows_httpexception():
    """No handler in routes/ turns an abort or get_or_404 into a 500."""
    problems: list[str] = []
    modules = sorted(ROUTES_DIR.glob("*.py"))
    assert modules, f"no route modules found under {ROUTES_DIR}"

    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        problems.extend(_swallowing_handlers(tree, module.name))

    assert not problems, (
        "A blanket `except Exception` is standing between get_or_404/abort and "
        "the client, so an unknown id answers 500 instead of 404. Add "
        "`except HTTPException: raise` ahead of it, or move the lookup out of "
        "the try:\n  " + "\n  ".join(problems)
    )
