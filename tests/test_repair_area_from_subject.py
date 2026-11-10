"""#645 repair: undo an area that was copied from an e-mail subject.

The descriptions are reconstructed from stored shapes ("320.000 € 1.013 €/m²
4 hab. 316 m²"), not captured e-mails.
"""

import json

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils import repair_area_from_subject as tool

SUBJECT = "¡Nuevo chalet en tu búsqueda: Rias Bajas costa - 350k - 150m2!"
URL = "https://www.idealista.com/inmueble/{}/"


@pytest.fixture
def app(tmp_path, monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture(autouse=True)
def _inflight_in_tmp(tmp_path, monkeypatch):
    """The real `inflight` wrapper runs; only its marker directory moves."""
    import utils.inflight as inflight_module

    monkeypatch.setattr(inflight_module, "INFLIGHT_DIRNAME", str(tmp_path / "inflight"))


def _row(n, *, description, subject=SUBJECT, area=150, area_type="built", url=None):
    prop = Property(
        source_email_id=f"imap_{n}",
        idealista_property_id=n,
        url=url or URL.format(n),
        email_subject=subject,
        description=description,
        area=area,
        area_type=area_type,
    )
    db.session.add(prop)
    db.session.commit()
    return prop.id


@pytest.fixture
def rows(app):
    return {
        "recovered": _row(1, description="320.000 € 1.013 €/m² 4 hab. 316 m²"),
        "confirmed": _row(2, description="99,000 € 309 €/m² 6 bed. 150 m2"),
        "unstated": _row(3, description="320.000 € 1.013 €/m² 4 hab."),
        "nosubject": _row(4, description="320.000 € 4 hab. 316 m²", subject=""),
        "other_area": _row(5, description="320.000 € 4 hab. 316 m²", area=200),
        "fotocasa": _row(
            6,
            description="320.000 € 4 hab. 316 m²",
            url="https://www.fotocasa.es/x/6",
        ),
        "plot": _row(7, description="59.000 € 118 €/m²", area_type="plot", area=150),
    }


def _state(prop_id):
    prop = db.session.get(Property, prop_id)
    db.session.refresh(prop)
    return (float(prop.area) if prop.area is not None else None, prop.area_type)


def test_a_report_writes_nothing(rows):
    before = {k: _state(v) for k, v in rows.items()}
    outcome = tool.repair(apply=False)

    assert outcome["counts"] == {
        "recovered": 1,
        "confirmed": 1,
        "unstated": 2,
        "ambiguous": 0,
        "no_description": 0,
    }
    assert {k: _state(v) for k, v in rows.items()} == before
    assert outcome["applied"] is False


def test_scope_is_the_subject_figure_on_an_idealista_row_only(rows):
    ids = {e["id"] for e in tool.repair()["rows"]}

    assert ids == {rows["recovered"], rows["confirmed"], rows["unstated"], rows["plot"]}


def test_apply_writes_the_outcomes_and_a_snapshot(rows, tmp_path):
    snap = tmp_path / "snap.json"
    outcome = tool.repair(apply=True, snapshot_path=str(snap))

    assert _state(rows["recovered"]) == (316.0, "built")
    assert _state(rows["confirmed"]) == (150.0, "built")
    assert _state(rows["unstated"]) == (None, "unknown")
    # a plot stays a plot: area_type records what the listing is
    assert _state(rows["plot"]) == (None, "plot")
    # out of scope, untouched
    assert _state(rows["nosubject"]) == (150.0, "built")
    assert _state(rows["other_area"]) == (200.0, "built")
    assert _state(rows["fotocasa"]) == (150.0, "built")

    saved = json.loads(snap.read_text())
    assert {r["id"]: (r["old_area"], r["old_area_type"]) for r in saved["rows"]} == {
        rows["recovered"]: (150.0, "built"),
        rows["unstated"]: (150.0, "built"),
        rows["plot"]: (150.0, "plot"),
    }
    assert sorted(outcome["written"]) == sorted(r["id"] for r in saved["rows"])
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["snap.json"]


def test_apply_without_a_snapshot_path_refuses_before_writing(rows):
    with pytest.raises(ValueError):
        tool.repair(apply=True, snapshot_path="")
    assert _state(rows["recovered"]) == (150.0, "built")


def test_a_second_apply_is_a_no_op(rows, tmp_path):
    tool.repair(apply=True, snapshot_path=str(tmp_path / "a.json"))
    again = tool.repair(apply=True, snapshot_path=str(tmp_path / "b.json"))

    assert again["written"] == []
    assert not (tmp_path / "b.json").exists()


def test_a_row_changed_between_preview_and_apply_is_skipped(
    rows, tmp_path, monkeypatch
):
    real_plan = tool._plan

    def plan_then_edit():
        plan = real_plan()
        db.session.get(Property, rows["recovered"]).area = 777
        db.session.commit()
        return plan

    monkeypatch.setattr(tool, "_plan", plan_then_edit)
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["skipped_changed"] == [rows["recovered"]]
    assert _state(rows["recovered"]) == (777.0, "built")
    assert _state(rows["unstated"]) == (None, "unknown")


def test_restore_puts_values_back_and_refuses_a_row_changed_after(rows, tmp_path):
    snap = tmp_path / "snap.json"
    tool.repair(apply=True, snapshot_path=str(snap))
    db.session.get(Property, rows["recovered"]).area = 999
    db.session.commit()

    dry = tool.restore(str(snap))
    assert dry["applied"] is False
    assert _state(rows["unstated"]) == (None, "unknown")

    done = tool.restore(str(snap), apply=True)

    assert done["changed_since_repair"] == [rows["recovered"]]
    assert sorted(done["restored"]) == sorted([rows["unstated"], rows["plot"]])
    assert _state(rows["recovered"]) == (999.0, "built")
    assert _state(rows["unstated"]) == (150.0, "built")
    assert _state(rows["plot"]) == (150.0, "plot")


def test_several_different_cards_are_reported_and_never_written(app, tmp_path):
    pid = _row(9, description="320.000 € 4 hab. 316 m² 99.000 € 3 hab. 90 m²", area=150)
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["ambiguous"] == 1
    assert _state(pid) == (150.0, "built")
