"""The taste prompt carries the parcel the cadastre measured (#498 follow-up).

`services/taste_service.gather_facts` asked for `cadastre["metrics"]["bbox_fill"]`
while `services/cadastre_service` writes `cadastre["geometry"]["bbox_fill_ratio"]`.
Both names missed, so `CADASTRAL PARCEL` and `PARCEL SHAPE` had never reached a
prompt — measured on production 2026-09-04, 6 rows carry a parcel, 6 under
`geometry` and 0 under `metrics`. The cost landed on the profile's own
reference: property 969, a parcel measured at 1616 m2, scored 58 with the reason
"нет данных о форме участка", while profile v3 weights plot shape 1.0 (its
heaviest like) and refuses an L-shaped parcel outright (its first dealbreaker).

What this file pins, and why each half is here rather than one assertion:

* the reader and the writer agree on the key names — asserted by running the
  REAL `shape_metrics` output into the REAL `gather_facts`, so a rename on
  either side goes red, which a fixture dict hand-written to today's spelling
  cannot do (it is the spelling that was wrong);
* the shape fed is `polsby_popper` and NOT `bbox_fill_ratio` — the bounding box
  is axis-aligned, so 969's clean *rotated* 26.6 x 63.9 m rectangle fills 0.45
  of its box and would have read as irregular. The trap is built here from
  coordinates rather than asserted from a constant;
* the compactness really separates the owner's own verdicts (774 at 0.30
  against 0.65-0.81 for every parcel they accepted);
* the cadastre's class and use ride along, as the cadastre's words;
* a row with no parcel says nothing, which is the module's shipped treatment;
* and a parcel arriving moves the facts fingerprint, which is what puts the row
  back in the scoring scope instead of leaving a stale number on screen.
"""

import itertools
import math

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import taste_service
from services.cadastre_service import shape_metrics
from tests import setup_test_environment

_SEQ = itertools.count(1)

# Modelled on property 969's parcel: the owner's own reading of it is a regular
# 26.6 x 63.9 m rectangle, and the cadastre records 14 vertices, 1616.2 m2,
# polsby_popper 0.657 and bbox_fill_ratio 0.447. A clean rectangle turned off
# the grid axes reproduces exactly that disagreement between the two ratios,
# from coordinates, which is the point of building it rather than pasting it.
NINE_SIX_NINE_SIDES = (26.6, 63.9)
NINE_SIX_NINE_ROTATION_DEG = 40.0


def _rotated_rectangle(width, height, degrees):
    """A closed ring for a rectangle turned off the axes, in metres."""
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    corners = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    ring = [(x * cos - y * sin, x * sin + y * cos) for x, y in corners]
    return ring + [ring[0]]


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
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _mk_property(profile_row, **overrides):
    values = dict(
        source_email_id=f"parcel-facts:{next(_SEQ)}",
        title="Casa en Malpica",
        price=290000,
        area=300,
        municipality="Malpica de Bergantiños",
        search_profile_id=profile_row.id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _line(facts, prefix):
    matches = [line for line in facts if line.startswith(prefix)]
    return matches[0] if matches else None


def test_the_reader_and_the_writer_agree_on_the_parcel_keys(app, profile_row):
    """The real `shape_metrics` output, stored the way the service stores it,
    reaches the prompt. A rename on either side reddens this."""
    metrics = shape_metrics([_rotated_rectangle(*NINE_SIX_NINE_SIDES, 40.0)])
    prop = _mk_property(
        profile_row,
        enrichment={"cadastre": {"geometry": metrics, "run_state": "ok"}},
    )

    facts = taste_service.gather_facts(prop)

    assert _line(facts, "CADASTRAL PARCEL:") == "CADASTRAL PARCEL: 1,700 m2"
    shape = _line(facts, "PARCEL SHAPE:")
    assert shape is not None, "the measured parcel never reached the prompt"
    assert "compactness 0.65" in shape


def test_the_shape_is_compactness_and_never_the_bounding_box_fill(app, profile_row):
    """969's parcel is a clean rectangle that fills under half its axis-aligned
    box. Feeding that ratio would teach the model the opposite of the truth."""
    metrics = shape_metrics(
        [_rotated_rectangle(*NINE_SIX_NINE_SIDES, NINE_SIX_NINE_ROTATION_DEG)]
    )
    # The trap, established from the coordinates rather than assumed: a clean
    # rectangle, and a bounding-box fill that reads as anything but.
    assert metrics["polsby_popper"] == pytest.approx(0.652, abs=0.005)
    assert metrics["bbox_fill_ratio"] < 0.5

    prop = _mk_property(
        profile_row, enrichment={"cadastre": {"geometry": metrics, "run_state": "ok"}}
    )
    facts = taste_service.gather_facts(prop)
    shape = _line(facts, "PARCEL SHAPE:")

    assert "bounding box" not in shape
    assert f"{metrics['bbox_fill_ratio']:.2f}" not in shape


def test_compactness_separates_the_owners_own_verdicts(app, profile_row):
    """774 was rejected as "L-shaped with a neck"; every parcel the owner kept
    measures 0.65-0.81. The number the prompt carries has to reproduce that."""
    l_shape = [
        (0.0, 0.0),
        (120.0, 0.0),
        (120.0, 30.0),
        (35.0, 30.0),
        (35.0, 146.0),
        (0.0, 146.0),
        (0.0, 0.0),
    ]
    rejected = shape_metrics([l_shape])
    accepted = shape_metrics(
        [_rotated_rectangle(*NINE_SIX_NINE_SIDES, NINE_SIX_NINE_ROTATION_DEG)]
    )

    assert rejected["polsby_popper"] < 0.45
    assert accepted["polsby_popper"] > 0.6

    def _shape_line(metrics):
        prop = _mk_property(
            profile_row,
            enrichment={"cadastre": {"geometry": metrics, "run_state": "ok"}},
        )
        return _line(taste_service.gather_facts(prop), "PARCEL SHAPE:")

    assert _shape_line(rejected) != _shape_line(accepted)


def test_the_cadastres_class_and_use_ride_along(app, profile_row):
    prop = _mk_property(
        profile_row,
        enrichment={
            "cadastre": {
                "geometry": {"area_m2": 1616.2, "polsby_popper": 0.657},
                "attributes": {"class": "UR", "use": "Residencial"},
            }
        },
    )

    facts = taste_service.gather_facts(prop)

    assert _line(facts, "CADASTRAL CLASS:").startswith("CADASTRAL CLASS: UR")
    assert _line(facts, "CADASTRAL USE:") == "CADASTRAL USE: Residencial"


def test_the_old_metrics_spelling_carries_nothing(app, profile_row):
    """The shape the reader used to ask for. Nothing on production has ever
    carried it, and reinstating it must not look like a working read."""
    prop = _mk_property(
        profile_row,
        enrichment={
            "cadastre": {"metrics": {"area_m2": 1616.2, "bbox_fill": 0.447}},
        },
    )

    facts = taste_service.gather_facts(prop)

    assert _line(facts, "CADASTRAL PARCEL:") is None
    assert _line(facts, "PARCEL SHAPE:") is None


def test_a_row_with_no_parcel_says_nothing_about_one(app, profile_row):
    prop = _mk_property(profile_row, enrichment={"sea": {"status": "ok"}})

    facts = taste_service.gather_facts(prop)

    assert _line(facts, "CADASTRAL PARCEL:") is None
    assert _line(facts, "PARCEL SHAPE:") is None
    assert _line(facts, "CADASTRAL CLASS:") is None


def test_a_parcel_arriving_moves_the_fingerprint(app, profile_row):
    """Which is what returns the row to the scoring scope: `read_taste` calls a
    score whose fingerprint no longer matches the row stale, and the backfill's
    scope IS the reader."""
    prop = _mk_property(profile_row, enrichment={})
    before = taste_service.facts_fingerprint(taste_service.gather_facts(prop))

    prop.enrichment = {
        "cadastre": {
            "geometry": shape_metrics(
                [_rotated_rectangle(*NINE_SIX_NINE_SIDES, NINE_SIX_NINE_ROTATION_DEG)]
            )
        }
    }
    db.session.commit()
    after = taste_service.facts_fingerprint(taste_service.gather_facts(prop))

    assert before != after
