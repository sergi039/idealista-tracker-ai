"""One subscription on screen: routes, their refusals, and auto-routing.

The SQLite half of migration 025's story: the Python boundary
(`canonical_profile` at the end of `resolve_profile`, `route_profile` as the
one writer of `routed_to`, the born-routed auto-creation). The PostgreSQL
trigger — the guarantee that covers raw SQL too — is pinned against a real
server in tests/test_postgres_migrations.py; this file says so instead of
pretending SQLite runs it.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.search_profile_service import SearchProfileService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _profile(name, **overrides):
    values = dict(name=name, is_active=True)
    values.update(overrides)
    row = SearchProfile(**values)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _listing(profile_id, **overrides):
    values = dict(
        source_email_id=f"route-test:{next(_SEQ)}",
        title=f"Row {next(_SEQ)}",
        price=100000,
        area=200,
        search_profile_id=profile_id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestRouteProfile:
    def test_a_route_moves_present_and_hides_the_stub(self, app):
        target = _profile("Galicia · costa")
        stub = _profile("Galicia Rias Altas ready")
        kept = _listing(stub.id)
        outcome = SearchProfileService.route_profile(stub.id, target.id)
        assert outcome == {"status": "ok", "moved": 1}
        assert stub.routed_to == target.id
        assert stub.is_hidden is True
        assert kept.search_profile_id == target.id

    @pytest.mark.parametrize(
        "make_case, reason",
        [
            (lambda t, s: (s.id, s.id), "self_route"),
            (lambda t, s: (s.id, 424242), "no_such_profile"),
        ],
    )
    def test_nonsense_routes_are_refused(self, app, make_case, reason):
        target = _profile("Target")
        stub = _profile("Stub")
        source_id, target_id = make_case(target, stub)
        outcome = SearchProfileService.route_profile(source_id, target_id)
        assert outcome["status"] == "refused"
        assert outcome["reason"] == reason

    def test_the_catch_all_never_routes_in_either_direction(self, app):
        default = _profile("Default", is_default=True)
        other = _profile("Other")
        assert (
            SearchProfileService.route_profile(default.id, other.id)["reason"]
            == "catch_all_never_routes"
        )
        assert (
            SearchProfileService.route_profile(other.id, default.id)["reason"]
            == "catch_all_never_routes"
        )

    def test_chains_are_refused_forward_and_backward(self, app):
        a = _profile("A")
        b = _profile("B")
        c = _profile("C")
        assert SearchProfileService.route_profile(a.id, b.id)["status"] == "ok"
        # Forward: routing anything ONTO a routed stub would chain.
        assert (
            SearchProfileService.route_profile(c.id, a.id)["reason"]
            == "target_is_routed"
        )
        # Backward: routing a profile something already routes TO would
        # chain from the other side — the operator re-points a->? first.
        assert (
            SearchProfileService.route_profile(b.id, c.id)["reason"]
            == "source_is_a_route_target"
        )

    def test_a_pattern_carrier_cannot_become_a_stub(self, app):
        target = _profile("Target")
        carrier = _profile("Carrier", auto_route_from_pattern="^Galicia ")
        outcome = SearchProfileService.route_profile(carrier.id, target.id)
        assert outcome["reason"] == "source_carries_a_pattern"


class TestCanonicalResolution:
    def test_canonical_profile_follows_one_hop(self, app):
        target = _profile("Target")
        stub = _profile("Stub", routed_to=None)
        stub.routed_to = target.id
        db.session.commit()
        assert SearchProfileService.canonical_profile(stub).id == target.id
        assert SearchProfileService.canonical_profile(target).id == target.id
        assert SearchProfileService.canonical_profile(None) is None

    def test_resolve_profile_lands_on_the_route_target(self, app):
        """A matcher resolves the stub; the listing must land on the target.

        The resolution layer keeps answering the #102 question (which saved
        search is this); the route answers where its listings live.
        """
        target = _profile("Galicia · costa")
        stub = _profile(
            "Portal stub",
            email_matchers=[{"pattern": "yaencontre", "priority": 10}],
        )
        SearchProfileService.route_profile(stub.id, target.id)
        resolved = SearchProfileService.resolve_profile(
            "Nuevas casas", "alerta de yaencontre en Bueu"
        )
        assert resolved.id == target.id

    def test_an_auto_created_profile_is_born_routed_and_hidden(self, app):
        """The four Galicia alerts still to deliver must not each put a chip
        back on the screen at their first email."""
        target = _profile("Galicia · costa", auto_route_from_pattern=r"^Galicia\s")
        created = SearchProfileService.get_or_create_profile_by_name(
            "Galicia Rias Baixas costa - ready 350k"
        )
        assert created is not None
        stub = db.session.get(SearchProfile, created.id)
        # get_or_create returns what it made; the row itself is the claim.
        raw = SearchProfile.query.filter_by(
            name="Galicia Rias Baixas costa - ready 350k"
        ).one()
        assert raw.routed_to == target.id
        assert raw.is_hidden is True
        assert stub is not None

    def test_a_name_nobody_claimed_is_born_ordinary(self, app):
        _profile("Galicia · costa", auto_route_from_pattern=r"^Galicia\s")
        created = SearchProfileService.get_or_create_profile_by_name(
            "Asturias oriente casas"
        )
        raw = SearchProfile.query.filter_by(name="Asturias oriente casas").one()
        assert raw.routed_to is None
        assert raw.is_hidden is False
        assert created is not None

    def test_a_broken_pattern_never_kills_mail_routing(self, app):
        _profile("Bad", auto_route_from_pattern="([unclosed")
        created = SearchProfileService.get_or_create_profile_by_name(
            "Galicia something"
        )
        raw = SearchProfile.query.filter_by(name="Galicia something").one()
        assert raw.routed_to is None
        assert created is not None

    def test_a_route_whose_target_vanished_keeps_the_stub(self, app):
        target = _profile("Target")
        stub = _profile("Stub")
        stub.routed_to = target.id
        db.session.commit()
        target_id = target.id
        db.session.delete(target)
        # SQLite enforces no FK by default; production PostgreSQL would
        # refuse the delete — this asserts only the reader's fail-safe.
        db.session.commit()
        with patch.object(
            SearchProfileService, "_auto_route_target_for", return_value=None
        ):
            resolved = SearchProfileService.canonical_profile(stub)
        assert resolved.id == stub.id
        assert stub.routed_to == target_id
