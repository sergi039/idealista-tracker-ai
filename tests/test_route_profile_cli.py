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
        import re
        from pathlib import Path

        source = Path("services/search_profile_service.py").read_text()
        emitted = set(re.findall(r'"reason": "([a-z_]+)"', source))
        assert emitted, "found no refusal reasons at all — did the parse break?"
        assert not emitted - set(REFUSALS), (
            "route_profile() can refuse with reasons the CLI cannot explain: "
            + ", ".join(sorted(emitted - set(REFUSALS)))
        )
