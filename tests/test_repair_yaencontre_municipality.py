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
  back and leaves a row that no longer differs alone.
"""

import json
import pathlib
from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils import repair_yaencontre_municipality as repair_tool

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
        price=205000,
        area=205,
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
