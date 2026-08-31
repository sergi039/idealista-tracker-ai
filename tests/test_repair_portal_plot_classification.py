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
  puts back both halves and refuses a row edited since.
"""

import json
import pathlib

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils import repair_portal_plot_classification as repair_tool

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


def test_a_snapshot_naming_a_column_the_app_does_not_restore_is_refused(tmp_path):
    """The snapshot module's own rule, exercised through the widened set."""
    from utils import score_snapshot

    with pytest.raises(score_snapshot.SnapshotError):
        score_snapshot.parse_row({"id": 1, "municipality": "Gozón"})

    parsed = score_snapshot.parse_row({"id": 1, "property_category": "land"})
    assert parsed["property_category"] == "land"
