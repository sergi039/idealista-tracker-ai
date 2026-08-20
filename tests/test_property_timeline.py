"""The conversation behind a listing: notes, exchanges, and what may edit them.

The storage arrived with the decision (#430, migration 021); this is the feed
over it. Three things are worth a test rather than a reading of the template:

* the feed is ordered by **when the exchange happened**, not by when somebody
  sat down to type it -- an answer given on the phone yesterday is recorded
  today, and the wrong order tells the story wrong;
* a **verdict entry cannot be edited or deleted through these controls**. It is
  the record of a decision, written beside the columns it describes, and a
  route that could reach it could edit the log into disagreement with the
  state it is the history of. Hiding the control in the template is not
  enough: the refusal is in the service and the route, and the test posts at
  one directly;
* **deletion is soft**. Everything else here can be recomputed; a sentence the
  owner typed cannot.
"""

from datetime import datetime, timedelta

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
def prop(app):
    profile = SearchProfile(name="Asturias", is_active=True, is_default=True)
    db.session.add(profile)
    db.session.commit()
    row = Property(source_email_id="bayas", title="Bayas", search_profile_id=profile.id)
    db.session.add(row)
    db.session.commit()
    return row


def _rendered(response):
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "An error occurred while loading property details" not in body
    return body


class TestRecording:
    def test_a_note_is_its_text(self, app, prop):
        owner_review.add_note(prop, body="the agent sent the ficha catastral")
        entries = owner_review.timeline(prop)
        assert len(entries) == 1
        assert entries[0].kind == "note"
        assert entries[0].channel is None

        for blank in ("", "   ", "\n", "\t"):
            with pytest.raises(owner_review.ReviewError):
                owner_review.add_note(prop, body=blank)

    def test_a_contact_carries_its_structure(self, app, prop):
        entry = owner_review.add_contact(
            prop,
            channel="whatsapp",
            counterpart="David Villa, Sellmi",
            asked="¿tienen la ficha catastral?",
            body="sent the PDF",
        )
        assert entry.kind == "contact"
        assert entry.channel == "whatsapp"
        assert entry.snapshot is None

    def test_a_visit_with_nobody_quoted_is_a_real_entry(self, app, prop):
        # "Went and looked, wrote nothing down" is something that happens, as
        # long as it says who was met.
        entry = owner_review.add_contact(prop, channel="visit", counterpart="Sellmi")
        assert entry.kind == "contact"

    def test_an_entirely_empty_contact_is_refused(self, app, prop):
        with pytest.raises(owner_review.ReviewError):
            owner_review.add_contact(prop, channel="visit")

    def test_a_channel_nobody_defined_is_refused(self, app, prop):
        with pytest.raises(owner_review.ReviewError):
            owner_review.add_contact(prop, channel="telegram", body="hi")


class TestTheOrder:
    def test_it_reads_by_when_it_happened_and_not_by_when_it_was_typed(self, app, prop):
        """The reason `happened_at` exists at all."""
        today = datetime(2026, 8, 20, 9, 0)

        # The recent exchange is typed FIRST and the older one second, so the
        # two orders disagree: by `created_at` the last-typed row leads, by
        # `happened_at` the recent exchange does. A fixture typed in
        # chronological order cannot tell the two apart -- measured, a mutation
        # swapping the sort column left this test green until the inserts were
        # reversed.
        owner_review.add_note(prop, body="the agent sent the ficha", happened_at=today)
        owner_review.add_note(
            prop,
            body="phoned the town hall last week",
            happened_at=today - timedelta(days=7),
        )

        bodies = [entry.body for entry in owner_review.timeline(prop)]
        assert bodies == ["the agent sent the ficha", "phoned the town hall last week"]

    def test_a_verdict_change_takes_its_place_in_the_same_feed(self, app, prop):
        owner_review.add_note(
            prop, body="asked about the RC", happened_at=datetime(2026, 8, 19, 9, 0)
        )
        owner_review.set_review(prop, decision="rejected", reason="irregular parcel")

        kinds = [entry.kind for entry in owner_review.timeline(prop)]
        # One feed, causally ordered: the decision comes after the question it
        # answers, and a reader does not re-interleave two lists by date.
        assert kinds == ["verdict", "note"]


class TestEditing:
    def test_editing_marks_the_entry_as_edited(self, app, prop):
        entry = owner_review.add_note(prop, body="first")
        assert owner_review.was_edited(entry) is False

        owner_review.edit_entry(entry, body="corrected")
        assert entry.body == "corrected"
        assert owner_review.was_edited(entry) is True

    def test_a_note_cannot_be_edited_into_nothing(self, app, prop):
        entry = owner_review.add_note(prop, body="real")
        with pytest.raises(owner_review.ReviewError):
            owner_review.edit_entry(entry, body="   ")
        assert entry.body == "real"

    def test_a_contact_cannot_be_emptied_either(self, app, prop):
        entry = owner_review.add_contact(prop, channel="phone", body="they answered")
        with pytest.raises(owner_review.ReviewError):
            owner_review.edit_entry(entry, body="", asked="", counterpart="")

    def test_deleting_is_soft_and_leaves_the_feed(self, app, prop):
        entry = owner_review.add_note(prop, body="a mis-tap away from gone")
        owner_review.soft_delete_entry(entry)

        assert owner_review.timeline(prop) == []
        # Kept, because nothing can recompute a sentence somebody typed.
        assert len(owner_review.timeline(prop, include_deleted=True)) == 1
        assert db.session.get(PropertyActivity, entry.id).deleted_at is not None


class TestAVerdictEntryIsNotANote:
    """The log of a decision is not editable through the note controls."""

    def _verdict_entry(self, prop):
        owner_review.set_review(prop, decision="waiting", reason="asked for the RC")
        return PropertyActivity.query.filter_by(kind="verdict").one()

    def test_the_service_refuses_to_edit_one(self, app, prop):
        entry = self._verdict_entry(prop)
        with pytest.raises(owner_review.ReviewError):
            owner_review.edit_entry(entry, body="rewritten")

    def test_the_service_refuses_to_delete_one(self, app, prop):
        entry = self._verdict_entry(prop)
        with pytest.raises(owner_review.ReviewError):
            owner_review.soft_delete_entry(entry)
        assert entry.deleted_at is None

    def test_the_route_refuses_too_and_writes_nothing(self, app, client, prop):
        """Not merely hidden in the template: posted at directly."""
        entry = self._verdict_entry(prop)

        response = client.post(
            f"/properties/{prop.id}/activity/{entry.id}",
            data={"action": "save", "body": "rewritten by hand"},
        )
        assert response.status_code == 302
        db.session.expire_all()
        stored = db.session.get(PropertyActivity, entry.id)
        assert stored.body is None
        assert stored.snapshot["decision"] == "waiting"

        client.post(
            f"/properties/{prop.id}/activity/{entry.id}", data={"action": "delete"}
        )
        db.session.expire_all()
        assert db.session.get(PropertyActivity, entry.id).deleted_at is None


class TestTheRoutes:
    def test_adding_a_note_through_the_form(self, app, client, prop):
        response = client.post(
            f"/properties/{prop.id}/activity",
            data={"kind": "note", "body": "the agent sent the ficha catastral"},
        )
        assert response.status_code == 302
        assert len(owner_review.timeline(prop)) == 1

    def test_adding_an_exchange_through_the_form(self, app, client, prop):
        client.post(
            f"/properties/{prop.id}/activity",
            data={
                "kind": "contact",
                "channel": "whatsapp",
                "counterpart": "David Villa, Sellmi",
                "asked": "ficha catastral?",
                "body": "sent the PDF",
                "happened_on": "2026-08-20",
            },
        )
        entry = owner_review.timeline(prop)[0]
        assert entry.channel == "whatsapp"
        assert entry.happened_at.date().isoformat() == "2026-08-20"

    def test_an_unreadable_date_writes_nothing(self, app, client, prop):
        client.post(
            f"/properties/{prop.id}/activity",
            data={"kind": "note", "body": "real", "happened_on": "soon"},
        )
        assert owner_review.timeline(prop) == []

    def test_an_entry_of_another_property_is_not_reachable_from_this_page(
        self, app, client, prop
    ):
        """The lookup is by both ids, so this is a 404 and not a write."""
        other = Property(
            source_email_id="other",
            title="Other",
            search_profile_id=prop.search_profile_id,
        )
        db.session.add(other)
        db.session.commit()
        entry = owner_review.add_note(other, body="belongs to the other listing")

        response = client.post(
            f"/properties/{prop.id}/activity/{entry.id}",
            data={"action": "delete"},
        )
        assert response.status_code == 404
        db.session.expire_all()
        assert db.session.get(PropertyActivity, entry.id).deleted_at is None

    def test_the_page_renders_the_feed(self, app, client, prop):
        owner_review.add_note(prop, body="the agent sent the ficha catastral")
        owner_review.add_contact(
            prop,
            channel="whatsapp",
            counterpart="David Villa, Sellmi",
            asked="ficha catastral?",
            body="sent the PDF",
        )
        owner_review.set_review(prop, decision="rejected", reason="irregular parcel")

        body = _rendered(client.get(f"/properties/{prop.id}"))
        assert 'id="property-timeline"' in body
        assert "the agent sent the ficha catastral" in body
        assert "David Villa, Sellmi" in body
        assert "ficha catastral?" in body
        # The decision appears in the feed as its own entry, with its reason.
        assert "Decision recorded" in body
        assert "irregular parcel" in body

    def test_an_empty_feed_says_so_rather_than_showing_nothing(self, app, client, prop):
        body = _rendered(client.get(f"/properties/{prop.id}"))
        assert "Nothing recorded yet" in body

    def test_a_deleted_entry_leaves_the_page(self, app, client, prop):
        entry = owner_review.add_note(prop, body="a mis-tap away from gone")
        client.post(
            f"/properties/{prop.id}/activity/{entry.id}", data={"action": "delete"}
        )
        body = _rendered(client.get(f"/properties/{prop.id}"))
        assert "a mis-tap away from gone" not in body


class TestCsrf:
    @pytest.fixture
    def app(self):
        setup_test_environment()
        application = create_app()
        application.config["TESTING"] = True
        with application.app_context():
            db.create_all()
            yield application
            db.session.remove()
            db.drop_all()

    def test_adding_without_a_token_is_refused(self, app):
        profile = SearchProfile(name="A", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.commit()
        row = Property(source_email_id="x", title="Plot", search_profile_id=profile.id)
        db.session.add(row)
        db.session.commit()

        client = app.test_client()
        response = client.post(
            f"/properties/{row.id}/activity", data={"kind": "note", "body": "hi"}
        )
        assert response.status_code == 400
        assert owner_review.timeline(row) == []
