"""#634: the plot of a house that states none, from the parcel an Enrich press placed.

Nothing here touches the network: the parcel's area is a stub, or the real
polygon of the owner's row 969 read from the committed Catastro answer.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from shapely.geometry import box
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services import (  # noqa: E402
    cadastre_service as c,
    neighbours_service as n,
    plot_cadastre as pc,
    subscription_criteria as sc,
)
from services.property_enrichment_service import PropertyEnrichmentService  # noqa: E402

REF = "4463719NH1946S"
DATA = Path(__file__).parent / "data" / "neighbours"
CRITERIA = {"min_house_m2": 150, "min_plot_m2": 700, "max_price_eur": 300000}


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _row(**kwargs):
    fields = dict(
        source_email_id="plot-cadastre",
        property_category="housing",
        property_subtype="house",
        price=250000,
        currency="EUR",
        area=200,
        area_type="built",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    fields.update(kwargs)
    p = Property(**fields)
    db.session.add(p)
    db.session.commit()
    return p


def _placed(p, **block):
    """What the neighbour step records once it has established the parcel."""
    enrichment = dict(p.enrichment or {})
    enrichment["neighbours"] = {
        "status": "ok",
        "subject": n.subject(p),
        "reference": REF,
        **block,
    }
    p.enrichment = enrichment
    db.session.commit()


@pytest.fixture
def area(monkeypatch):
    stub = Mock(return_value=1234.5)
    monkeypatch.setattr(pc, "parcel_area_m2", stub)
    return stub


def test_the_parcel_area_becomes_the_plot_with_its_source(app, area):
    p = _row(enrichment={"pool": {"status": "ok"}})
    _placed(p)
    block = pc.enrich(p)
    assert block["value"] == 1234.5 and block["reference"] == REF
    assert block["source"] == pc.SOURCE
    assert block["extractor_version"] == pc.EXTRACTOR_VERSION
    db.session.refresh(p)
    assert float(p.plot_area) == 1234.5
    assert p.enrichment[pc.ENRICHMENT_KEY] == block
    assert p.enrichment["neighbours"]["status"] == "ok"
    assert p.enrichment["pool"] == {"status": "ok"}  # a sibling block survives
    area.assert_called_once_with(REF)


def test_the_plot_and_its_source_are_written_together_or_not_at_all(
    app, area, monkeypatch
):
    p = _row()
    _placed(p)
    monkeypatch.setattr(
        db.session, "commit", Mock(side_effect=RuntimeError("commit refused"))
    )
    with pytest.raises(RuntimeError):
        pc.enrich(p)
    monkeypatch.undo()
    db.session.refresh(p)
    assert p.plot_area is None
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_no_commit_ever_carries_one_of_the_two_writes_without_the_other(
    app, area, monkeypatch
):
    p = _row()
    _placed(p)
    seen = []
    real_commit = db.session.commit

    def spy():
        seen.append((p.plot_area is not None, pc.ENRICHMENT_KEY in p.enrichment))
        return real_commit()

    monkeypatch.setattr(db.session, "commit", spy)
    assert pc.enrich(p) is not None
    assert seen and all(plot == source for plot, source in seen)


def test_the_written_plot_decides_the_subscription_criteria(app, area):
    p = _row()
    _placed(p)
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is None
    pc.enrich(p)
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is True


@pytest.mark.parametrize("stated", [900, 100])
def test_a_stated_plot_is_never_overwritten_and_nothing_is_asked(app, area, stated):
    p = _row(plot_area=stated)
    _placed(p)
    assert pc.enrich(p) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert float(p.plot_area) == stated
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_a_plot_stated_while_catastro_answers_wins_under_the_lock(app, monkeypatch):
    p = _row()
    _placed(p)

    def slow(reference):
        db.session.execute(
            text("UPDATE properties SET plot_area=950 WHERE id=:i"), {"i": p.id}
        )
        return 1234.5

    monkeypatch.setattr(pc, "parcel_area_m2", slow)
    assert pc.enrich(p) is None
    db.session.refresh(p)
    assert float(p.plot_area) == 950
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_a_row_that_moved_while_catastro_answered_is_not_written(app, monkeypatch):
    p = _row()
    _placed(p)

    def moved(reference):
        db.session.execute(
            text("UPDATE properties SET location_lat=43.6 WHERE id=:i"), {"i": p.id}
        )
        return 1234.5

    monkeypatch.setattr(pc, "parcel_area_m2", moved)
    assert pc.enrich(p) is None
    db.session.refresh(p)
    assert p.plot_area is None


@pytest.mark.parametrize(
    "kind",
    [
        dict(property_subtype="apartment"),
        dict(property_subtype="penthouse"),
        dict(property_category=None, property_subtype=None),
    ],
    ids=["flat", "penthouse", "kind_unknown"],
)
def test_only_a_house_reads_a_plot_from_its_parcel(app, area, kind):
    p = _row(**kind)
    _placed(p)
    assert pc.enrich(p) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert p.plot_area is None


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"status": "no_parcel", "subject": "x"},
        {"status": "unavailable", "reason": "parcel_lookup_failed"},
        {"status": "ok", "subject": "not-this-row's-subject", "reference": REF},
        {"status": "ok", "reference": REF},
        {"status": "ok", "subject": "set below", "reference": "not a reference"},
    ],
    ids=[
        "never_measured",
        "no_parcel",
        "unavailable",
        "stale_subject",
        "no_subject",
        "unreadable_reference",
    ],
)
def test_a_parcel_the_press_did_not_establish_leaves_the_plot_unknown(app, area, block):
    p = _row()
    if block is not None:
        if block.get("subject") == "set below":
            block = {**block, "subject": n.subject(p)}
        p.enrichment = {"neighbours": block}
        db.session.commit()
    assert pc.enrich(p) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert p.plot_area is None
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is None


def test_a_catastro_refusal_writes_nothing_and_is_not_a_zero(app, monkeypatch):
    p = _row()
    _placed(p)
    monkeypatch.setattr(
        pc, "parcel_area_m2", Mock(side_effect=c.CadastreError(c.REFUSED, "HTTP 503"))
    )
    assert pc.enrich(p) is None
    db.session.refresh(p)
    assert p.plot_area is None
    assert pc.ENRICHMENT_KEY not in p.enrichment


def _stub_outline(monkeypatch, shape):
    monkeypatch.setattr(n, "_metric_parcel", lambda reference: (shape, 25829))


def test_the_area_is_the_checked_polygon_of_the_real_parcel(monkeypatch):
    shape = n._shape(
        c._parse_gml((DATA / "969_parcel.gml").read_text(), 25829)["polygons"]
    )
    _stub_outline(monkeypatch, shape)
    value = pc.parcel_area_m2(REF)
    assert value == round(shape.area, 1) and value > 0


@pytest.mark.parametrize(
    "width, height",
    [(5e8, 1), (1e5, 1e5)],
    ids=["credible_but_over_the_column", "not_credible"],
)
def test_an_area_that_cannot_be_a_plot_is_refused(monkeypatch, width, height):
    _stub_outline(monkeypatch, box(0, 0, width, height))
    with pytest.raises(c.CadastreError):
        pc.parcel_area_m2(REF)


def test_an_empty_or_nan_polygon_is_refused(monkeypatch):
    _stub_outline(monkeypatch, box(0, 0, 0, 0))
    with pytest.raises(c.CadastreError):
        pc.parcel_area_m2(REF)


def test_the_press_runs_it_after_the_neighbour_step_and_survives_its_failure(
    monkeypatch,
):
    order = []
    service = PropertyEnrichmentService(neighbours_service=Mock())
    service.neighbours_service.enrich.side_effect = lambda *a, **k: order.append("n")
    monkeypatch.setattr(
        "services.property_enrichment_service.advertiser.enrich", lambda *_a, **_k: None
    )
    monkeypatch.setattr(service, "enrich_free_sources", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "services.property_enrichment_service.plot_cadastre.enrich",
        lambda prop, commit: order.append(("plot", commit)),
    )
    service._advisory_pass(Mock(), use_ai=False, with_pool=False)
    assert order == ["n", ("plot", True)]


def test_a_failing_plot_step_ends_its_transaction_and_does_not_fail_the_press(
    app, monkeypatch
):
    """The step can fail after it took the row `FOR UPDATE`: the press ends that
    transaction (rollback) rather than carry the lock into the steps after it."""
    p = _row()
    service = PropertyEnrichmentService(neighbours_service=Mock())
    monkeypatch.setattr(
        "services.property_enrichment_service.advertiser.enrich", lambda *_a, **_k: None
    )
    monkeypatch.setattr(service, "enrich_free_sources", lambda *_a, **_k: None)

    def takes_the_row_then_fails(prop, commit):
        db.session.refresh(prop, with_for_update=True)
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "services.property_enrichment_service.plot_cadastre.enrich",
        takes_the_row_then_fails,
    )
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)
    service._advisory_pass(p, use_ai=False, with_pool=False)
    rollback.assert_called_once()


def test_a_cache_miss_starts_no_catastro_request_and_leaves_the_plot_unknown(
    app, monkeypatch
):
    """The step asks Catastro for nothing: an unreachable or expired cache is a
    miss, the outline is not fetched, and the plot stays unknown."""
    p = _row()
    _placed(p, epsg=25829)
    monkeypatch.setattr(c, "_cache_get", lambda _key: None)
    monkeypatch.setattr(c, "_cache_set", lambda *_a: None)
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)
    transport = Mock(side_effect=AssertionError("a request was started"))
    monkeypatch.setattr(c.requests, "get", transport)
    assert pc.enrich(p) is None
    assert transport.call_count == 0
    db.session.refresh(p)
    assert p.plot_area is None
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_the_block_is_plain_json(app, area):
    p = _row()
    _placed(p)
    json.dumps(pc.enrich(p))
