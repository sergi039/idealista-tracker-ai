"""The preview must describe the rows, not the argument it forgot.

`--dry-run` is the arm an operator reads *before* authorising a run that
rewrites `enrichment`, every score column and `scoring` for every located row.
It shipped in #358 calling `SeaDistanceService.measure(lat, lon)` with no third
argument, so `accuracy` fell back to its default of `None` -> "unknown" ->
approximate, and the preview reported `approximate_origin` for all 730 rows,
including the 193 Google matched to an address.

Nothing in the suite touched this arm, which is how a two-line call site went
out wrong in a PR whose own regression file has 21 tests: the argument was
added to the callee and to one of its two call sites, and the tests exercised
the callee.

So this pins the arm itself — the same status the real run would store, per
row, from the same fixture coastline.
"""

from decimal import Decimal

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services import sea_distance_service as sea_module  # noqa: E402
from services.coordinate_quality import (  # noqa: E402
    TIER_LISTING_PIN,
    TIER_LOCALITY,
    record_portal_coordinate,
)
from services.sea_distance_service import (  # noqa: E402
    STATUS_APPROXIMATE_ORIGIN,
    STATUS_OK,
)
from utils import recalc_sea_distance as tool  # noqa: E402

# A coastline node ~24 m north of the fixture listings, so a measurement that
# happens is unmistakably a measurement: the shoreline itself.
ROW_LAT = 43.5723710
ROW_LON = -5.9963786
COAST_NODE = (ROW_LAT + 0.000214, ROW_LON)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


class _CurrentApp:
    """Hands main() the app context the fixture already established."""

    def app_context(self):
        from contextlib import nullcontext

        return nullcontext()


def _listing(key, accuracy):
    prop = Property(
        source_email_id=f"dry-{key}",
        title=f"Dry run {key}",
        municipality="Castrillón",
        location_lat=Decimal(str(ROW_LAT)),
        location_lon=Decimal(str(ROW_LON)),
        location_accuracy=accuracy,
        score_total=50,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _run_dry(monkeypatch, capsys=None):
    """Drive main() with --dry-run over the fixture coastline."""
    monkeypatch.setattr(
        sea_module, "fetch_coastline_points", lambda lat, lon, **kw: [COAST_NODE]
    )
    monkeypatch.setattr(tool, "create_app", lambda: _CurrentApp())
    monkeypatch.setattr("sys.argv", ["recalc", "--dry-run", "--sleep", "0"])
    tool.main()


class TestThePreviewReadsTheRow:
    def test_a_precise_row_previews_as_measured(self, app, monkeypatch, caplog):
        """The defect, stated as the value it got wrong.

        Without the accuracy argument this row previews as
        `approximate_origin`: the operator is told the whole table is
        unmeasurable, and the one number that would contradict it — 193
        precise rows — never appears.
        """
        _listing("precise", "precise")

        with caplog.at_level("INFO"):
            _run_dry(monkeypatch)

        assert f"'{STATUS_OK}': 1" in caplog.text
        assert STATUS_APPROXIMATE_ORIGIN not in caplog.text

    def test_an_approximate_row_previews_as_the_centroid_it_is(
        self, app, monkeypatch, caplog
    ):
        """The control: the same coastline, the same point, the other label."""
        _listing("approx", "approximate")

        with caplog.at_level("INFO"):
            _run_dry(monkeypatch)

        assert f"'{STATUS_APPROXIMATE_ORIGIN}': 1" in caplog.text

    def test_the_two_are_counted_apart_in_one_run(self, app, monkeypatch, caplog):
        """Both in one summary, because that is the shape the operator reads."""
        _listing("precise", "precise")
        _listing("approx", "approximate")
        _listing("unknown", None)

        with caplog.at_level("INFO"):
            _run_dry(monkeypatch)

        # An unlabelled row is a centroid until proven otherwise, so 2 of the 3.
        assert f"'{STATUS_OK}': 1" in caplog.text
        assert f"'{STATUS_APPROXIMATE_ORIGIN}': 2" in caplog.text

    def test_a_dry_run_still_writes_nothing(self, app, monkeypatch):
        """The other half of the promise, and it has never been pinned either."""
        prop = _listing("precise", "precise")

        _run_dry(monkeypatch)

        db.session.expire_all()
        reloaded = db.session.get(Property, prop.id)
        assert reloaded.enrichment is None
        assert float(reloaded.score_total) == 50.0


class TestThePreviewReadsTheRowsTier:
    """#493 put a second argument in front of the same trap.

    `measure` now also takes the coordinate *tier*, and the dry run is again
    the arm that would silently omit it: the summary it logs counts statuses,
    and a pin row and a centroid row both log `approximate_origin`, so no
    assertion over that text can tell the two apart. What distinguishes them
    is the band -- 2 km against 5 km -- so this records the argument the call
    actually received, rather than that a call happened (#297).
    """

    def _spy(self, monkeypatch):
        seen = []
        real = sea_module.SeaDistanceService.measure

        def measure(inner_self, lat, lon, accuracy, **kw):
            seen.append(kw.get("tier"))
            return real(inner_self, lat, lon, accuracy, **kw)

        monkeypatch.setattr(sea_module.SeaDistanceService, "measure", measure)
        return seen

    def test_a_pin_row_previews_under_its_own_tier(self, app, monkeypatch):
        prop = _listing("pin", "approximate")
        prop.enrichment = record_portal_coordinate(
            None, source="fotocasa", lat=ROW_LAT, lon=ROW_LON
        )
        db.session.commit()

        seen = self._spy(monkeypatch)
        _run_dry(monkeypatch)

        assert seen == [TIER_LISTING_PIN]

    def test_a_centroid_row_is_the_control(self, app, monkeypatch):
        """Same coordinate, same coastline, no pin block -- and the preview
        must still call it a centroid, or the test above would pass for a call
        site that hard-coded the narrow tier."""
        _listing("centroid", "approximate")

        seen = self._spy(monkeypatch)
        _run_dry(monkeypatch)

        assert seen == [TIER_LOCALITY]


class TestTheArgumentCannotBeForgottenAgain:
    def test_measure_requires_an_accuracy(self, app):
        """The fix is the signature, not the one call site that got it wrong.

        A default made "nobody passed one" indistinguishable from "this row is
        a centroid". There are two callers and both hold the row; a required
        argument turns the next omission into a TypeError at the call, instead
        of a plausible wrong answer in a report.
        """
        with pytest.raises(TypeError):
            sea_module.SeaDistanceService().measure(ROW_LAT, ROW_LON)

    def test_measure_requires_a_tier(self, app):
        """The identical argument, one ticket later (#493).

        A default here is not merely untidy: it was written first, and the two
        tests that measure a precise row's distance came straight back as
        `approximate_origin` with a 5 km band, because the safest-looking
        default is a locality centroid and a precise row is not one. Both
        callers hold the row and can answer.
        """
        with pytest.raises(TypeError):
            sea_module.SeaDistanceService().measure(ROW_LAT, ROW_LON, "precise")
