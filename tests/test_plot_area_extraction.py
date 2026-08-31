"""The parcel's surface, out of the fotocasa payload and into the column.

The parser always read `surfaceLand` and, for a HOUSE, dropped it on the
floor — `area` is the built surface and the plot went nowhere. Now it rides
`listing.plot_area` into `properties.plot_area`, with fotocasa's 0-as-blank
convention intact: zero is never a tiny plot (#98). The backfill re-reads
only fotocasa pages (the one portal that answers this machine AND states
plots) and writes nothing for a page that states none.
"""

import json
import re
from pathlib import Path

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import fotocasa_source
from tests import setup_test_environment

FIXTURE = Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"


def _payload():
    html = FIXTURE.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'id="__initial_props__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(match.group(1)), html


def _html_with(payload):
    html = FIXTURE.read_text(encoding="utf-8", errors="ignore")
    return re.sub(
        r'(id="__initial_props__"[^>]*>).*?(</script>)',
        lambda m: m.group(1) + json.dumps(payload) + m.group(2),
        html,
        flags=re.S,
    )


class TestTheParserKeepsThePlot:
    def test_the_real_land_fixture_states_its_plot(self):
        _, html = _payload()
        listing = fotocasa_source.parse_listing(html, "https://www.fotocasa.es/x")
        assert listing.plot_area == 1945
        assert listing.area == 1945
        assert listing.area_type == "plot"

    def test_a_house_keeps_built_in_area_and_the_plot_beside_it(self):
        payload, _ = _payload()
        payload["realEstate"]["buildingSubtype"] = "Chalet"
        payload["realEstate"]["buildingType"] = "Chalet"
        payload["realEstate"]["features"]["surface"] = 210
        payload["realEstate"]["features"]["surfaceLand"] = 850
        listing = fotocasa_source.parse_listing(
            _html_with(payload), "https://www.fotocasa.es/x"
        )
        assert listing.area == 210
        assert listing.area_type == "built"
        assert listing.plot_area == 850

    def test_zero_is_a_blank_never_a_tiny_plot(self):
        payload, _ = _payload()
        payload["realEstate"]["buildingSubtype"] = "Chalet"
        payload["realEstate"]["buildingType"] = "Chalet"
        payload["realEstate"]["features"]["surface"] = 210
        payload["realEstate"]["features"]["surfaceLand"] = 0
        payload["realEstateAdDetailEntityV2"]["groundSurface"] = 0
        listing = fotocasa_source.parse_listing(
            _html_with(payload), "https://www.fotocasa.es/x"
        )
        assert listing.plot_area is None


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


class TestTheBackfill:
    def _row(self, profile_id, url, plot=None, seq=[0]):
        seq[0] += 1
        prop = Property(
            source_email_id=f"plot:{seq[0]}",
            title=f"Row {seq[0]}",
            price=1,
            area=200,
            url=url,
            plot_area=plot,
            search_profile_id=profile_id,
        )
        db.session.add(prop)
        db.session.commit()
        return prop

    def test_scope_is_fotocasa_rows_without_a_plot(self, app):
        from utils import backfill_plot_area

        profile = SearchProfile(name="G", is_active=True)
        db.session.add(profile)
        db.session.commit()
        wanted = self._row(
            profile.id, "https://www.fotocasa.es/es/comprar/vivienda/x/1/d"
        )
        self._row(
            profile.id,
            "https://www.fotocasa.es/es/comprar/vivienda/y/2/d",
            plot=900,
        )
        self._row(profile.id, "https://www.idealista.com/inmueble/123/")

        class Args:
            ids = []
            skip_ids = []
            limit = 0

        rows = backfill_plot_area._scope(Args())
        assert [p.id for p in rows] == [wanted.id]

    def test_a_page_stating_no_plot_leaves_the_scope_but_stays_unknown(
        self, app, monkeypatch, capsys
    ):
        """The gate review's finding: the tool re-fetched every known
        no-plot page forever, because the only thing marking a row done was
        a `plot_area` the page had just said does not exist. The READING is
        recorded now — and it is not a zero: `plot_area` stays NULL, so the
        criteria verdict still reads unknown (#98)."""
        import contextlib

        from utils import backfill_plot_area

        profile = SearchProfile(name="G", is_active=True)
        db.session.add(profile)
        db.session.commit()
        row = self._row(profile.id, "https://www.fotocasa.es/es/comprar/vivienda/n/7/d")

        monkeypatch.setattr(
            backfill_plot_area.fotocasa_source,
            "fetch_listing",
            lambda url: fotocasa_source.FotocasaListing(url=url, plot_area=None),
        )
        monkeypatch.setattr(
            backfill_plot_area, "inflight", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(backfill_plot_area.time, "sleep", lambda s: None)
        monkeypatch.setattr(backfill_plot_area, "create_app", lambda: app)
        monkeypatch.setattr("sys.argv", ["backfill_plot_area", "--apply"])
        backfill_plot_area.main()

        assert row.plot_area is None, "a stated absence is never a zero"
        assert (row.enrichment or {})["plot_lookup"]["status"] == "page_states_no_plot"

        class Args:
            ids = []
            skip_ids = []
            limit = 0

        assert backfill_plot_area._scope(Args()) == [], (
            "a row whose page answered must leave the scope"
        )

        class NamedArgs(Args):
            ids = [row.id]

        assert [p.id for p in backfill_plot_area._scope(NamedArgs())] == [row.id], (
            "--ids must override the marker — an operator naming a row means ask again"
        )

    def test_a_page_stating_no_plot_writes_nothing_and_a_refusal_stops(
        self, app, monkeypatch, capsys
    ):
        import contextlib

        from utils import backfill_plot_area

        profile = SearchProfile(name="G", is_active=True)
        db.session.add(profile)
        db.session.commit()
        rows = [
            self._row(
                profile.id,
                f"https://www.fotocasa.es/es/comprar/vivienda/r{i}/{i}/d",
            )
            for i in range(1, 6)
        ]

        answers = iter(
            [
                fotocasa_source.FotocasaListing(url="u", plot_area=777.0),
                fotocasa_source.FotocasaListing(url="u", plot_area=None),
                fotocasa_source.FotocasaListing(url="u", refusal="blocked"),
                fotocasa_source.FotocasaListing(url="u", refusal="blocked"),
                fotocasa_source.FotocasaListing(url="u", refusal="blocked"),
            ]
        )
        monkeypatch.setattr(
            backfill_plot_area.fotocasa_source,
            "fetch_listing",
            lambda url: next(answers),
        )
        monkeypatch.setattr(
            backfill_plot_area, "inflight", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(backfill_plot_area.time, "sleep", lambda s: None)
        monkeypatch.setattr(backfill_plot_area, "create_app", lambda: app)
        monkeypatch.setattr("sys.argv", ["backfill_plot_area", "--apply"])
        backfill_plot_area.main()
        out = capsys.readouterr().out
        assert "Stopping: 3 refusals in a row" in out
        assert "1 filled" in out
        assert float(rows[0].plot_area) == 777.0
        assert rows[1].plot_area is None


class TestAWithdrawnAdvertIsNotAHostRefusal:
    """From the #502 review: three dead adverts stalled the run forever.

    fotocasa redirects a withdrawn listing to a search page, and
    `fetch_listing` returns `REFUSAL_NOT_A_LISTING` for it
    (services/fotocasa_source.py). Counting that as a host refusal tripped the
    three-in-a-row stop, nothing was written, and the scope is ordered by id —
    so the next run met the same three and stopped again. No forward progress
    at any number of re-runs, while the module's docstring called itself
    resumable.

    The sibling already drew this line and said why:
    `utils/backfill_advertiser._NOT_A_HOST_REFUSAL`, "a run would stop on its
    third row having asked nobody anything".
    """

    @staticmethod
    def _row(profile_id, url):
        prop = Property(
            title="t",
            source_email_id=f"x:{url}",
            url=url,
            search_profile_id=profile_id,
        )
        db.session.add(prop)
        db.session.commit()
        return prop

    def test_three_dead_adverts_do_not_stop_a_run_with_work_left(
        self, app, monkeypatch, capsys
    ):
        import contextlib

        from utils import backfill_plot_area

        profile = SearchProfile(name="G", is_active=True)
        db.session.add(profile)
        db.session.commit()
        base = "https://www.fotocasa.es/es/comprar/vivienda/n"
        dead = [self._row(profile.id, f"{base}/{n}/d") for n in (1, 2, 3)]
        alive = self._row(profile.id, f"{base}/4/d")

        def _fetch(url):
            if url == alive.url:
                return fotocasa_source.FotocasaListing(url=url, plot_area=1200)
            return fotocasa_source.FotocasaListing(
                url=url, refusal=fotocasa_source.REFUSAL_NOT_A_LISTING
            )

        monkeypatch.setattr(backfill_plot_area.fotocasa_source, "fetch_listing", _fetch)
        monkeypatch.setattr(
            backfill_plot_area, "inflight", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(backfill_plot_area.time, "sleep", lambda s: None)
        monkeypatch.setattr(backfill_plot_area, "create_app", lambda: app)
        monkeypatch.setattr("sys.argv", ["backfill_plot_area", "--apply"])
        backfill_plot_area.main()

        # Re-read: `main()` commits through its own session, so the objects
        # this test holds are stale. Asserting on a stale None would pass for
        # the wrong reason in exactly the direction being tested.
        db.session.expire_all()
        alive = db.session.get(Property, alive.id)
        dead = [db.session.get(Property, row.id) for row in dead]

        assert float(alive.plot_area) == 1200.0, (
            "the run stopped on three withdrawn adverts and never reached the "
            "row that had an answer"
        )
        assert "Stopping" not in capsys.readouterr().out
        for row in dead:
            assert row.plot_area is None, "a gone advert is not a measurement"

    def test_a_real_host_refusal_still_stops_the_run(self, app, monkeypatch, capsys):
        """The negative control. Without it the change above could be read as
        "never stop", which would walk a blocked run through the whole scope —
        the thing the three-in-a-row rule exists to prevent."""
        import contextlib

        from utils import backfill_plot_area

        profile = SearchProfile(name="G", is_active=True)
        db.session.add(profile)
        db.session.commit()
        base = "https://www.fotocasa.es/es/comprar/vivienda/b"
        for n in (1, 2, 3):
            self._row(profile.id, f"{base}/{n}/d")
        alive = self._row(profile.id, f"{base}/4/d")

        monkeypatch.setattr(
            backfill_plot_area.fotocasa_source,
            "fetch_listing",
            lambda url: fotocasa_source.FotocasaListing(
                url=url, refusal=fotocasa_source.REFUSAL_BLOCKED
            ),
        )
        monkeypatch.setattr(
            backfill_plot_area, "inflight", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(backfill_plot_area.time, "sleep", lambda s: None)
        monkeypatch.setattr(backfill_plot_area, "create_app", lambda: app)
        monkeypatch.setattr("sys.argv", ["backfill_plot_area", "--apply"])
        backfill_plot_area.main()

        db.session.expire_all()
        alive = db.session.get(Property, alive.id)

        assert "Stopping" in capsys.readouterr().out
        assert alive.plot_area is None
