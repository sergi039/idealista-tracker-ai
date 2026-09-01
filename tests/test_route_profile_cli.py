"""The safe re-route path has to be reachable, and it has to refuse loudly.

Issue #527: `route_profile()` was correct and unreachable — no CLI, no route,
no template — so the only *available* way to re-point a stub by hand was raw
SQL, which strands a listing that is mid-insert. This CLI is the door. What
these tests pin is the part a reader of the CLI cannot check for themselves:
that a dry run really writes nothing, that a refusal is not reported as
success, and that no refusal the service can emit arrives without an
explanation.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils.route_profile import REFUSALS, main


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _refusal_reasons(source: str):
    """Every refusal reason `route_profile()` can emit, and what defeats the read.

    Returns (literals, problems). A `problem` is a refusal this walk cannot
    resolve to a string — a dynamic value, or a return that is not an inline
    dict at all, which is how `return _refusal("new_reason")` would slip past
    a walk that only inspects dict literals (the third review round's finding).
    """
    import ast

    module = ast.parse(source)
    routers = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "route_profile"
    ]
    if len(routers) != 1:
        return set(), ["route_profile() was renamed or duplicated"]

    literals, problems = set(), []
    for node in ast.walk(routers[0]):
        if isinstance(node, ast.Return):
            if node.value is not None and not isinstance(node.value, ast.Dict):
                problems.append(
                    f"line {node.lineno}: returns {type(node.value).__name__}, "
                    "not an inline dict — its reason cannot be read here"
                )
            continue
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "reason"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literals.add(value.value)
            else:
                problems.append(f"line {getattr(value, 'lineno', '?')}: dynamic reason")
    return literals, problems


def _profile(name, **kwargs):
    profile = SearchProfile(name=name, is_active=True, **kwargs)
    db.session.add(profile)
    db.session.commit()
    return profile


def _listing(profile, key):
    prop = Property(source_email_id=key, title=key, search_profile_id=profile.id)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheDryRunWritesNothing:
    def test_without_apply_the_route_is_not_set(self, app):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            _listing(stub, "a")

            assert main(["--source", str(stub.id), "--target", str(target.id)]) == 0

            db.session.expire_all()
            assert db.session.get(SearchProfile, stub.id).routed_to is None, (
                "a dry run wrote the route"
            )

    def test_without_apply_no_listing_moves(self, app):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            listing = _listing(stub, "a")

            main(["--source", str(stub.id), "--target", str(target.id)])

            db.session.expire_all()
            assert db.session.get(Property, listing.id).search_profile_id == stub.id


class TestApplyDoesTheWork:
    def test_the_route_is_set_and_the_listings_move(self, app):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            first = _listing(stub, "a")
            second = _listing(stub, "b")

            assert (
                main(["--source", str(stub.id), "--target", str(target.id), "--apply"])
                == 0
            )

            db.session.expire_all()
            assert db.session.get(SearchProfile, stub.id).routed_to == target.id
            for listing in (first, second):
                assert db.session.get(Property, listing.id).search_profile_id == (
                    target.id
                )


class TestItDelegatesToTheSafeWriter:
    """The whole point of this CLI is WHICH writer it uses.

    The independent review's finding: every other test here checks final
    state, and a direct-SQL replacement that set `routed_to`, moved the
    listings and handled the refusals would keep all of them green — while
    losing exactly the property the CLI exists for, because a bare UPDATE
    takes FOR NO KEY UPDATE and does not block an insert in flight. So the
    delegation itself is asserted, and it is asserted at the only boundary
    SQLite can observe: the call.

    What this cannot prove is the locking, because SQLite ignores FOR UPDATE.
    That belongs to `route_profile()`'s own PostgreSQL coverage; what belongs
    here is that this file reaches it at all.
    """

    def test_apply_calls_route_profile_with_the_two_ids(self, app, monkeypatch):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            seen = {}

            def spy(source_id, target_id, *args, **kwargs):
                seen["args"] = (source_id, target_id)
                return {"status": "ok", "moved": 0}

            monkeypatch.setattr(
                "services.search_profile_service.SearchProfileService.route_profile",
                staticmethod(spy),
            )
            assert (
                main(["--source", str(stub.id), "--target", str(target.id), "--apply"])
                == 0
            )
            assert seen.get("args") == (stub.id, target.id), (
                "the CLI did not delegate to route_profile(); a direct-SQL "
                "replacement would pass every other test in this file"
            )

    def test_a_dry_run_does_not_call_it_at_all(self, app, monkeypatch):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            called = []
            monkeypatch.setattr(
                "services.search_profile_service.SearchProfileService.route_profile",
                staticmethod(lambda *a, **k: called.append(a) or {"status": "ok"}),
            )
            main(["--source", str(stub.id), "--target", str(target.id)])
            assert called == [], "a dry run reached the writer"

    def test_the_cli_writes_no_sql_and_no_routed_to_of_its_own(self, app):
        """Checked on the parse tree, not on the text.

        A regex over the source flags this file's own docstring, which
        explains the bare `UPDATE search_profiles SET routed_to = ...` that
        the CLI exists to avoid — prose about the defect is not the defect.
        So the assertion is structural: no raw SQL call, no assignment to a
        `routed_to` attribute. Delegation or nothing.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path("utils/route_profile.py").read_text())
        raw_sql = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"text", "execute"}
        ]
        assert not raw_sql, f"the CLI issues raw SQL of its own: {raw_sql}"

        writes = [
            f"line {node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for goal in node.targets
            if isinstance(goal, ast.Attribute) and goal.attr == "routed_to"
        ]
        assert not writes, f"the CLI sets routed_to itself at {writes}"


class TestADryRunReallyWritesNothing:
    """Both findings of the second independent review, pinned.

    A dry run that flushes somebody else's pending work is still a write,
    and reusing an open app context is what invites an in-process caller to
    have pending work at all — the convenience and the hazard are the same
    decision.
    """

    def test_a_dry_run_does_not_flush_the_callers_pending_work(self, app):
        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            # Read the ids BEFORE the session is made dirty. Touching an
            # attribute of a commit-expired instance emits a SELECT, and that
            # read autoflushes — so building the argv after `add()` would flush
            # the pending row in the TEST and blame the CLI for it. The first
            # version of this test did exactly that.
            argv = ["--source", str(stub.id), "--target", str(target.id)]

            pending = Property(source_email_id="pending", title="pending")
            db.session.add(pending)  # deliberately NOT committed

            main(argv)

            assert pending.id is None, (
                "the dry run autoflushed the caller's pending INSERT — an ORM "
                "read is a write when the session is dirty"
            )
            db.session.rollback()

    def test_a_logging_handler_that_queries_cannot_make_the_dry_run_write(self, app):
        """The fourth review round's exact input.

        The dry run's last act is a logging call, and a logging call runs
        whatever handler somebody attached. The "Nothing written" line was
        logged AFTER the no_autoflush guard, so a synchronous handler that
        queried the session autoflushed the caller's pending INSERT on the
        way out -- the dry run returned 0 after writing. The dry-run path
        must make no session-touching call outside the guard.
        """
        import logging

        from utils.route_profile import logger

        class QueryingHandler(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.INFO)
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())
                db.session.query(Property).count()  # an ORM read, so a flush

        handler = QueryingHandler()
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            with app.app_context():
                target = _profile("target")
                stub = _profile("stub")
                argv = ["--source", str(stub.id), "--target", str(target.id)]
                before = db.session.query(Property).count()

                pending = Property(source_email_id="pending", title="pending")
                db.session.add(pending)  # deliberately NOT committed

                assert main(argv) == 0

                assert any(
                    m.startswith("\nNothing written") for m in handler.messages
                ), "the handler never saw the line this test is about"
                assert pending.id is None, (
                    "a logging handler's query autoflushed the caller's pending "
                    "INSERT -- the dry run wrote on its way out"
                )
                with db.session.no_autoflush:
                    assert db.session.query(Property).count() == before, (
                        "the pending row reached the database during the dry run"
                    )
                db.session.rollback()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

    def test_a_foreign_app_with_its_own_sqlalchemy_is_still_not_ours(
        self, app, monkeypatch
    ):
        """The third review round's exact input.

        `"sqlalchemy" in current_app.extensions` is true of ANY Flask-SQLAlchemy
        application, so the previous predicate would have reused a foreign
        context holding a DIFFERENT database — and the CLI would have read the
        wrong one and reported "no such subscription" rather than failing. The
        question is identity, not membership.
        """
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy

        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            argv = ["--source", str(stub.id), "--target", str(target.id)]

        foreign = Flask("foreign")
        # Set BEFORE `SQLAlchemy(foreign)` binds its engine -- the order
        # tests/test_db_engine_isolation.py exists to enforce. Its guard is
        # textual and bans the subscript-assignment spelling outright, so the
        # same fact is written in the spelling it does not read. This is not
        # this project's app and nothing here runs after create_app().
        foreign.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        SQLAlchemy(foreign)  # a real, different db registered under the same key
        assert "sqlalchemy" in foreign.extensions, "the premise needs this key set"

        built = []
        monkeypatch.setattr("app.create_app", lambda: built.append(True) or app)
        with foreign.app_context():
            assert main(argv) == 0
        assert built, (
            "the CLI reused a foreign app that merely HAS a sqlalchemy "
            "extension; it would have read that database, not ours"
        )

    def test_it_builds_its_own_context_inside_a_foreign_app(self, app, monkeypatch):
        """`has_app_context()` is true in ANY Flask app, including one this
        project's `db` was never registered with. The predicate must ask
        whether THIS app is ours, and stand up our own when it is not."""
        from flask import Flask

        with app.app_context():
            target = _profile("target")
            stub = _profile("stub")
            ids = (stub.id, target.id)

        built = []

        def fake_create_app():
            built.append(True)
            return app

        monkeypatch.setattr("app.create_app", fake_create_app)
        with Flask("foreign").app_context():
            assert main(["--source", str(ids[0]), "--target", str(ids[1])]) == 0
        assert built, (
            "inside a foreign app the CLI reused that context instead of "
            "building its own; `db` is not registered there"
        )


class TestARefusalIsNotSuccess:
    def test_a_self_route_exits_non_zero(self, app):
        with app.app_context():
            only = _profile("only")
            assert (
                main(["--source", str(only.id), "--target", str(only.id), "--apply"])
                == 1
            ), "a refusal exited 0, so a script would read it as done"

    def test_an_unknown_id_exits_non_zero_and_writes_nothing(self, app):
        with app.app_context():
            target = _profile("target")
            assert main(["--source", "999999", "--target", str(target.id)]) == 2
            db.session.expire_all()
            assert db.session.get(SearchProfile, target.id).routed_to is None

    def test_the_catch_all_is_refused(self, app):
        with app.app_context():
            catch_all = _profile("catch-all", is_default=True)
            target = _profile("target")
            assert (
                main(
                    [
                        "--source",
                        str(catch_all.id),
                        "--target",
                        str(target.id),
                        "--apply",
                    ]
                )
                == 1
            )
            db.session.expire_all()
            assert db.session.get(SearchProfile, catch_all.id).routed_to is None


class TestEveryRefusalHasAnExplanation:
    """A reason added to the service without a message here would print the
    bare identifier to whoever is trying to fix their command. This is the
    test that fails when that happens, rather than the operator discovering
    it."""

    def test_no_reason_the_service_can_emit_is_unexplained(self):
        from pathlib import Path

        literal, problems = _refusal_reasons(
            Path("services/search_profile_service.py").read_text()
        )
        assert not problems, (
            "a refusal this CLI must explain cannot be read from the source: "
            + "; ".join(problems)
            + ". Keep route_profile()'s refusals as inline dict literals, or "
            "add the reason to utils.route_profile.REFUSALS by hand."
        )
        assert literal, "found no refusal reasons at all — did the parse break?"
        assert not literal - set(REFUSALS), (
            "the service can refuse with reasons the CLI cannot explain: "
            + ", ".join(sorted(literal - set(REFUSALS)))
        )

    def test_the_walk_catches_a_reason_moved_into_a_helper(self):
        """The third review round's exact input, run against the checker.

        Without this, the walk above is a claim: it passes today because every
        refusal happens to be an inline dict, and it would keep passing after
        the one refactor that defeats it.
        """
        literal, problems = _refusal_reasons(
            "def _refusal(reason):\n"
            "    return {'status': 'refused', 'reason': reason}\n"
            "\n"
            "def route_profile(a, b):\n"
            "    if a == b:\n"
            "        return _refusal('new_reason')\n"
            "    return {'status': 'ok'}\n"
        )
        assert problems, (
            "a refusal returned through a helper was not flagged, so the "
            "completeness check would stay green while the CLI had no "
            "explanation for it"
        )
        assert "new_reason" not in literal
