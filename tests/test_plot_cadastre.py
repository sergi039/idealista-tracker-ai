"""#634: the plot of a house that states none, from the parcel an Enrich press placed.

Nothing here touches the network. Most tests stub the parcel's area to isolate
one rule; `TestTheRealPath` stubs nothing but the transport and the cache: the
committed Catastro answers for Bayas go through the real neighbour step, the
real cache and the real plot step to the stored `plot_area`.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
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
    """What the neighbour step answered in this press: stored, and returned."""
    result = {
        "status": "ok",
        "subject": n.subject(p),
        "reference": REF,
        **block,
    }
    enrichment = dict(p.enrichment or {})
    enrichment["neighbours"] = result
    p.enrichment = enrichment
    db.session.commit()
    return result


@pytest.fixture
def area(monkeypatch):
    stub = Mock(return_value=1234.5)
    monkeypatch.setattr(pc, "parcel_area_m2", stub)
    return stub


def test_the_parcel_area_becomes_the_plot_with_its_source(app, area):
    p = _row(enrichment={"pool": {"status": "ok"}})
    placed = _placed(p)
    block = pc.enrich(p, placed)
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
    placed = _placed(p)
    monkeypatch.setattr(
        db.session, "commit", Mock(side_effect=RuntimeError("commit refused"))
    )
    with pytest.raises(RuntimeError):
        pc.enrich(p, placed)
    monkeypatch.undo()
    db.session.refresh(p)
    assert p.plot_area is None
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_no_commit_ever_carries_one_of_the_two_writes_without_the_other(
    app, area, monkeypatch
):
    p = _row()
    placed = _placed(p)
    seen = []
    real_commit = db.session.commit

    def spy():
        seen.append((p.plot_area is not None, pc.ENRICHMENT_KEY in p.enrichment))
        return real_commit()

    monkeypatch.setattr(db.session, "commit", spy)
    assert pc.enrich(p, placed) is not None
    assert seen and all(plot == source for plot, source in seen)


def test_the_written_plot_decides_the_subscription_criteria(app, area):
    p = _row()
    placed = _placed(p)
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is None
    pc.enrich(p, placed)
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is True


@pytest.mark.parametrize("stated", [900, 100])
def test_a_stated_plot_is_never_overwritten_and_nothing_is_asked(app, area, stated):
    p = _row(plot_area=stated)
    placed = _placed(p)
    assert pc.enrich(p, placed) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert float(p.plot_area) == stated
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_a_plot_stated_while_catastro_answers_wins_under_the_lock(app, monkeypatch):
    p = _row()
    placed = _placed(p)

    def slow(reference):
        db.session.execute(
            text("UPDATE properties SET plot_area=950 WHERE id=:i"), {"i": p.id}
        )
        return 1234.5

    monkeypatch.setattr(pc, "parcel_area_m2", slow)
    assert pc.enrich(p, placed) is None
    db.session.refresh(p)
    assert float(p.plot_area) == 950
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_a_row_that_moved_while_catastro_answered_is_not_written(app, monkeypatch):
    p = _row()
    placed = _placed(p)

    def moved(reference):
        db.session.execute(
            text("UPDATE properties SET location_lat=43.6 WHERE id=:i"), {"i": p.id}
        )
        return 1234.5

    monkeypatch.setattr(pc, "parcel_area_m2", moved)
    assert pc.enrich(p, placed) is None
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
    placed = _placed(p)
    assert pc.enrich(p, placed) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert p.plot_area is None


def _kept(p):
    return {**_placed(p), n.KEPT_PREVIOUS: True}


@pytest.mark.parametrize(
    "answer",
    [
        lambda p: None,
        lambda p: {"status": "no_parcel", "subject": n.subject(p)},
        lambda p: {"status": "unavailable", "reason": "parcel_lookup_failed"},
        lambda p: {
            "status": "ok",
            "subject": "not-this-row's-subject",
            "reference": REF,
        },
        lambda p: {"status": "ok", "reference": REF},
        lambda p: {"status": "ok", "subject": n.subject(p), "reference": "not one"},
        _kept,
        lambda p: "ok",
    ],
    ids=[
        "step_raised",
        "no_parcel",
        "unavailable",
        "stale_subject",
        "no_subject",
        "unreadable_reference",
        "kept_an_earlier_block",
        "not_an_answer",
    ],
)
def test_a_parcel_this_press_did_not_establish_leaves_the_plot_unknown(
    app, area, answer
):
    """Even with a matching `ok` block stored by an earlier press."""
    p = _row()
    _placed(p)
    assert pc.enrich(p, answer(p)) is None
    area.assert_not_called()
    db.session.refresh(p)
    assert p.plot_area is None
    assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is None


def test_a_catastro_refusal_writes_nothing_and_is_not_a_zero(app, monkeypatch):
    p = _row()
    placed = _placed(p)
    monkeypatch.setattr(
        pc, "parcel_area_m2", Mock(side_effect=c.CadastreError(c.REFUSED, "HTTP 503"))
    )
    assert pc.enrich(p, placed) is None
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


@pytest.mark.parametrize(
    "neighbour_step, expected",
    [
        (Mock(return_value={"status": "ok", "reference": REF}), "answer"),
        (Mock(side_effect=RuntimeError("neighbour lookup failed")), None),
    ],
    ids=["answered", "raised"],
)
def test_the_press_hands_the_plot_step_what_the_neighbour_step_answered_now(
    app, monkeypatch, neighbour_step, expected
):
    order = []
    service = PropertyEnrichmentService(neighbours_service=Mock())
    service.neighbours_service.enrich = neighbour_step
    monkeypatch.setattr(
        "services.property_enrichment_service.advertiser.enrich", lambda *_a, **_k: None
    )
    monkeypatch.setattr(service, "enrich_free_sources", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "services.property_enrichment_service.plot_cadastre.enrich",
        lambda prop, established, commit: order.append((established, commit)),
    )
    service._advisory_pass(Mock(), use_ai=False, with_pool=False)
    neighbour_step.assert_called_once()
    handed = neighbour_step.return_value if expected == "answer" else None
    assert order == [(handed, True)]


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

    def takes_the_row_then_fails(prop, established, commit):
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
    placed = _placed(p, epsg=25829)
    monkeypatch.setattr(c, "_cache_get", lambda _key: None)
    monkeypatch.setattr(c, "_cache_set", lambda *_a: None)
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)
    transport = Mock(side_effect=AssertionError("a request was started"))
    monkeypatch.setattr(c.requests, "get", transport)
    assert pc.enrich(p, placed) is None
    assert transport.call_count == 0
    db.session.refresh(p)
    assert p.plot_area is None
    assert pc.ENRICHMENT_KEY not in p.enrichment


def test_the_block_is_plain_json(app, area):
    p = _row()
    placed = _placed(p)
    json.dumps(pc.enrich(p, placed))


# -- the real path: Catastro's committed answers, the real cache, no stubs ------

BAYAS = "33016A003001530001HQ"
BAYAS_DATA = Path(__file__).parent / "data"
EMPTY_BUILDINGS = (
    '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" '
    'numberMatched="0" numberReturned="0"/>'
)


def _gml(epsg):
    name = f"catastro_parcel_33016A00300153_{epsg}.gml"
    return (BAYAS_DATA / name).read_text(encoding="latin-1")


class _Catastro:
    """The committed Catastro answers behind the real transport and cache keys."""

    def __init__(self, monkeypatch):
        self.cache = {}
        self.calls = []
        self.down = False
        monkeypatch.setattr(c, "_cache_get", self.cache.get)
        monkeypatch.setattr(c, "_cache_set", self.cache.__setitem__)
        monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
        monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)
        monkeypatch.setattr(c.requests, "get", self.get)

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.down:
            raise requests.ConnectionError("Catastro unreachable")
        if "wfsCP" in url:
            epsg = kwargs["params"]["srsname"].split("::")[1]
            return SimpleNamespace(status_code=200, text=_gml(epsg))
        if "wfsBU" in url:
            return SimpleNamespace(status_code=200, text=EMPTY_BUILDINGS)
        raise AssertionError(f"an unexpected Catastro request: {url}")

    def forget_the_building_walk(self):
        for key in [k for k in self.cache if "neighbours_buildings" in k]:
            del self.cache[key]


def _bayas_area():
    """The parcel's area read straight from the committed file, not from the code."""
    shape = n._shape(c._parse_gml(_gml(25829), 25829)["polygons"])
    return round(shape.area, 1)


class TestTheRealPath:
    @pytest.fixture
    def press(self, app, monkeypatch):
        catastro = _Catastro(monkeypatch)
        service = PropertyEnrichmentService()
        monkeypatch.setattr(
            "services.property_enrichment_service.advertiser.enrich",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(service, "enrich_free_sources", lambda *_a, **_k: None)
        p = _row(cadastral_reference=BAYAS)

        def run():
            service._advisory_pass(p, use_ai=False, with_pool=False)
            db.session.refresh(p)

        return SimpleNamespace(catastro=catastro, service=service, row=p, run=run)

    def test_the_committed_answers_end_as_the_stored_plot(self, press):
        press.run()
        p = press.row
        assert p.enrichment["neighbours"]["status"] == "ok"
        assert float(p.plot_area) == _bayas_area() > 0
        block = p.enrichment[pc.ENRICHMENT_KEY]
        assert block["value"] == _bayas_area()
        assert block["reference"] == c.parcel_reference(BAYAS)
        assert block["source"] == pc.SOURCE
        # The neighbour step's three requests are all there are: the plot step
        # read the outline it had cached.
        assert len(press.catastro.calls) == 3
        assert sc.read_verdict(p, CRITERIA)["checks"]["plot"] is not None

    def _forget_the_plot(self, press):
        p = press.row
        enrichment = {k: v for k, v in p.enrichment.items() if k != pc.ENRICHMENT_KEY}
        p.enrichment = enrichment
        p.plot_area = None
        db.session.commit()

    def test_a_lookup_refused_in_this_press_writes_no_plot_from_an_earlier_ok(
        self, press
    ):
        press.run()  # an earlier press: the parcel is established and cached
        self._forget_the_plot(press)
        press.catastro.forget_the_building_walk()
        press.catastro.down = True
        press.run()
        p = press.row
        assert p.enrichment["neighbours"]["status"] == "ok"  # kept, as before
        assert p.plot_area is None
        assert pc.ENRICHMENT_KEY not in p.enrichment

    def test_a_neighbour_step_that_raises_writes_no_plot_from_an_earlier_ok(
        self, press, monkeypatch
    ):
        press.run()
        self._forget_the_plot(press)
        monkeypatch.setattr(
            press.service.neighbours_service,
            "enrich",
            Mock(side_effect=RuntimeError("neighbour lookup failed")),
        )
        press.run()
        assert press.row.plot_area is None
        assert pc.ENRICHMENT_KEY not in press.row.enrichment

    def test_an_expired_cache_is_a_miss_that_starts_no_request(self, press):
        press.run()
        self._forget_the_plot(press)
        press.catastro.cache.clear()
        before = len(press.catastro.calls)
        # The neighbour step walks again (it may ask); the plot step must not.
        placed = press.row.enrichment["neighbours"]
        press.catastro.down = True
        assert pc.enrich(press.row, placed) is None
        assert len(press.catastro.calls) == before
        assert press.row.plot_area is None
