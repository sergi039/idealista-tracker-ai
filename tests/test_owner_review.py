"""The owner's decision and the outstanding action, read two ways (#430).

The contract this file exists for is the one `tests/test_advertiser.py` states:
the badge reads Python and the dropdown's counts read SQL, so a count that
disagrees with the badges under it is a third wrong number rather than a
disclosure. `TestTheTwoReadingsAgree` runs one matrix through both.

The rest of it pins the parts that were argued about before a line was written:
that an absent decision is `undecided` and never a rejection (#98), that a
decision and an outstanding action are independent, that a due date nobody set
cannot raise, that the writer takes the row before it reads the old state, and
that the log cannot be edited through the notes routes it will share a table
with.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, PropertyActivity, SearchProfile
from services import owner_review
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Asturias", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _prop(profile, slug, **kwargs):
    row = Property(
        source_email_id=slug,
        title=kwargs.pop("title", f"Plot {slug}"),
        municipality=kwargs.pop("municipality", "Castrillón"),
        search_profile_id=profile.id,
        **kwargs,
    )
    db.session.add(row)
    db.session.commit()
    return row


TODAY = date(2026, 8, 20)

# (name, columns, expected decision, expected action)
ROWS = [
    ("untouched", {}, "undecided", "none"),
    (
        "interested-no-action",
        {"owner_verdict": "interested"},
        "interested",
        "none",
    ),
    (
        "interested-with-a-late-call",
        {
            "owner_verdict": "interested",
            "next_action": "call the architect",
            "next_action_due_on": date(2026, 8, 1),
        },
        "interested",
        # The point of the split: an outstanding action is legal under any
        # decision, so this is `interested` AND `overdue`, not one or the other.
        "overdue",
    ),
    (
        "waiting-undated",
        {"owner_verdict": "waiting", "next_action": "condiciones de edificabilidad"},
        "waiting",
        # No date is `pending`, never `overdue`: nobody promised a day.
        "pending",
    ),
    (
        "waiting-due-today",
        {
            "owner_verdict": "waiting",
            "next_action": "condiciones",
            "next_action_due_on": TODAY,
        },
        "waiting",
        # Due today is not late today.
        "pending",
    ),
    (
        "waiting-due-yesterday",
        {
            "owner_verdict": "waiting",
            "next_action": "condiciones",
            "next_action_due_on": TODAY - timedelta(days=1),
        },
        "waiting",
        "overdue",
    ),
    (
        "rejected",
        {"owner_verdict": "rejected", "owner_verdict_reason": "irregular parcel"},
        "rejected",
        "none",
    ),
    (
        "undecided-but-owed-something",
        {"next_action": "ask the agency for the RC"},
        "undecided",
        "pending",
    ),
    (
        "action-is-whitespace",
        # A hand-written UPDATE can leave this; it is not an action.
        {"next_action": "   "},
        "undecided",
        "none",
    ),
]


class TestTheTwoReadingsAgree:
    """One matrix, both languages, one query."""

    def test_every_row_reads_the_same_both_ways(self, app, profile):
        for name, columns, _, _ in ROWS:
            _prop(profile, name, **columns)

        decisions_in_sql = dict(
            db.session.query(
                Property.source_email_id, owner_review.decision_expression(Property)
            ).all()
        )
        actions_in_sql = dict(
            db.session.query(
                Property.source_email_id,
                owner_review.action_expression_portable(Property, TODAY),
            ).all()
        )

        for name, _, expected_decision, expected_action in ROWS:
            row = Property.query.filter_by(source_email_id=name).one()
            assert owner_review.read_decision(row)["state"] == expected_decision, name
            assert decisions_in_sql[name] == expected_decision, name
            assert owner_review.read_action(row, TODAY)["state"] == expected_action, (
                name
            )
            assert actions_in_sql[name] == expected_action, name

    def test_the_filters_select_what_the_badges_mark(self, app, profile):
        for name, columns, _, _ in ROWS:
            _prop(profile, name, **columns)

        for state in owner_review.DECISION_STATES:
            clause = owner_review.decision_filter_clause(Property, state)
            selected = Property.query.filter(clause).all()
            for row in selected:
                assert owner_review.read_decision(row)["state"] == state
            assert len(selected) == len([r for r in ROWS if r[2] == state])

        for state in owner_review.ACTION_STATES:
            clause = owner_review.action_filter_clause(Property, state, TODAY)
            selected = Property.query.filter(clause).all()
            for row in selected:
                assert owner_review.read_action(row, TODAY)["state"] == state
            assert len(selected) == len([r for r in ROWS if r[3] == state])

    def test_a_verdict_nobody_defined_reads_as_undecided_both_ways(self, app, profile):
        """Towards "nobody decided", never towards a decision nobody made.

        The database refuses this value outright, but only on PostgreSQL:
        `ck_properties_owner_verdict_enum` lives in migration 021 and not on
        the model, for the reason models.py records, so it is pinned by
        `tests/test_postgres_migrations.py` against a real server. What is
        pinned *here* is what the application does if such a row ever reaches
        it anyway -- a hand-written UPDATE on a database that predates the
        constraint, or a restore from an older dump.
        """
        row = _prop(profile, "nonsense-verdict")
        # Written past the service, the way `docker exec ... psql` would.
        db.session.execute(
            db.text("UPDATE properties SET owner_verdict = 'maybe' WHERE id = :id"),
            {"id": row.id},
        )
        db.session.commit()
        db.session.expire_all()
        stored = db.session.get(Property, row.id)

        assert owner_review.read_decision(stored)["state"] == "undecided"
        in_sql = (
            db.session.query(owner_review.decision_expression(Property))
            .filter(Property.id == row.id)
            .scalar()
        )
        assert in_sql == "undecided"

    def test_an_unknown_filter_value_selects_nothing_rather_than_everything(
        self, app, profile
    ):
        _prop(profile, "one", owner_verdict="rejected")
        assert owner_review.decision_filter_clause(Property, "nonsense") is None
        assert owner_review.action_filter_clause(Property, "nonsense", TODAY) is None


class TestAnAbsentDecisionIsNotARejection:
    """#98, in the column the owner will filter on most."""

    def test_undecided_is_its_own_filter_option_with_its_own_count(self, app, profile):
        _prop(profile, "judged", owner_verdict="rejected")
        _prop(profile, "not-judged")

        counts = dict(
            db.session.query(
                owner_review.decision_expression(Property), db.func.count()
            ).group_by(owner_review.decision_expression(Property))
        )
        options = {
            choice["value"]: choice["count"]
            for choice in owner_review.decision_options(counts)
        }
        assert options["undecided"] == 1
        assert options["rejected"] == 1

    def test_undecided_never_reads_as_decided(self, app, profile):
        row = _prop(profile, "untouched")
        verdict = owner_review.read_decision(row)
        assert verdict["state"] == "undecided"
        assert verdict["decided"] is False
        assert verdict["reason"] is None


class TestADueDateNobodySetCannotRaise:
    """`None < date` is a TypeError, and the page degrades by redirect."""

    def test_an_action_with_no_date_is_pending(self, app, profile):
        row = _prop(profile, "no-date", owner_verdict="waiting", next_action="ask")
        assert owner_review.read_action(row, TODAY)["state"] == "pending"
        assert owner_review.read_action(row, TODAY)["overdue"] is False

    def test_a_decided_row_with_no_action_renders_in_the_list(
        self, app, client, profile
    ):
        _prop(profile, "decided-no-action", owner_verdict="waiting")
        response = client.get("/properties")
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        # /properties catches any exception, flashes and re-renders with no
        # rows at the same 200, so the status alone proves nothing here.
        assert "An error occurred while loading properties" not in body
        assert "Waiting" in body


class TestTheWriter:
    def test_it_records_the_columns_and_one_event_carrying_the_whole_state(
        self, app, profile
    ):
        row = _prop(profile, "bayas")
        owner_review.set_review(
            row,
            decision="rejected",
            reason="irregular parcel",
            action="ask for the condiciones",
            due_on=date(2026, 9, 20),
        )

        assert row.owner_verdict == "rejected"
        assert row.owner_verdict_reason == "irregular parcel"
        assert row.next_action == "ask for the condiciones"
        assert row.next_action_due_on == date(2026, 9, 20)

        events = PropertyActivity.query.filter_by(kind="verdict").all()
        assert len(events) == 1
        snapshot = events[0].snapshot
        # The whole state, not a from/to pair: a changed reason under an
        # unchanged decision is a real change and a pair would lose it.
        assert snapshot["decision"] == "rejected"
        assert snapshot["reason"] == "irregular parcel"
        assert snapshot["action"] == "ask for the condiciones"
        assert snapshot["due_on"] == "2026-09-20"
        assert snapshot["previous"]["decision"] is None

    def test_changing_only_the_reason_still_records_an_event(self, app, profile):
        row = _prop(profile, "reason-only")
        owner_review.set_review(row, decision="waiting", reason="first")
        owner_review.set_review(row, decision="waiting", reason="second")

        events = (
            PropertyActivity.query.filter_by(kind="verdict")
            .order_by(PropertyActivity.id)
            .all()
        )
        assert len(events) == 2
        assert events[1].snapshot["reason"] == "second"
        assert events[1].snapshot["previous"]["reason"] == "first"

    def test_writing_the_same_state_twice_records_nothing_the_second_time(
        self, app, profile
    ):
        row = _prop(profile, "idempotent")
        owner_review.set_review(row, decision="interested", reason="close to the sea")
        result = owner_review.set_review(
            row, decision="interested", reason="close to the sea"
        )
        assert result["changed"] is False
        assert PropertyActivity.query.filter_by(kind="verdict").count() == 1

    def test_clearing_returns_the_row_to_undecided_and_not_to_rejected(
        self, app, profile
    ):
        row = _prop(profile, "cleared")
        owner_review.set_review(row, decision="rejected", reason="too far")
        owner_review.set_review(row, decision=None)

        assert row.owner_verdict is None
        assert owner_review.read_decision(row)["state"] == "undecided"

    def test_a_due_date_with_no_action_is_refused(self, app, profile):
        row = _prop(profile, "dangling-date")
        with pytest.raises(owner_review.ReviewError):
            owner_review.set_review(
                row, decision="waiting", action="   ", due_on=date(2026, 9, 20)
            )

    def test_an_unknown_decision_is_refused(self, app, profile):
        row = _prop(profile, "nonsense")
        with pytest.raises(owner_review.ReviewError):
            owner_review.set_review(row, decision="maybe")

    def test_it_refuses_a_session_with_other_work_pending(self, app, profile):
        row = _prop(profile, "pending-session")
        # This function ends the transaction on every exit, so anything else in
        # flight would be committed or discarded wholesale.
        db.session.add(Property(source_email_id="in-flight", title="x"))
        with pytest.raises(owner_review.ReviewError):
            owner_review.set_review(row, decision="waiting")
        db.session.rollback()

    def test_it_takes_the_row_before_it_reads_the_old_state(self, app, profile):
        """`FOR UPDATE`, and before the read -- not after it.

        Two presses on four gunicorn threads would otherwise read the same old
        decision and append two contradictory transitions, each atomic and both
        wrong: the shape of #339, one column over.

        This asserts the call rather than the effect, and that limit is worth
        stating: SQLite has no row lock to observe, so what a two-session
        PostgreSQL test would prove -- that the second writer really waits --
        is not proven here. What is proven is that the lock is taken, and taken
        *before* the snapshot the new state is diffed against is read; removing
        the line makes this red, which a behavioural assertion on SQLite could
        not.
        """
        row = _prop(profile, "locked")
        order = []

        real_refresh = db.session.refresh

        def watched_refresh(instance, *args, **kwargs):
            order.append(("refresh", kwargs.get("with_for_update")))
            return real_refresh(instance, *args, **kwargs)

        real_snapshot = owner_review.review_snapshot

        def watched_snapshot(record):
            order.append(("read", None))
            return real_snapshot(record)

        with (
            patch.object(db.session, "refresh", watched_refresh),
            patch.object(owner_review, "review_snapshot", watched_snapshot),
        ):
            owner_review.set_review(row, decision="rejected", reason="irregular")

        assert ("refresh", True) in order, order
        assert order.index(("refresh", True)) < order.index(("read", None)), order

    def test_there_is_no_public_way_to_hold_the_lock_past_the_call(self):
        import inspect

        signature = inspect.signature(owner_review.set_review)
        # A lock whose release the callee cannot see is worse than the race it
        # was taken against (services/enrichment_write.py states the same).
        assert "commit" not in signature.parameters


class TestTheHistoryDisclosure:
    def test_a_column_written_behind_the_app_is_disclosed(self, app, profile):
        row = _prop(profile, "hand-written")
        owner_review.set_review(row, decision="waiting", reason="asked")
        assert owner_review.history_out_of_sync(row) is False

        # What `docker exec ... psql` does: the column moves, no event follows.
        row.owner_verdict = "rejected"
        db.session.commit()
        assert owner_review.history_out_of_sync(row) is True

    def test_a_changed_reason_is_disclosed_too(self, app, profile):
        row = _prop(profile, "reason-changed")
        owner_review.set_review(row, decision="waiting", reason="first")
        row.owner_verdict_reason = "rewritten by hand"
        db.session.commit()
        # The comparison is the whole snapshot, not just the decision column.
        assert owner_review.history_out_of_sync(row) is True

    def test_an_untouched_row_is_not_disclosed(self, app, profile):
        row = _prop(profile, "untouched")
        assert owner_review.history_out_of_sync(row) is False

    def test_the_row_readers_never_query(self, app, profile):
        """The list calls these once per row; a query here is an N+1."""
        row = _prop(profile, "pure")
        owner_review.set_review(row, decision="rejected")

        # Touch every column first: after a commit the attributes are expired,
        # so the first access would emit the row's own refresh SELECT and the
        # test would be measuring SQLAlchemy rather than these two functions.
        # The list has its rows loaded when it calls them.
        _ = (
            row.owner_verdict,
            row.owner_verdict_reason,
            row.owner_verdict_at,
            row.next_action,
            row.next_action_due_on,
        )

        statements = []
        from sqlalchemy import event

        engine = db.session.get_bind()

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            owner_review.read_decision(row)
            owner_review.read_action(row, TODAY)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert statements == []


class TestTheRoute:
    def test_it_records_and_redirects(self, app, client, profile):
        row = _prop(profile, "via-route")
        response = client.post(
            f"/properties/{row.id}/review",
            data={
                "verdict": "rejected",
                "reason": "irregular parcel",
                "next_action": "ask for the RC",
                "due_on": "2026-09-20",
            },
        )
        assert response.status_code == 302
        db.session.expire_all()
        stored = db.session.get(Property, row.id)
        assert stored.owner_verdict == "rejected"
        assert stored.next_action_due_on == date(2026, 9, 20)

    def test_a_bad_date_is_a_flash_and_not_a_crash(self, app, client, profile):
        row = _prop(profile, "bad-date")
        response = client.post(
            f"/properties/{row.id}/review",
            data={"verdict": "waiting", "next_action": "ask", "due_on": "soon"},
        )
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict is None

    def test_an_unknown_verdict_writes_nothing(self, app, client, profile):
        row = _prop(profile, "bad-verdict")
        client.post(f"/properties/{row.id}/review", data={"verdict": "maybe"})
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict is None

    def test_the_property_page_renders_the_section(self, app, client, profile):
        row = _prop(profile, "detail")
        response = client.get(f"/properties/{row.id}")
        body = response.get_data(as_text=True)
        # This route degrades by REDIRECT, so a 200 is itself the assertion --
        # plus the flash text, in case that ever changes.
        assert response.status_code == 200
        assert "An error occurred while loading property details" not in body
        assert 'id="owner-review"' in body
        assert "Not decided yet" in body


class TestCsrf:
    """There is no authentication, so the token is the whole of the defence."""

    @pytest.fixture
    def app(self):
        setup_test_environment()
        application = create_app()
        application.config["TESTING"] = True
        # Deliberately NOT disabling CSRF here.
        with application.app_context():
            db.create_all()
            yield application
            db.session.remove()
            db.drop_all()

    def test_a_post_without_a_token_is_refused_and_writes_nothing(self, app):
        client = app.test_client()
        row = SearchProfile(name="Asturias", is_active=True)
        db.session.add(row)
        db.session.commit()
        prop = _prop(row, "csrf")

        response = client.post(
            f"/properties/{prop.id}/review", data={"verdict": "rejected"}
        )
        assert response.status_code == 400
        db.session.expire_all()
        assert db.session.get(Property, prop.id).owner_verdict is None


class TestTheLogIsNotEditableAsANote:
    """PR2 adds note/contact editing over this same table."""

    def test_a_verdict_row_carries_its_snapshot_and_a_note_cannot(self, app, profile):
        row = _prop(profile, "kinds")
        owner_review.set_review(row, decision="waiting")
        event = PropertyActivity.query.filter_by(kind="verdict").one()
        assert event.snapshot is not None
        assert event.body is None
        assert event.channel is None

    def test_a_note_row_carries_no_snapshot(self, app, profile):
        row = _prop(profile, "note-kind")
        note = PropertyActivity(
            property_id=row.id,
            kind="note",
            happened_at=datetime(2026, 8, 20, 9, 0),
            body="the agent sent the ficha catastral",
        )
        db.session.add(note)
        db.session.commit()
        assert note.snapshot is None
