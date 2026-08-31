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

    def test_a_routed_source_cannot_be_re_pointed(self, app):
        """The review's split reproduction: re-pointing S from B to C left
        S's history on B while its future went to C — "ok, moved 0" over a
        silent fork."""
        b = _profile("B")
        c = _profile("C")
        s = _profile("S")
        _listing(s.id)
        assert SearchProfileService.route_profile(s.id, b.id)["status"] == "ok"
        outcome = SearchProfileService.route_profile(s.id, c.id)
        assert outcome["status"] == "refused"
        assert outcome["reason"] == "source_already_routed"

    def test_the_paste_door_cannot_land_a_listing_on_a_stub(self, app):
        """The review's SQLite reproduction: build_property took the
        profile id verbatim, and without the PostgreSQL trigger a stale
        form naming a stub kept the row there."""
        from services.fotocasa_import import build_property

        target = _profile("Target")
        stub = _profile("Stub")
        SearchProfileService.route_profile(stub.id, target.id)
        prop = build_property(
            {
                "url": "https://www.fotocasa.es/es/comprar/vivienda/x/9/d",
                "listing_id": 9,
                "title": "Pasted",
                "price": 1,
                "area": 100,
            },
            profile_id=stub.id,
        )
        db.session.add(prop)
        db.session.commit()
        assert prop.search_profile_id == target.id

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


class TestABlankPatternClaimsNothing:
    """`auto_route_from_pattern = ''` adopted every new subscription.

    From the #502 review, reproduced: `''` survives the query's
    `isnot(None)`, and `re.search("", anything)` matches, so one profile
    carrying an empty string silently took ownership of every profile the
    ingester auto-created — born routed and hidden, no chip, no notice.

    Guarded on the read side because that is the only side there is: the
    column has no UI writer anywhere in the tree, so hand SQL is its one
    interface, and CLAUDE.md names direct SQL a supported workflow. Nothing
    here refuses an over-broad pattern in general — `.` would match
    everything too — only the blank that means "unset".
    """

    # `" "` is the one that carries weight besides `""`: a single space is a
    # legal regex that matches almost every real subscription name, so it is
    # the whitespace case that would actually adopt. `"\t"` matches nothing
    # here and is included only as the third shape the column can hold.
    @pytest.mark.parametrize("blank", ["", " ", "\t"])
    def test_a_blank_pattern_adopts_nobody(self, app, blank):
        carrier = _profile("Carrier", auto_route_from_pattern=blank)
        db.session.commit()

        created = SearchProfileService.get_or_create_profile_by_name(
            "Asturias oriente casas"
        )
        db.session.commit()

        assert created.routed_to is None, (
            f"a {blank!r} pattern adopted a new subscription: it was born "
            "routed and hidden with no chip and no notice"
        )
        assert created.is_hidden is not True
        assert carrier.id != created.id

    def test_a_real_pattern_still_adopts(self, app):
        """The positive control. Without it the test above passes on a
        routing path that never fires at all."""
        carrier = _profile("Galicia costa", auto_route_from_pattern="^Galicia ")
        db.session.commit()

        created = SearchProfileService.get_or_create_profile_by_name(
            "Galicia norte casas"
        )
        db.session.commit()

        assert created.routed_to == carrier.id
        assert created.is_hidden is True

    def test_routing_a_blank_carrier_away_does_not_die_on_the_check(self, app):
        """Symptom B: `route_profile` answered `{"status": "ok", "moved": 0}`
        and then the CHECK refused the write at flush, because the column was
        `''` rather than NULL while `ck_search_profiles_stub_has_no_pattern`
        compares against NULL."""
        carrier = _profile("Carrier", auto_route_from_pattern="")
        target = _profile("Target")
        db.session.commit()

        result = SearchProfileService.route_profile(carrier.id, target.id)
        db.session.commit()

        assert result["status"] == "ok"
        assert carrier.routed_to == target.id
        assert carrier.auto_route_from_pattern is None, (
            "a blank pattern was left on a row that is now a routed stub, "
            "which the CHECK refuses"
        )
