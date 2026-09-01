"""The repair for the rows #503 could only fix going forward.

#503 taught the fotocasa reader to read its own URL, so a plot the payload
calls `Residential` is stored as a plot from now on. The rows already in the
table kept what the old parser wrote: measured on production 2026-08-31, 10
rows on a `/comprar/terreno/` URL, 7 of them filed `housing` and 3 already
`land` but still holding `area_type='built'`.

What these tests pin, in the order the things can go wrong:

* the scope is the shipped parser's own reading and nothing else, so a row
  the parser would file the same way today is left alone;
* a dry run writes nothing, because this application cannot delete a property
  and a repair that ran by accident has no undo but the snapshot;
* the score is recomputed, because `scorer_for()` picks the scorer *by*
  `property_category` and the stored 100.00s were the housing scorer's answer
  about a field;
* the snapshot carries the classification beside the scores, and `restore`
  puts back both halves and refuses a row edited since;
* "refuses a row edited since" is a real compare-and-swap now (SNAPSHOT-001):
  the snapshot records what the repair wrote, an edited row is named and left
  alone, a rescore does not block the restore, and `--overwrite-hand-edits`
  is the only way past — naming every row it overwrites.
"""

import json
import pathlib

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils import repair_portal_plot_classification as repair_tool
from utils import score_snapshot

PLOT_URL = "https://www.fotocasa.es/es/comprar/terreno/gozon/bocines/190280914/d"
HOUSE_URL = "https://www.fotocasa.es/es/comprar/vivienda/naron/feal/190540646/d"


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


def _property(app, url, **kwargs):
    prop = Property(
        source_email_id=kwargs.pop("source_email_id", f"fotocasa:{id(url)}"),
        url=url,
        title=kwargs.pop("title", "Residencial en venta en Lugar Susacasa, Gozón"),
        price=54000,
        area=21472,
        search_profile_id=app.config["TEST_PROFILE_ID"],
        **kwargs,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def test_a_plot_filed_as_a_house_is_in_scope(app):
    _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )

    outcome = repair_tool.repair()

    assert outcome["found"] == 1


def test_a_row_already_correct_is_left_alone(app):
    _property(
        app,
        PLOT_URL,
        property_category="land",
        property_subtype="plot",
        area_type="plot",
    )

    assert repair_tool.repair()["found"] == 0


def test_a_dwelling_url_is_never_touched(app):
    """The scope is the portal's own path, not the title or the payload word."""
    _property(
        app,
        HOUSE_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )

    assert repair_tool.repair()["found"] == 0


def test_the_half_broken_shape_is_in_scope_too(app):
    """Live rows 1305 and 1320: classified `land`, still measured as built."""
    _property(
        app,
        PLOT_URL,
        property_category="land",
        property_subtype="plot",
        area_type="built",
    )

    assert repair_tool.repair()["found"] == 1


def test_a_dry_run_writes_nothing(app):
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )

    repair_tool.repair(apply=False)

    db.session.expire_all()
    reread = db.session.get(Property, prop.id)
    assert reread.property_category == "housing"
    assert reread.area_type == "built"


def test_applying_re_files_the_row(app):
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )

    outcome = repair_tool.repair(apply=True, backup=False)

    assert outcome["repaired"] == 1
    db.session.expire_all()
    reread = db.session.get(Property, prop.id)
    assert reread.property_category == "land"
    assert reread.property_subtype == "plot"
    assert reread.area_type == "plot"


def test_the_score_is_recomputed_not_carried_over(app):
    """`scorer_for()` selects by category, so the old number was another
    scorer's answer. Leaving it would be a judgement about a row that no
    longer exists."""
    from decimal import Decimal

    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    prop.score_total = Decimal("100.00")
    prop.scoring = {"stale": True}
    db.session.commit()

    repair_tool.repair(apply=True, backup=False)

    db.session.expire_all()
    reread = db.session.get(Property, prop.id)
    assert reread.scoring != {"stale": True}


def test_the_snapshot_carries_the_classification_and_restores_both_halves(
    app, tmp_path
):
    from decimal import Decimal

    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    prop.score_total = Decimal("100.00")
    db.session.commit()

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    row = payload["scores"][0]
    assert row["property_category"] == "housing"
    assert row["area_type"] == "built"
    assert row["score_total"] == "100.00"

    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "land"

    repair_tool.restore(path, apply=True)

    db.session.expire_all()
    back = db.session.get(Property, prop.id)
    assert back.property_category == "housing"
    assert back.area_type == "built"
    assert back.score_total == Decimal("100.00")


def test_the_snapshot_records_what_the_repair_wrote(app, tmp_path):
    """The `repaired` record is read off the live session after the mutation —
    recorded == written by construction, never a prediction of
    `reconcile_area_type`'s answer kept in sync by hope."""
    _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["scores"][0]["repaired"] == {
        "property_category": "land",
        "property_subtype": "plot",
        "area_type": "plot",
    }


def test_a_classification_set_by_hand_after_the_repair_survives_restore(app, tmp_path):
    """The SNAPSHOT-001 defect: the old restore overwrote exactly this row."""
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    db.session.expire_all()
    edited = db.session.get(Property, prop.id)
    assert edited.property_category == "land"
    edited.property_category = "commercial"  # the owner re-filed it by hand
    db.session.commit()

    outcome = repair_tool.restore(path, apply=True)

    assert outcome["restored"] == 0
    assert outcome["skipped_edited"] == [prop.id]
    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "commercial"


def test_a_later_rescore_does_not_block_the_restore(app, tmp_path):
    """Only the repaired columns are compared, never the scores: a rescore
    moves `score_total` without touching the classification, and a guard over
    it would refuse this correct restore."""
    from decimal import Decimal

    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    prop.score_total = Decimal("100.00")
    db.session.commit()

    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    db.session.expire_all()
    rescored = db.session.get(Property, prop.id)
    rescored.score_total = Decimal("12.50")  # a weight change moved it since
    db.session.commit()

    outcome = repair_tool.restore(path, apply=True)

    assert outcome["restored"] == 1
    assert outcome["skipped_edited"] == []
    db.session.expire_all()
    back = db.session.get(Property, prop.id)
    assert back.property_category == "housing"
    assert back.score_total == Decimal("100.00")


def test_a_legacy_snapshot_is_refused_and_the_flag_is_the_way_past(app, tmp_path):
    """A before-state-only snapshot cannot tell 'still the repair's write'
    from 'hand-edited since'; it is refused by default, restored behind the
    flag, and every blind overwrite is enumerated."""
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    for row in payload["scores"]:
        del row["repaired"]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(score_snapshot.SnapshotError, match="cannot be told apart"):
        repair_tool.restore(str(legacy), apply=True)
    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "land"

    outcome = repair_tool.restore(str(legacy), apply=True, overwrite_hand_edits=True)

    assert outcome["restored"] == 1
    assert outcome["overwritten_without_record"] == [prop.id]
    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "housing"


def test_the_flag_overwrites_the_row_the_cas_skipped_and_names_it(app, tmp_path):
    """On a CAS-capable snapshot the flag is the second, deliberate pass: the
    operator read the skip report and decided the edited row goes back too —
    and the output still names it, as measured rather than blind."""
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    db.session.expire_all()
    edited = db.session.get(Property, prop.id)
    edited.property_category = "commercial"
    db.session.commit()

    outcome = repair_tool.restore(path, apply=True, overwrite_hand_edits=True)

    assert outcome["restored"] == 1
    assert outcome["overwritten_edited"] == [prop.id]
    assert outcome["overwritten_without_record"] == []
    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "housing"


def test_the_record_is_data_about_the_repair_not_a_column(app):
    """`repaired` must be skipped by `differs` and `apply_row` the way `id` is,
    or it gets compared against — and written onto — the model."""
    prop = _property(
        app,
        PLOT_URL,
        property_category="land",
        property_subtype="plot",
        area_type="plot",
    )
    parsed = score_snapshot.parse_row(
        {
            "id": prop.id,
            "property_category": "land",
            "repaired": {"property_category": "land"},
        }
    )

    # Compared as a column, `getattr(prop, "repaired")` raises; treated as
    # data, the only real column agrees and nothing differs.
    assert score_snapshot.differs(prop, parsed) is False

    score_snapshot.apply_row(prop, parsed)
    assert not hasattr(prop, "repaired")


def test_a_repaired_record_may_not_carry_scores_or_be_empty():
    """Scores are excluded from the CAS by design (a rescore is legitimate),
    and an empty record would pass every comparison vacuously."""
    with pytest.raises(score_snapshot.SnapshotError, match="columns a repair sets"):
        score_snapshot.parse_row(
            {
                "id": 1,
                "property_category": "land",
                "repaired": {"score_total": "50"},
            }
        )
    with pytest.raises(
        score_snapshot.SnapshotError, match="not a record of what the repair wrote"
    ):
        score_snapshot.parse_row({"id": 1, "property_category": "land", "repaired": {}})
    # A row whose only payload is the record guards a column it does not
    # restore, which is the over-coverage refusal below.
    with pytest.raises(score_snapshot.SnapshotError, match="does not restore"):
        score_snapshot.parse_row({"id": 1, "repaired": {"property_category": "land"}})


def test_a_repaired_record_must_cover_exactly_what_the_row_restores():
    """rx round 1, HIGH: a record guarding only `property_category` on a row
    that also restores `property_subtype` leaves the subtype to be overwritten
    CAS-unchecked — a hand-edited subtype would be lost without the flag. The
    other direction is the guard-too-wide: a record guarding `municipality` on
    a row that does not restore it would skip the row over drift the restore
    would never touch. Both shapes are refused at parse time, so the whole
    file restores nothing rather than half-guarding."""
    with pytest.raises(score_snapshot.SnapshotError, match="does not cover"):
        score_snapshot.parse_row(
            {
                "id": 1,
                "property_category": "land",
                "property_subtype": "plot",
                "repaired": {"property_category": "land"},
            }
        )
    with pytest.raises(score_snapshot.SnapshotError, match="does not restore"):
        score_snapshot.parse_row(
            {
                "id": 1,
                "property_category": "land",
                "repaired": {"property_category": "land", "municipality": "Vigo"},
            }
        )


def test_a_refused_snapshot_write_leaves_no_repair_pending(app, tmp_path):
    """rx round 1, HIGH: the snapshot is now written after the session
    mutations, and `score_snapshot.write` refuses an existing path with a
    catchable SystemExit. A caller surviving that exception must not be able
    to commit a repair whose rollback point was never written — the repair
    rolls the session back before the exception leaves."""
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    path = tmp_path / "snap.json"
    path.write_text("{}", encoding="utf-8")  # the path is already taken

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        repair_tool.repair(apply=True, snapshot_path=str(path), backup=True)
    db.session.commit()  # the caller that survives and commits anyway

    db.session.expire_all()
    reread = db.session.get(Property, prop.id)
    assert reread.property_category == "housing"
    assert reread.area_type == "built"


def test_restore_is_a_dry_run_by_default(app, tmp_path):
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    path = str(tmp_path / "snap.json")
    repair_tool.repair(apply=True, snapshot_path=path, backup=True)

    repair_tool.restore(path, apply=False)

    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "land"


def test_a_row_the_classifier_will_not_call_land_is_reported_not_forced(
    app, monkeypatch
):
    """The repair asks the shipped classifier; it does not overrule it.

    A tool that writes "land" whatever the code says is a second copy of the
    classification decision, and the subscription whose rules disagree would
    then fight its own ingest.
    """
    prop = _property(
        app,
        PLOT_URL,
        property_category="housing",
        property_subtype="house",
        area_type="built",
    )
    monkeypatch.setattr(
        repair_tool, "_classification_now", lambda _p: ("commercial", "retail")
    )

    outcome = repair_tool.repair(apply=True, backup=False)

    assert outcome["found"] == 1
    assert outcome["repaired"] == 0
    assert outcome["skipped"] == 1
    db.session.expire_all()
    assert db.session.get(Property, prop.id).property_category == "housing"


def test_a_snapshot_naming_a_column_the_app_does_not_restore_is_refused(tmp_path):
    """The snapshot module's own rule, exercised through the widened set."""
    # The row carries a restorable column too, or the refusal that fires is
    # "restores nothing" and the test passes without the rule under it.
    with pytest.raises(score_snapshot.SnapshotError, match="unknown column"):
        score_snapshot.parse_row(
            {"id": 1, "score_total": "50", "title": "Finca en Gozón"}
        )

    parsed = score_snapshot.parse_row({"id": 1, "property_category": "land"})
    assert parsed["property_category"] == "land"
