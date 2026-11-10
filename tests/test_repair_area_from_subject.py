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
from utils.idealista_extractors import extract_price

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


def _row(
    n,
    *,
    description,
    subject=SUBJECT,
    area=150,
    area_type="built",
    url=None,
    price="first",
):
    if price == "first":  # the row's price is the description's first amount
        price = extract_price(description.split("€")[0] + "€")
    prop = Property(
        price=price,
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
        "no_card": 0,
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
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [
        "snap.json",
        "snap.json.journal",
    ]


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


def test_two_blocks_at_the_rows_price_with_different_areas_are_ambiguous(app, tmp_path):
    pid = _row(
        9, description="320.000 € 4 hab. 316 m² 320.000 € 3 hab. 90 m²", area=150
    )
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["ambiguous"] == 1
    assert _state(pid) == (150.0, "built")


def test_a_card_without_an_area_never_borrows_the_next_cards(app, tmp_path):
    """Finding 3: card 111 has no area, card 222 states 90."""
    pid = _row(
        11,
        description="320.000 € 4 hab. Chalet B 99.000 € 3 hab. 90 m²",
        area=150,
    )
    tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert _state(pid) == (None, "unknown")


def test_a_description_with_links_is_read_by_the_rows_own_card(app, tmp_path):
    pid = _row(
        12,
        description=(
            "Tu búsqueda 350.000 € 150 m² /inmueble/12/ 320.000 € 4 hab. 316 m² "
            "/inmueble/99/ 99.000 € 3 hab. 90 m²"
        ),
        area=150,
        price=320000,
    )
    tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert _state(pid) == (316.0, "built")


def test_a_description_with_no_price_block_is_not_written(app, tmp_path):
    pid = _row(13, description="Chalet con vistas. Consulte al anunciante.")
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["no_card"] == 1
    assert _state(pid) == (150.0, "built")


def test_a_skipped_row_is_not_restored_over_an_independent_correction(
    rows, tmp_path, monkeypatch
):
    """Finding 4: another writer sets the planned value before the lock."""
    real_plan = tool._plan

    def plan_then_correct():
        plan = real_plan()
        db.session.get(Property, rows["recovered"]).area = 316
        db.session.commit()
        return plan

    monkeypatch.setattr(tool, "_plan", plan_then_correct)
    snap = tmp_path / "s.json"
    tool.repair(apply=True, snapshot_path=str(snap))

    saved = json.loads(snap.read_text())
    status = {r["id"]: r["status"] for r in saved["rows"]}
    assert saved["phase"] == "final"
    assert status[rows["recovered"]] == "skipped"
    written = [r for r in saved["rows"] if r["status"] == "written"]
    assert all("written_area" in r for r in written)

    tool.restore(str(snap), apply=True)

    assert _state(rows["recovered"]) == (316.0, "built")
    assert _state(rows["unstated"]) == (150.0, "built")


def test_the_plan_is_on_disk_before_the_first_write(rows, tmp_path, monkeypatch):
    """Finding 4: phase one exists, marked planned, when the first row changes."""
    snap = tmp_path / "s.json"
    seen = {}
    real_locked = tool._locked

    def spy(prop_id):
        if "phase" not in seen:
            seen.update(json.loads(snap.read_text()))
        return real_locked(prop_id)

    monkeypatch.setattr(tool, "_locked", spy)
    tool.repair(apply=True, snapshot_path=str(snap))

    assert seen["phase"] == "planned"
    assert {r["status"] for r in seen["rows"]} == {"planned"}


def _planned_snapshot(snap):
    todo = [e for e in tool._plan() if e["outcome"] in tool.WRITES]
    snap.write_text(
        json.dumps(
            {"phase": "planned", "rows": [{**e, "status": "planned"} for e in todo]}
        )
    )
    return {e["id"]: e for e in todo}


def _journal_line(entry):
    return json.dumps(
        {
            "id": entry["id"],
            "old_area": entry["old_area"],
            "old_area_type": entry["old_area_type"],
            "written_area": entry["new_area"],
            "written_area_type": entry["new_area_type"],
        }
    )


def test_an_independent_correction_survives_an_interrupted_run(
    rows, tmp_path, monkeypatch
):
    """Plan 150 -> 316; someone sets 316; the lock skips it; the run dies."""
    real_plan = tool._plan

    def plan_then_correct():
        plan = real_plan()
        db.session.get(Property, rows["recovered"]).area = 316
        db.session.commit()
        return plan

    monkeypatch.setattr(tool, "_plan", plan_then_correct)
    snap = tmp_path / "s.json"
    real_final = tool._write_snapshot

    def die_before_final(payload, path):
        if payload["phase"] == "final":
            raise KeyboardInterrupt
        real_final(payload, path)

    monkeypatch.setattr(tool, "_write_snapshot", die_before_final)
    with pytest.raises(KeyboardInterrupt):
        tool.repair(apply=True, snapshot_path=str(snap))
    assert json.loads(snap.read_text())["phase"] == "planned"

    done = tool.restore(str(snap), apply=True)

    assert _state(rows["recovered"]) == (316.0, "built")
    assert rows["recovered"] in [r["id"] for r in done["not_journaled"]]
    assert sorted(done["restored"]) == sorted([rows["unstated"], rows["plot"]])
    assert _state(rows["unstated"]) == (150.0, "built")


def test_an_interrupted_run_restores_exactly_the_journaled_rows(rows, tmp_path):
    snap = tmp_path / "s.json"
    planned = _planned_snapshot(snap)
    journaled = [rows["recovered"], rows["unstated"]]
    (tmp_path / "s.json.journal").write_text(
        "\n".join(_journal_line(planned[i]) for i in journaled) + "\n"
    )
    for i in journaled:
        prop = db.session.get(Property, i)
        prop.area = planned[i]["new_area"]
        prop.area_type = planned[i]["new_area_type"]
    db.session.commit()

    done = tool.restore(str(snap), apply=True)

    assert sorted(done["restored"]) == sorted(journaled)
    assert [r["id"] for r in done["not_journaled"]] == [rows["plot"]]
    assert _state(rows["recovered"]) == (150.0, "built")
    assert _state(rows["unstated"]) == (150.0, "built")
    assert _state(rows["plot"]) == (150.0, "plot")


def test_a_torn_last_journal_line_is_ignored_and_reported(rows, tmp_path):
    snap = tmp_path / "s.json"
    planned = _planned_snapshot(snap)
    rid = rows["recovered"]
    (tmp_path / "s.json.journal").write_text(
        _journal_line(planned[rid]) + '\n{"id": ' + str(rows["unstated"]) + ', "old_a'
    )
    prop = db.session.get(Property, rid)
    prop.area = 316
    db.session.get(Property, rows["unstated"]).area = None
    db.session.commit()

    done = tool.restore(str(snap), apply=True)

    assert done["restored"] == [rid]
    assert done["unreadable_journal_lines"] == 1
    assert rows["unstated"] in [r["id"] for r in done["not_journaled"]]
    assert _state(rows["unstated"]) == (None, "built")


def test_apply_refuses_when_the_journal_path_exists(rows, tmp_path):
    snap = tmp_path / "s.json"
    (tmp_path / "s.json.journal").write_text("earlier")

    with pytest.raises(FileExistsError):
        tool.repair(apply=True, snapshot_path=str(snap))

    assert not snap.exists()
    assert _state(rows["recovered"]) == (150.0, "built")


def test_a_full_run_journals_every_written_row(rows, tmp_path):
    snap = tmp_path / "s.json"
    outcome = tool.repair(apply=True, snapshot_path=str(snap))
    lines = (tmp_path / "s.json.journal").read_text().splitlines()

    assert sorted(json.loads(line)["id"] for line in lines) == sorted(
        outcome["written"]
    )
    tool.restore(str(snap), apply=True)
    assert _state(rows["recovered"]) == (150.0, "built")


def test_a_row_whose_subject_vanished_before_the_lock_is_not_written(
    rows, tmp_path, monkeypatch
):
    """Finding 5: the full scope is re-evaluated under the lock."""
    real_plan = tool._plan

    def plan_then_blank():
        plan = real_plan()
        db.session.get(Property, rows["recovered"]).email_subject = ""
        db.session.commit()
        return plan

    monkeypatch.setattr(tool, "_plan", plan_then_blank)
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert rows["recovered"] in outcome["skipped_changed"]
    assert _state(rows["recovered"]) == (150.0, "built")


def test_a_changed_description_changes_the_outcome_and_the_row_is_skipped(
    rows, tmp_path, monkeypatch
):
    real_plan = tool._plan

    def plan_then_edit():
        plan = real_plan()
        db.session.get(Property, rows["recovered"]).description = "320.000 € 4 hab."
        db.session.commit()
        return plan

    monkeypatch.setattr(tool, "_plan", plan_then_edit)
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert rows["recovered"] in outcome["skipped_changed"]
    assert _state(rows["recovered"]) == (150.0, "built")


def test_an_existing_snapshot_is_never_overwritten(rows, tmp_path):
    """Finding 6."""
    snap = tmp_path / "s.json"
    snap.write_text("earlier run")

    with pytest.raises(FileExistsError):
        tool.repair(apply=True, snapshot_path=str(snap))

    assert snap.read_text() == "earlier run"
    assert _state(rows["recovered"]) == (150.0, "built")


def test_the_command_line_refuses_an_existing_snapshot(rows, tmp_path):
    snap = tmp_path / "s.json"
    snap.write_text("earlier run")

    with pytest.raises(SystemExit) as exit_info:
        tool.main(["--apply", "--snapshot", str(snap)])

    assert exit_info.value.code != 0
    assert snap.read_text() == "earlier run"


# --- real shapes found on production data (2026-11-10) ----------------------

PREHEADER_3943 = (
    "Alertas Casa o chalet independiente en Cornazo - Rubianes, Vilagarcía de "
    "Arousa 320.000 € . Os presentamos esta vivienda unifamiliar situada en un "
    "entorno residencial tranquilo, … Una propiedad que combina amplitu... "
    "Hola, Sergioalicante, 1 anuncio recién publicado con tus criterios "
    "320.000 € 1.013 €/m² 4 hab. 316 m² Os presentamos esta vivienda unifamiliar s"
)
PREHEADER_3848 = (
    "Alertas Casa o chalet independiente en Redondela 299.000 € . SE VENDE CASA "
    "VIVIENDA EN CESANTES - … - 5 HABITACIONES - 2 COCINAS - COMEDOR - 3 BAÑOS "
    "- 1... Hola, Sergioalicante, 1 anuncio recién publicado con tus criterios "
    "299.000 € 1.634 €/m² 5 hab. 183 m² SE VENDE CASA "
)
PREHEADER_1537 = (
    "Alertas Casa o chalet independiente en Lugar Costenla, 31, Carballo "
    "349.000 € . Cuando vengas a visitarla … Hola, Sergioalicante, 1 anuncio "
    "recién publicado con tus criterios 349.000 € 1.058 €/m² 4 hab. 330 m² "
    "Cuando vengas "
)
ENGLISH_407 = (
    "Hello Sergioalicante, 1 new listing that matches your search criteria "
    "58,000 € 1,500 m2 Developed plot ? NEW BUILDING PLOT FOR SALE IN TIÑANA, "
)


@pytest.mark.parametrize(
    "n, description, price, area, area_type, expected",
    [
        (3943, PREHEADER_3943, 320000, 150, "built", (316.0, "built")),
        (3848, PREHEADER_3848, 299000, 150, "built", (183.0, "built")),
        (1537, PREHEADER_1537, 349000, 150, "built", (330.0, "built")),
        (407, ENGLISH_407, 58000, 1000, "plot", (1500.0, "plot")),
    ],
)
def test_real_preheader_descriptions_recover_the_cards_area(
    app, tmp_path, n, description, price, area, area_type, expected
):
    pid = _row(
        n,
        description=description,
        price=price,
        area=area,
        area_type=area_type,
        subject="Nuevo en tu búsqueda: costa - 150m2 - 1000m2",
    )
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["recovered"] == 1
    assert _state(pid) == expected


def test_a_block_at_another_price_never_supplies_the_area(app, tmp_path):
    """Card 222 at 99.000 € cannot repair row 111 at 320.000 €."""
    pid = _row(
        111,
        price=320000,
        description=(
            "320.000 € . teaser Hola 320.000 € 4 hab. Chalet B 99.000 € 3 hab. 90 m²"
        ),
    )
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["unstated"] == 1
    assert _state(pid) == (None, "unknown")


def test_a_row_with_no_block_at_its_price_is_no_card(app, tmp_path):
    other = _row(21, price=500000, description="320.000 € 4 hab. 316 m²")
    nothing = _row(22, price=None, description="320.000 € 4 hab. 316 m²")
    outcome = tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert outcome["counts"]["no_card"] == 2
    assert _state(other) == (150.0, "built")
    assert _state(nothing) == (150.0, "built")


def test_a_price_drop_description_matches_on_the_new_price(app, tmp_path):
    pid = _row(
        23,
        price=312000,
        description=(
            "Hola 325.000€ ↓4% 312.000 € 910 €/m² 6 hab. 357 m² Chalet independiente"
        ),
    )
    tool.repair(apply=True, snapshot_path=str(tmp_path / "s.json"))

    assert _state(pid) == (357.0, "built")
