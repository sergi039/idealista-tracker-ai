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
        """Parsed, not grepped.

        The review's finding: a regex over `"reason": "..."` sees only inline
        string literals, so `NEW_REASON = "target_archived"` followed by
        `return {"reason": NEW_REASON}` would leave this green while the CLI
        printed "no explanation is recorded for this reason". The AST walk
        below cannot know that reason's value either — but it can SEE that one
        exists and fail, which turns a blind spot into a loud instruction.
        """
        import ast
        from pathlib import Path

        module = ast.parse(Path("services/search_profile_service.py").read_text())
        routers = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef) and node.name == "route_profile"
        ]
        assert len(routers) == 1, "route_profile() was renamed or duplicated"

        literal, dynamic = set(), []
        for node in ast.walk(routers[0]):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "reason"):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literal.add(value.value)
                else:
                    dynamic.append(f"line {getattr(value, 'lineno', '?')}")

        assert literal, "found no refusal reasons at all — did the parse break?"
        assert not dynamic, (
            "route_profile() builds a refusal reason dynamically at "
            + ", ".join(dynamic)
            + "; this test cannot read its value, so add the reason to "
            "utils.route_profile.REFUSALS by hand and make the value a literal"
        )
        assert not literal - set(REFUSALS), (
            "the service can refuse with reasons the CLI cannot explain: "
            + ", ".join(sorted(literal - set(REFUSALS)))
        )
