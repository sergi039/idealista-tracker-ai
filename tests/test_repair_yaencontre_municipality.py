"""Re-reading the municipality off titles yaencontre already sent.

#507 fixed the parser; this repairs the rows written before it. Measured on
production after that deploy: 39 rows still carry a district where the
municipality goes and 63 carry nothing, `/properties` offers 16 district
options, and Vigo is not selectable at all.

What these tests pin:

* the scope and the new value are the shipped parser's own reading, so a row
  it now reads the same way is out of scope;
* a row it can only answer `None` for is left alone -- writing a blank over a
  stored name is a loss, not a correction;
* a dry run writes nothing, because this application cannot delete a property;
* the score is recomputed, because `same_municipality()` builds the peer pool
  from this string and moving a row changes the neighbours it was measured
  against;
* the snapshot carries the name beside the scores, and `restore` puts both
  back and leaves a row that no longer differs alone;
* the snapshot also records what the repair *wrote* (SNAPSHOT-001), so
  `restore` is a real compare-and-swap: a name set by hand after the repair
  survives it, a rescore does not block it, and a legacy snapshot that cannot
  tell the two apart is refused unless `--overwrite-hand-edits` is said out
  loud — and then every overwritten row is named.
"""

import json
import pathlib
from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_scoring_service import PropertyScoringService
from tests import setup_test_environment
from utils import repair_yaencontre_municipality as repair_tool
from utils import score_snapshot

YAE = "https://www.yaencontre.com/venta/casa/inmueble-45358-112353204"
OTHER_PORTAL = "https://www.fotocasa.es/es/comprar/vivienda/vigo/teis/190540646/d"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Galicia costa",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        app.config["TEST_PROFILE_ID"] = profile.id
        yield app
        db.drop_all()


def _row(app, *, title, municipality, url=YAE, **kwargs):
    prop = Property(
        source_email_id=kwargs.pop("source_email_id", f"yaencontre:{id(title)}"),
        url=url,
        title=title,
        municipality=municipality,
        price=kwargs.pop("price", 205000),
        area=kwargs.pop("area", 205),
        search_profile_id=app.config["TEST_PROFILE_ID"],
        **kwargs,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def test_a_district_is_replaced_by_its_municipality(app):
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )

    outcome = repair_tool.repair(apply=True, backup=False)

    assert outcome["repaired"] == 1
    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Vigo"
    # The report of what changed has to describe a change. The rename pass runs
    # before the scoring pass, so a `before` read inside the second pass would
    # be the new name -- the record saying the row already held what it was
    # given, which is the only record of this run there is.
    assert outcome["rows"][0]["before"]["municipality"] == "Teis en Vigo"
    assert outcome["rows"][0]["after"]["municipality"] == "Vigo"


def test_a_row_the_old_reading_left_blank_is_named(app):
    """63 live rows: no street, so no comma, so the old rule answered None."""
    prop = _row(app, title="Casa en venta en Boiro", municipality=None)

    repair_tool.repair(apply=True, backup=False)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Boiro"


def test_a_row_the_parser_already_agrees_with_is_out_of_scope(app):
    _row(
        app,
        title="Casa adosada en venta en avenida Compostela, Outes",
        municipality="Outes",
    )

    assert repair_tool.repair()["found"] == 0


def test_a_title_the_parser_cannot_name_keeps_its_stored_name(app):
    """`None` is the parser's refusal, and a blank is worse than a district."""
    prop = _row(app, title="Salinas / subida a San Martín", municipality="Castrillón")

    assert repair_tool.repair()["found"] == 0
    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Castrillón"


def test_a_street_read_off_a_hand_imported_title_is_refused(app):
    """Property 761 on production: the last comma is a street, not a district.

    Its stored `Gijón` is right and the parser proposes `Calle del Castañeu`.
    Two guards catch it independently, so the test asserts the reason as well
    as the refusal -- a row skipped for the wrong reason is a row the next
    shape of this defect walks past.
    """
    prop = _row(app, title="Porceyo, Gijón, Calle del Castañeu", municipality="Gijón")

    outcome = repair_tool.repair()

    assert outcome["found"] == 0
    assert outcome["skipped"] == [
        {
            "id": prop.id,
            "stored": "Gijón",
            "proposed": "Calle del Castañeu",
            "reason": "stored_name_is_not_a_district",
        }
    ]


def test_a_proposal_the_register_does_not_know_is_refused(app):
    """The stored value IS a district string, so only the INE join can stop it."""
    _row(
        app,
        title="Terreno en venta en Bañugues, Gozón, Calle Go en Nowhere",
        municipality="Somewhere en Nowhere",
    )

    outcome = repair_tool.repair()

    assert outcome["found"] == 0
    assert [s["reason"] for s in outcome["skipped"]] == [
        "proposal_is_not_a_municipality"
    ]


def test_another_portal_is_never_touched(app):
    _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
        url=OTHER_PORTAL,
    )

    assert repair_tool.repair()["found"] == 0


def test_a_dry_run_writes_nothing(app):
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )

    repair_tool.repair(apply=False)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Teis en Vigo"


def test_the_score_is_recomputed_because_the_peer_pool_moved(app):
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    prop.scoring = {"stale": True}
    db.session.commit()

    repair_tool.repair(apply=True, backup=False)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).scoring != {"stale": True}


def test_the_snapshot_carries_the_name_and_restore_puts_both_back(app, tmp_path):
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    prop.score_total = Decimal("61.75")
    db.session.commit()

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["scores"][0]["municipality"] == "Teis en Vigo"
    assert payload["scores"][0]["score_total"] == "61.75"

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Vigo"

    repair_tool.restore(path, apply=True)

    db.session.expire_all()
    back = db.session.get(Property, prop.id)
    assert back.municipality == "Teis en Vigo"
    assert back.score_total == Decimal("61.75")


def _legacy_copy(path, target):
    """The exact shape of the 2026-08-31 production snapshot: before-state only.

    Built by stripping the `repaired` record off a real snapshot rather than by
    hand, so the fixture cannot drift from what the tool writes.
    """
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    for row in payload["scores"]:
        del row["repaired"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    return str(target)


def test_the_snapshot_records_what_the_repair_wrote(app, tmp_path):
    """The `repaired` record is read off the live session after the mutation,
    so recorded == written by construction — this pins that it really is the
    written value and nothing else."""
    _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["scores"][0]["repaired"] == {"municipality": "Vigo"}


def test_a_name_set_by_hand_after_the_repair_survives_restore(app, tmp_path):
    """The SNAPSHOT-001 defect: the old restore overwrote exactly this row.

    Against a before-state snapshot, a row still carrying the repair's write
    and one hand-edited to a third value both `differ`, so the no-op check the
    docstring called compare-and-swap protected neither. The CAS compares the
    row against what the repair *wrote* and leaves the edited row alone,
    naming it.
    """
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    db.session.expire_all()
    edited = db.session.get(Property, prop.id)
    assert edited.municipality == "Vigo"
    edited.municipality = "Redondela"  # the owner corrected it by hand
    db.session.commit()

    outcome = repair_tool.restore(path, apply=True)

    assert outcome["restored"] == 0
    assert outcome["skipped_edited"] == [prop.id]
    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Redondela"


def test_a_later_rescore_does_not_block_the_restore(app, tmp_path):
    """The trap: only the repaired column is compared, never the scores.

    A rescore or a weight change legitimately moves `score_total` without
    anybody touching the name; a CAS that compared the score columns would
    refuse this correct restore.
    """
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    prop.score_total = Decimal("61.75")
    db.session.commit()

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    db.session.expire_all()
    rescored = db.session.get(Property, prop.id)
    rescored.score_total = Decimal("48.25")  # a weight change moved it since
    db.session.commit()

    outcome = repair_tool.restore(path, apply=True)

    assert outcome["restored"] == 1
    assert outcome["skipped_edited"] == []
    db.session.expire_all()
    back = db.session.get(Property, prop.id)
    assert back.municipality == "Teis en Vigo"
    assert back.score_total == Decimal("61.75")


def test_a_legacy_snapshot_is_refused_and_says_why(app, tmp_path):
    """The production file of 2026-08-31 holds only the before-state, in which
    'still the repair's write' and 'hand-edited since' are indistinguishable —
    restoring it can only overwrite both, so it is refused by default."""
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)
    legacy = _legacy_copy(path, tmp_path / "legacy.json")

    with pytest.raises(score_snapshot.SnapshotError, match="cannot be told apart"):
        repair_tool.restore(legacy, apply=True)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Vigo"


def test_the_flag_restores_a_legacy_snapshot_and_names_what_it_overwrote(app, tmp_path):
    """`--overwrite-hand-edits` is the out-loud way past the refusal, and every
    row it overwrites blind is enumerated — an overwrite nobody can list
    afterwards is half the defect back."""
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)
    legacy = _legacy_copy(path, tmp_path / "legacy.json")

    db.session.expire_all()
    edited = db.session.get(Property, prop.id)
    edited.municipality = "Redondela"  # the hand edit the flag agrees to lose
    db.session.commit()

    outcome = repair_tool.restore(legacy, apply=True, overwrite_hand_edits=True)

    assert outcome["restored"] == 1
    assert outcome["overwritten_without_record"] == [prop.id]
    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Teis en Vigo"


def test_a_refused_snapshot_write_leaves_no_repair_pending(app, tmp_path):
    """rx round 1, HIGH: the snapshot is now written after the session
    mutations, and `score_snapshot.write` refuses an existing path with a
    catchable SystemExit. A caller surviving that exception must not be able
    to commit a repair whose rollback point was never written."""
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    path = tmp_path / "snap.json"
    path.write_text("{}", encoding="utf-8")  # the path is already taken

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        repair_tool.repair(apply=True, snapshot_path=str(path), backup=True)
    db.session.commit()  # the caller that survives and commits anyway

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Teis en Vigo"


def test_restore_is_a_dry_run_by_default(app, tmp_path):
    prop = _row(
        app,
        title="Casa adosada en venta en calle Rosa, Teis en Vigo",
        municipality="Teis en Vigo",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    repair_tool.restore(path, apply=False)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).municipality == "Vigo"


def test_every_row_is_scored_against_the_finished_table(app):
    """The rescore must not judge a row against a half-repaired table.

    Scoring reaches `property_comparables.same_municipality()`, which asks the
    table which spellings exist -- a live query, and the session autoflushes
    before it. Renaming and scoring one row at a time therefore scored each row
    against the rows renamed so far: the early ones found no municipality peers,
    fell through the comparables ladder to a wider scope, and were written a
    number the app does not produce from the committed table. Reproduced on six
    rows sharing one municipality, five moved on a plain re-score afterwards.

    Every other test in this file repairs a single row, which is exactly why
    none of them could see it.

    Three details of this fixture are load bearing and were measured, not
    chosen. `property_subtype` must be set, because the comparables ladder
    offers the municipality tier only for a row that has one
    (`property_comparables.py`) -- without it the rename cannot change the peer
    pool and this test passes over the defect instead of at it, which is what
    the first version of it did. There must be **more** rows sharing the
    municipality than the ladder's `min_peers`, so the late rows reach the tier
    the early ones could not. And the peers elsewhere must be the same subtype,
    or the wider scope the early rows fall through to is empty too and every
    row agrees by accident. Against the interleaved form this fixture moves 5
    of 6 rows, the first by 16.56 points.
    """
    districts = ["Teis", "Lavadores", "Cabral", "Coruxo", "Matamá", "Bouzas"]
    for index, district in enumerate(districts):
        _row(
            app,
            title=f"Casa adosada en venta en calle {index}, {district} en Vigo",
            municipality=f"{district} en Vigo",
            source_email_id=f"yaencontre:vigo-{index}",
            url=f"https://www.yaencontre.com/venta/casa/inmueble-1-{index}",
            price=200000 + index * 20000,
            area=200 + index * 10,
            property_category="housing",
            property_subtype="house",
        )
    for index in range(20):
        _row(
            app,
            title=f"Casa en venta en calle Larga {index}, Boiro",
            municipality="Boiro",
            source_email_id=f"peer:{index}",
            url=f"https://www.idealista.com/inmueble/{9000 + index}/",
            price=120000 + index * 9000,
            area=190 + index * 6,
            property_category="housing",
            property_subtype="house",
        )

    repair_tool.repair(apply=True, backup=False)

    db.session.expire_all()
    written = {
        prop.id: prop.score_total
        for prop in db.session.query(Property).filter(Property.municipality == "Vigo")
    }
    assert len(written) == len(districts)

    # Re-score every one of them against the table as it now stands. A row
    # written against the finished table cannot move.
    scorer = PropertyScoringService()
    for prop_id in written:
        scorer.calculate_for_property(db.session.get(Property, prop_id))
    db.session.commit()

    db.session.expire_all()
    moved = {
        prop_id: (written[prop_id], db.session.get(Property, prop_id).score_total)
        for prop_id in written
        if db.session.get(Property, prop_id).score_total != written[prop_id]
    }
    assert not moved, f"scored against a half-repaired table: {moved}"
