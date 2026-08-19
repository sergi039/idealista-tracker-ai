"""Every derived answer names the set it is about (UNIVERSE-001 in #265).

Five surfaces in this repository answer questions derived from `properties`,
over five different populations. The decision recorded in #410 is that those
populations are legitimately different and must **not** be unified — a
selected set, an inventory aggregate, a pool relative to one subject, a global
exact-equality class and an operational work queue are five questions, and
forcing one "all" on them would make four of them answer something nobody
asked. What was missing is that none of them *said* which set it used, so two
of them disagreeing read as a fact about the listings rather than as a
difference of scope. That is how 38 of 87 municipality drill-downs came to
contradict the rows above them (#417).

So what is pinned here is the disclosure, surface by surface: the population,
what is in it that a reader would not expect, what was held back, and what a
derived number was adjusted for.

Each assertion is written so that deleting the disclosure makes it red, and —
where the disclosure is a rendered line — so that a page which failed to
render cannot pass it. `routes/main_routes.py` turns a template error into a
flash and a redirect, and a test that only looked for the absence of a string
would be green on both.
"""

import re
from decimal import Decimal

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402


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


def _profiles():
    live = SearchProfile(name="Land at Norte", is_active=True)
    retired = SearchProfile(name="Legacy Lands", is_active=False)
    hidden = SearchProfile(name="Solares Norte", is_active=True, is_hidden=True)
    db.session.add_all([live, retired, hidden])
    db.session.commit()
    return live, retired, hidden


def _listing(slug, profile_id=None, **kwargs):
    prop = Property(
        source_email_id=f"pop_{slug}",
        title=f"{slug} listing",
        municipality=kwargs.pop("municipality", "Gijón"),
        search_profile_id=profile_id,
        listing_status=kwargs.pop("listing_status", "active"),
        price=kwargs.pop("price", 200000),
        area=kwargs.pop("area", 1000),
        **kwargs,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheMunicipalityTableNamesItsInventory:
    """`/municipalities` spans every subscription and said so nowhere.

    On production 2026-08-19 that meant 772 listings — 461 from 10 live
    subscriptions and 311 from 4 retired ones — under a header reading
    "87 · 772 listings", which is the owner's live searches to anyone who has
    not read the route.
    """

    @pytest.fixture
    def inventory(self, app):
        live, retired, hidden = _profiles()
        _listing("live_a", live.id)
        _listing("live_b", live.id)
        _listing("retired_a", retired.id)
        _listing("retired_b", retired.id)
        _listing("retired_c", retired.id)
        _listing("hidden_a", hidden.id)
        _listing("orphan", None)
        _listing("gone", live.id, listing_status="removed")
        return {"live": live.id, "retired": retired.id, "hidden": hidden.id}

    def _scope_line(self, client, query=""):
        response = client.get(f"/municipalities{query}")
        assert response.status_code == 200, "the page did not render"
        body = response.get_data(as_text=True)
        # Asserted, not searched for: a template error redirects, and the page
        # it lands on has no scope line either.
        assert 'id="municipalities-scope"' in body
        match = re.search(r'id="municipalities-scope">(.*?)</div>', body, re.S)
        assert match
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()

    def test_the_composition_is_on_the_page(self, client, inventory):
        line = self._scope_line(client)
        assert "3 subscriptions holding 7 listings" in line
        # Subscriptions of each kind, with their listings in brackets. The
        # delisted row is outside the default aggregate and outside these
        # counts with it.
        assert "1 live (2)" in line
        assert "1 retired (3)" in line
        assert "1 hidden (1)" in line
        assert "1 listing with no subscription" in line

    def test_the_line_follows_the_page_it_describes(self, client, inventory):
        assert "removed and sold excluded" in self._scope_line(client)
        archived = self._scope_line(client, "?archived=on")
        assert "removed and sold included" in archived
        # The delisted row is in the aggregate now, so it is in the mix too.
        assert "1 live (3)" in archived
        assert "favorites only" in self._scope_line(client, "?favorites=on")

    def test_the_ratio_says_what_it_is_a_ratio_of(self, client, inventory):
        body = client.get("/municipalities").get_data(as_text=True)
        assert 'id="municipalities-basis"' in body
        assert "unadjusted median" in body
        assert "not a size-adjusted price" in body

    def test_both_languages_carry_the_disclosure(self, client, inventory):
        """An English-only assertion passes on a template that hardcoded it."""
        with client.session_transaction() as session:
            session["language"] = "es"
        body = client.get("/municipalities").get_data(as_text=True)
        assert "todos los anuncios almacenados" in body
        assert "1 activa (2)" in body
        assert "1 retirada (3)" in body
        assert "mediana sin ajustar" in body


class TestTheMixReadsTheFlagsTheWayTheRestOfTheAppDoes:
    """The classification has to agree with `SearchProfileService`'s clauses.

    `visible_clause()` is `is_hidden.isnot(True)` and `hidden_clause()` is
    `is_hidden.is_(True)`, written as two clauses precisely so they stay each
    other's complement. `subscription_mix` asks the clause rather than reading
    the column, and what that buys is agreement by construction rather than by
    coincidence.
    """

    def test_it_classifies_hidden_the_way_the_clause_does(self, app):
        """One matrix through both readings, the way `tests/test_advertiser.py`
        runs one through the badge and the SQL beside it.

        What this cannot construct is the case `visible_clause` was actually
        written for -- `is_hidden` NULL. The column is `NOT NULL` in the model,
        so the ORM and raw SQL both refuse it, and a test that pretended
        otherwise would be theatre. The clause's guard is defensive against a
        schema this repository does not have; what is observable, and pinned
        here, is that the two readings select the same rows for every flag
        combination the schema does allow.
        """
        from services.population import subscription_mix
        from services.search_profile_service import SearchProfileService

        made = {}
        for label, active, hidden in (
            ("live", True, False),
            ("retired", False, False),
            ("hidden_live", True, True),
            ("hidden_retired", False, True),
        ):
            profile = SearchProfile(name=label, is_active=active, is_hidden=hidden)
            db.session.add(profile)
            db.session.commit()
            made[label] = profile.id

        mix = subscription_mix({profile_id: 1 for profile_id in made.values()})
        by_clause = {
            profile_id
            for (profile_id,) in db.session.query(SearchProfile.id).filter(
                SearchProfileService.hidden_clause()
            )
        }

        assert by_clause == {made["hidden_live"], made["hidden_retired"]}
        # Hidden wins over active, and each subscription is counted once, so
        # the kinds sum to the subscriptions there are.
        assert (mix.active, mix.retired, mix.hidden) == (1, 1, 2)
        assert mix.subscriptions == len(made)
        assert mix.listings == len(made)

    def test_an_id_naming_no_subscription_is_its_own_kind(self, app):
        """Not folded into any of the three: "nobody knows what this was" is
        not a fourth flavour of subscription, and this app has no delete
        route, so such a row can only arrive from outside it."""
        from services.population import subscription_mix

        mix = subscription_mix({424242: 3})

        assert (mix.unknown, mix.listings_unknown) == (1, 3)
        assert (mix.active, mix.retired, mix.hidden) == (0, 0, 0)
        assert mix.listings == 3
        assert mix.is_mixed is True


class TestTheJsonApiNamesItsPage:
    """`count` is the size of the page, and was the only number in the payload.

    Measured 2026-08-19: the bare endpoint and `profile_id=all` both answered
    `count: 0` while bare `/properties` showed 461 listings — the same request
    read two ways, with nothing in the payload able to say which.
    """

    def test_the_scope_block_separates_the_answer_from_the_page(self, client, app):
        live, _, _ = _profiles()
        for index in range(5):
            _listing(f"api_{index}", live.id)

        payload = client.get(f"/api/properties?profile_id={live.id}&limit=2").get_json()

        assert payload["count"] == 2
        scope = payload["scope"]
        assert scope["total"] == 5
        assert scope["returned"] == 2
        assert scope["truncated"] is True
        assert scope["cap"] == 2
        assert scope["basis"]
        assert scope["subscriptions"]["active"] == 1
        assert scope["subscriptions"]["listings_active"] == 5
        assert scope["population"] == "one_subscription"

    def test_a_spelling_the_parser_drops_is_disclosed(self, client, app):
        """`profile_id=all` is read as omission, and now says so.

        The contract is deliberately unchanged (#410): `type=int` cannot be
        redefined safely here. What changed is that a caller can tell the
        difference between "you asked for the default" and "we could not read
        what you asked for".
        """
        live, _, _ = _profiles()
        _listing("api_live", live.id)

        payload = client.get("/api/properties?profile_id=all").get_json()

        scope = payload["scope"]
        assert scope["profile_id_requested"] == "all"
        assert scope["profile_id_applied"] != "all"
        assert scope["profile_id_source"] == "unrecognized"
        assert any("not a subscription id" in note for note in scope["notes"])

    def test_omission_and_a_bad_spelling_are_told_apart(self, client, app):
        """Two states a single boolean would collapse, which is the gap #410
        names: a caller that sent nothing gets the default on purpose, and a
        caller that sent `all` got it by accident."""
        _profiles()

        assert (
            client.get("/api/properties").get_json()["scope"]["profile_id_source"]
            == "omitted"
        )
        assert client.get("/api/properties").get_json()["scope"]["notes"] == []

    def test_a_real_id_carries_no_note(self, client, app):
        live, _, _ = _profiles()
        _listing("api_only", live.id)

        scope = client.get(f"/api/properties?profile_id={live.id}").get_json()["scope"]

        assert scope["notes"] == []
        assert scope["profile_id_source"] == "requested"
        assert scope["profile_id_applied"] == live.id


class TestTheCoordinateClassNamesItsCap:
    """The list is capped at 25 and never said so.

    The largest cluster on production is 21 (2026-08-19), so nothing is hidden
    today — which is a measurement, not a design, and the disclosure is what
    keeps it one.
    """

    def test_the_total_travels_with_the_ids(self, app):
        from services.coordinate_quality import shared_coordinate_peers

        subject = _listing(
            "coord_subject",
            location_lat=Decimal("43.5400000"),
            location_lon=Decimal("-5.6600000"),
        )
        for index in range(4):
            _listing(
                f"coord_peer_{index}",
                location_lat=Decimal("43.5400000"),
                location_lon=Decimal("-5.6600000"),
            )

        ids, population = shared_coordinate_peers(subject, limit=2)

        assert len(ids) == 2
        assert population.total == 4
        assert population.returned == 2
        assert population.truncated is True
        assert population.not_shown == 2

    def test_a_row_with_no_coordinate_is_not_an_empty_cluster(self, app):
        """`total` stays None: nobody looked, which is not "nobody is there"."""
        from services.coordinate_quality import shared_coordinate_peers

        ids, population = shared_coordinate_peers(_listing("coord_nowhere"))

        assert ids == []
        assert population.total is None
        assert population.truncated is False

    def test_the_page_says_how_many_it_is_not_showing(self, client, app):
        subject = _listing(
            "coord_page_subject",
            location_lat=Decimal("43.5100000"),
            location_lon=Decimal("-5.6100000"),
        )
        for index in range(27):
            _listing(
                f"coord_page_peer_{index}",
                location_lat=Decimal("43.5100000"),
                location_lon=Decimal("-5.6100000"),
            )

        response = client.get(f"/properties/{subject.id}")
        assert response.status_code == 200, "the property page did not render"
        body = response.get_data(as_text=True)

        assert 'id="shared-coordinate-truncated"' in body
        assert "and 2 more" in body


class TestTheComparablePoolNamesItself:
    """The pool is one subscription's listings inside one tier of a ladder.

    A model handed "avg €26/m²" reads it as the local market. #386 taught the
    prompt to say when the *sizes* were mixed; it still could not say how many
    peers there were, whose subscription they came from, or that the query
    stopped at a ceiling.
    """

    def test_the_meta_names_the_population(self, app):
        from services.property_comparables import collect_comparables

        live, other, _ = _profiles()
        subject = _listing(
            "peer_subject",
            live.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )
        for index in range(3):
            _listing(
                f"peer_{index}",
                live.id,
                area=1000,
                property_category="land",
                property_subtype="plot",
            )
        # A listing of the same shape in another subscription: not a peer.
        _listing(
            "peer_elsewhere",
            other.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )

        rows, meta = collect_comparables(
            subject, category="land", min_peers=3, limit=600
        )

        assert len(rows) == 3
        assert meta["peers_used"] == 3
        assert meta["peers_matched"] == 3
        assert meta["peers_cap"] == 600
        assert meta["profile_scope"] == "own_subscription"

    def test_a_capped_pool_says_what_it_was_capped_out_of(self, app):
        from services.property_comparables import collect_comparables

        live, _, _ = _profiles()
        subject = _listing(
            "cap_subject",
            live.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )
        for index in range(6):
            _listing(
                f"cap_peer_{index}",
                live.id,
                area=1000,
                property_category="land",
                property_subtype="plot",
            )

        _, meta = collect_comparables(subject, category="land", min_peers=1, limit=2)

        assert meta["peers_used"] == 2
        assert meta["peers_matched"] == 6
        assert meta["peers_cap"] == 2

    def test_a_subject_with_no_subscription_compares_against_everything(self, app):
        """The other branch of `profile_scope`, and the reason it is named.

        `collect_comparables` scopes to the subject's own `search_profile_id`
        only when it has one. A listing with none is compared against every
        subscription at once -- a different question with the same answer
        shape, so the prompt has to be told which it got. Production held 0
        such rows on 2026-08-19; ingestion can still produce one (#110).
        """
        from services.property_comparables import collect_comparables

        live, other, _ = _profiles()
        subject = _listing(
            "orphan_subject",
            None,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )
        _listing(
            "orphan_peer_a",
            live.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )
        _listing(
            "orphan_peer_b",
            other.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )

        rows, meta = collect_comparables(
            subject, category="land", min_peers=2, limit=600
        )

        assert meta["profile_scope"] == "every_subscription"
        assert len(rows) == 2, "both subscriptions' listings are peers here"

    def test_the_prompt_says_it_out_loud(self):
        from services.property_ai_service import PropertyAIService

        lines = PropertyAIService._market_basis_lines(
            {
                "comparable_scope": "municipality+subtype+area_band",
                "size_comparable": True,
                "area_band_m2": [800.0, 1250.0],
                "peers_used": 2,
                "peers_matched": 600,
                "peers_cap": 2,
                "profile_scope": "own_subscription",
            }
        )
        joined = " ".join(lines)
        assert "Pool: 2 peers" in joined
        assert "own subscription only" in joined
        assert "capped at 2 of 600 matching" in joined

    def test_the_listed_three_say_what_they_were_chosen_out_of(self, app):
        """ "3 similar properties" with no denominator reads as the comparison."""
        from services.property_ai_service import PropertyAIService

        live, _, _ = _profiles()
        subject = _listing(
            "hdr_subject",
            live.id,
            area=1000,
            property_category="land",
            property_subtype="plot",
        )
        for index in range(5):
            _listing(
                f"hdr_peer_{index}",
                live.id,
                area=1000,
                property_category="land",
                property_subtype="plot",
            )

        prompt, _ = PropertyAIService()._build_prompt(subject)

        assert "of 5 peers" in prompt
        assert "Pool: 5 peers from this listing's own subscription only" in prompt

    def test_the_three_say_when_the_pool_they_came_from_was_itself_capped(self):
        """ "3 of 600" while 5,000 matched is the truncation defect one layer in."""
        from services.property_ai_service import PropertyAIService

        header = " ".join(
            PropertyAIService._market_basis_lines(
                {
                    "comparable_scope": "category",
                    "size_comparable": False,
                    "peers_used": 600,
                    "peers_matched": 5000,
                    "peers_cap": 600,
                    "profile_scope": "every_subscription",
                }
            )
        )
        assert (
            "Pool: 600 peers from every subscription (this listing has none)" in header
        )
        assert "capped at 600 of 5000 matching" in header


class TestABackfillSaysWhatItIsAboutToCover:
    """ "572 rows" cannot say that 112 of them belong to searches that stopped.

    The scopes stay profile-agnostic (#410): a hidden subscription keeps
    ingesting, and showing it again must not reveal holes. What an operator is
    owed is the composition and the worst-case bill, before the first call.
    """

    def test_log_scope_writes_the_composition(self, app, caplog):
        from utils.enrich_scope import log_scope
        import logging

        live, retired, hidden = _profiles()
        rows = [
            _listing("scope_live", live.id),
            _listing("scope_retired", retired.id),
            _listing("scope_hidden", hidden.id),
            _listing("scope_orphan", None),
        ]

        logger = logging.getLogger("test_scope")
        with caplog.at_level(logging.INFO):
            population = log_scope(logger, rows, label="queue", notes=("free",))

        assert population.subscriptions.retired == 1
        assert population.subscriptions.hidden == 1
        assert population.subscriptions.listings_unassigned == 1
        text = caplog.text
        assert "1 live, 1 retired, 1 hidden" in text
        assert "1 with no subscription" in text
        assert "note: free" in text

    def test_the_travel_recalc_can_be_asked_before_it_spends(
        self, app, monkeypatch, caplog
    ):
        """The most expensive run in the repository had no way to ask.

        ~$0.36 a listing over every located row, and `--snapshot` was required
        before anyone could even see the scope.
        """
        import logging
        from contextlib import nullcontext

        from utils import recalc_property_travel as tool

        live, retired, _ = _profiles()
        rows = [
            _listing(
                "travel_live",
                live.id,
                location_lat=Decimal("43.5"),
                location_lon=Decimal("-5.6"),
            ),
            _listing(
                "travel_retired",
                retired.id,
                location_lat=Decimal("43.5"),
                location_lon=Decimal("-5.7"),
            ),
        ]

        class _CurrentApp:
            def app_context(self):
                return nullcontext()

        def _explode(*args, **kwargs):
            raise AssertionError("a dry run must not build the travel service")

        monkeypatch.setattr(tool, "create_app", lambda: _CurrentApp())
        monkeypatch.setattr(tool, "PropertyTravelService", _explode)
        monkeypatch.setattr("sys.argv", ["recalc", "--dry-run"])

        with caplog.at_level(logging.INFO):
            tool.main()

        assert "1 live, 1 retired" in caplog.text
        # The arithmetic, not just the label: two located rows at the recorded
        # ~$0.36 a listing, 7 Places calls and 26 Distance Matrix elements each.
        assert "<=14 Places calls" in caplog.text
        assert "<=52 Distance Matrix elements" in caplog.text
        assert "$0.72" in caplog.text
        db.session.expire_all()
        for row in rows:
            assert db.session.get(Property, row.id).travel is None
