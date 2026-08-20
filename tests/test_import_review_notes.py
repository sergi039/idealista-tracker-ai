"""Converting a hand-written review into the storage that now exists (#430).

Property 774's whole thread went into `enrichment` through `docker exec`,
because on 2026-08-20 there was nowhere else. The fixtures here are that block
verbatim, from production.

Three things are tested rather than assumed, and each is a way to lose
something:

* the conversion **refuses a property that already carries entries**, so a
  second run cannot double the timeline;
* it **copies** — `enrichment` still holds what it held, because nothing here
  deletes a measurement;
* `restore` **stops rather than overwriting** an entry somebody edited or a
  verdict somebody set afterwards. Undoing a conversion is not licence to
  delete work that came after it.
"""

import json

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import owner_review
from tests import setup_test_environment
from utils import import_review_notes

# The real blocks, as production holds them.
REVIEW = {
    "verdict": "rejected",
    "reason": (
        "parcel shape: irregular, L-shaped with a neck; the owner wants a "
        "regular (square/rectangular) plot"
    ),
    "evidence": (
        "cadastral plan RC 33016A003001530001HQ: fills 0.35 of its 120x146 m "
        "bounding box, Polsby-Popper 0.30 against ~0.79 for a square"
    ),
    "decided_at": "2026-08-20",
    "decided_by": "owner",
    "not_rejected_for": (
        "price, location or planning: the cadastre calls the parcel URBANO and "
        "the agent has the condiciones de edificabilidad requested in writing "
        "(verbal answer: a single-family house is possible; suministros a pie "
        "de parcela)"
    ),
    "communicated_to_agent_at": "2026-08-20T09:17:46.939217+00:00",
}

CADASTRE = {
    "clase": "URBANO",
    "parcela": 153,
    "poligono": 3,
    "locality": "Truevano",
    "municipality": "Castrillón",
    "referencia_catastral": "33016A003001530001HQ",
    "superficie_grafica_m2": 6193,
    "received": {
        "at": "2026-08-20T09:24:00+02:00",
        "from": "David Villa, Sellmi",
        "channel": "whatsapp",
        "document": "Consulta descriptiva y gráfica de datos catastrales (PDF)",
    },
}


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


@pytest.fixture
def prop(app):
    profile = SearchProfile(name="Land at Norte", is_active=True, is_default=True)
    db.session.add(profile)
    db.session.commit()
    row = Property(
        source_email_id="bayas",
        title="Bayas, Castrillón, Ct-6, Santa María del Mar",
        search_profile_id=profile.id,
        enrichment={"review": REVIEW, "cadastre": CADASTRE},
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestThePlan:
    def test_it_reads_the_verdict_and_the_reference(self, app, prop):
        plan = import_review_notes.plan_for(prop)
        assert plan["decision"] == "rejected"
        assert plan["reason"] == REVIEW["reason"]
        assert plan["cadastral_reference"] == "33016A003001530001HQ"

    def test_the_exchange_that_delivered_the_document_is_reconstructed(self, app, prop):
        plan = import_review_notes.plan_for(prop)
        contact = next(e for e in plan["entries"] if e["kind"] == "contact")

        assert contact["channel"] == "whatsapp"
        assert contact["counterpart"] == "David Villa, Sellmi"
        assert "Consulta descriptiva" in contact["body"]
        assert "33016A003001530001HQ" in contact["body"]
        # The time the block records, not the time of the conversion.
        assert contact["happened_at"].date().isoformat() == "2026-08-20"

    def test_it_does_not_claim_the_document_is_stored(self, app, prop):
        """The PDF arrived in the owner's WhatsApp; no copy is here.

        Saying "sent the ficha catastral" is a record of what happened. Writing
        it as an attachment, because its name is known, would be the defect this
        whole feature exists to remove.
        """
        plan = import_review_notes.plan_for(prop)
        contact = next(e for e in plan["entries"] if e["kind"] == "contact")
        assert contact["body"].startswith("sent ")

        import_review_notes.convert(prop, plan)
        from models import PropertyAttachment

        assert PropertyAttachment.query.count() == 0

    def test_the_verbal_answers_are_kept_as_one_sentence(self, app, prop):
        """They were written as one, and splitting them invents two."""
        plan = import_review_notes.plan_for(prop)
        notes = [e for e in plan["entries"] if e["kind"] == "note"]
        hearsay = next(n for n in notes if "URBANO" in n["body"])
        assert "single-family house is possible" in hearsay["body"]
        assert "suministros a pie" in hearsay["body"]

    def test_a_property_with_no_block_produces_nothing(self, app, prop):
        prop.enrichment = {}
        db.session.commit()
        plan = import_review_notes.plan_for(prop)
        assert plan["entries"] == []
        assert plan["decision"] is None


class TestTheConversion:
    def test_it_writes_the_timeline_the_ticket_asks_for(self, app, prop):
        plan = import_review_notes.plan_for(prop)
        import_review_notes.convert(prop, plan)

        entries = owner_review.timeline(prop)
        kinds = [entry.kind for entry in entries]
        assert "contact" in kinds
        assert "note" in kinds
        # `set_review` appends the verdict entry, so the decision and its
        # history arrive together rather than through two writers.
        assert "verdict" in kinds

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert stored.owner_verdict == "rejected"
        assert stored.cadastral_reference == "33016A003001530001HQ"
        assert owner_review.history_out_of_sync(stored) is False

    def test_enrichment_is_left_alone(self, app, prop):
        """A copy, not a move: nothing here deletes a measurement."""
        import_review_notes.convert(prop, import_review_notes.plan_for(prop))

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert stored.enrichment["review"] == REVIEW
        assert stored.enrichment["cadastre"]["referencia_catastral"]

    def test_a_second_run_is_refused(self, app, prop):
        plan = import_review_notes.plan_for(prop)
        import_review_notes.convert(prop, plan)

        with pytest.raises(RuntimeError, match="already carries"):
            import_review_notes.convert(prop, import_review_notes.plan_for(prop))

        # And nothing was doubled by the attempt.
        assert len([e for e in owner_review.timeline(prop) if e.kind == "contact"]) == 1

    def test_it_refuses_a_property_somebody_has_already_used(self, app, prop):
        """Not just its own second run: any entries at all."""
        owner_review.add_note(prop, body="typed by hand before the conversion")

        with pytest.raises(RuntimeError, match="already carries"):
            import_review_notes.convert(prop, import_review_notes.plan_for(prop))


class TestTheRestore:
    def _convert_with_snapshot(self, prop, tmp_path):
        plan = import_review_notes.plan_for(prop)
        snapshot = import_review_notes.snapshot_for(prop, plan)
        result = import_review_notes.convert(prop, plan)
        result["before"] = snapshot["before"]

        path = tmp_path / "snapshot.json"
        path.write_text(
            json.dumps({"converted": [result], "snapshots": [snapshot]}, default=str)
        )
        return path

    def test_it_removes_exactly_what_the_conversion_wrote(self, app, prop, tmp_path):
        path = self._convert_with_snapshot(prop, tmp_path)
        assert owner_review.timeline(prop)

        restored = import_review_notes.restore(str(path))

        assert restored == 1
        assert owner_review.timeline(prop) == []
        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert stored.owner_verdict is None
        assert stored.cadastral_reference is None
        # And the block it copied from is still there, so it can be re-run.
        assert stored.enrichment["review"] == REVIEW

    def test_it_stops_rather_than_deleting_an_edited_entry(self, app, prop, tmp_path):
        path = self._convert_with_snapshot(prop, tmp_path)
        entry = next(e for e in owner_review.timeline(prop) if e.kind == "note")
        owner_review.edit_entry(entry, body="the owner corrected this afterwards")

        restored = import_review_notes.restore(str(path))

        # Undoing a conversion is not licence to delete work that came after it.
        assert restored == 0
        assert any(
            e.body == "the owner corrected this afterwards"
            for e in owner_review.timeline(prop)
        )

    def test_it_stops_when_an_entry_it_wrote_is_already_gone(self, app, prop, tmp_path):
        path = self._convert_with_snapshot(prop, tmp_path)
        entry = next(e for e in owner_review.timeline(prop) if e.kind == "note")
        db.session.delete(entry)
        db.session.commit()

        assert import_review_notes.restore(str(path)) == 0
        # The rest is left as it is rather than half-removed.
        assert owner_review.timeline(prop)


def test_the_scope_is_the_block_and_not_a_hard_coded_id(app, prop):
    """A hard-coded 774 would make this a note about one row, not a tool."""
    other = Property(
        source_email_id="plain",
        title="No review block",
        search_profile_id=prop.search_profile_id,
        enrichment={"sea": {"status": "ok"}},
    )
    db.session.add(other)
    db.session.commit()

    found = [row.id for row in import_review_notes.candidates()]
    assert prop.id in found
    assert other.id not in found
