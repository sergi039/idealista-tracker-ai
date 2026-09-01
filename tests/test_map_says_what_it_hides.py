"""The map's default criteria hide is disclosed and liftable, not silent.

Closing-audit finding 4 (2026-09-01), found by two lenses independently:
`/map` applied the default criteria reading — correctly, since #445's rule is
that a filter one surface keeps and another drops disagrees about which
listings exist — and said NOTHING: 8 of 157 markers absent on production,
`templates/map.html` not containing the word "criteria" once, no disclosure
and no control. On a map an empty stretch of coast IS the answer being read,
so a silent hide is #98's shape: "nothing here" where the truth is "hidden
from here".

The disclosure mirrors the list's own line — the same one-home count
(`subscription_criteria.apply_filter(..., count_hidden=True)`) and the same
i18n line — with the lift in the only control the map has, a link to the
same URL under `criteria=all`. The mode is STATED in that link rather than
dropped, because `criteria` is the one filter whose absence still filters
(`utils/listing_filters.CLEARED_NOT_ABSENT`, the #508 loop).

The count is the map's own: rows without coordinates are not markers this
page could have drawn, which is why production said 8 where the list said
62 — pinned here with a coordinate-less hidden fail that must NOT be
counted. And the acceptance condition is the audit's: follow the rendered
link and count what comes back.
"""

from __future__ import annotations

import html
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}

NOTE_RE = re.compile(
    r'id="map-criteria-hidden-note"[^>]*>(.*?)</div>',
    re.S,
)


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


_SEQ = iter(range(1, 100_000))


def _mk(profile_id, **overrides):
    n = next(_SEQ)
    values = dict(
        source_email_id=f"map_hide:{n}",
        title=f"Listing {n}",
        municipality="Cedeira",
        listing_status="active",
        property_category="housing",
        search_profile_id=profile_id,
        price=120000,
        area=200.0,
        area_type="built",
        plot_area=1000.0,
        location_lat=43.5,
        location_lon=-8.0,
        url=f"https://www.idealista.com/inmueble/{8000 + n}/",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _fails(profile_id, **overrides):
    values = dict(area=100.0, plot_area=300.0)
    values.update(overrides)
    return _mk(profile_id, **values)


@pytest.fixture
def world(app):
    """Three mappable pass rows, two mappable unjudged fails, one hidden
    fail with NO coordinate (a marker this map could never have drawn), one
    favorited fail (exempt, so never hidden), one fail in another
    municipality (for the filters-survive-the-link case)."""
    galicia = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(galicia)
    db.session.commit()
    pid = galicia.id

    rows = {
        "pass_1": _mk(pid),
        "pass_2": _mk(pid),
        "pass_3": _mk(pid),
        "fail_mapped_1": _fails(pid),
        "fail_mapped_2": _fails(pid),
        "fail_no_coords": _fails(pid, location_lat=None, location_lon=None),
        "fail_favorited": _fails(pid, is_favorite=True),
        "fail_elsewhere": _fails(pid, municipality="Camariñas"),
    }
    return {"pid": pid, "ids": {name: row.id for name, row in rows.items()}}


def _body(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.get_data(as_text=True)


def _markers(body) -> set[int]:
    match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.S)
    assert match, "no marker payload on the map page"
    return {
        int(pid) for pid in re.findall(r'"id":\s*(\d+)', html.unescape(match.group(1)))
    }


def _note(body):
    """(count, reveal href) from the disclosure, or None."""
    match = NOTE_RE.search(body)
    if match is None:
        return None
    chunk = match.group(1)
    count = re.search(r"Criteria:\s*(\d+)\s*failing hidden", chunk)
    href = re.search(r'href="([^"]+)"', chunk)
    assert count and href, "the note carries its count and its lift"
    return int(count.group(1)), html.unescape(href.group(1))


class TestTheMapSaysWhatItHides:
    def test_the_default_map_hides_and_says_so(self, client, world):
        ids = world["ids"]
        body = _body(client, f"/map?profile_id={world['pid']}")
        markers = _markers(body)
        assert ids["fail_mapped_1"] not in markers
        assert ids["fail_favorited"] in markers, "a judged row is never hidden"
        note = _note(body)
        assert note is not None, "a hide with no disclosure reads as nothing here"
        count, _ = note
        assert count == 3, (
            "the mappable unjudged fails: fail_mapped_1/2 and fail_elsewhere; "
            "the coordinate-less one is not a marker this map could have drawn"
        )

    def test_the_lift_link_opens_exactly_the_missing_markers(self, client, world):
        """Follow the rendered link: default markers plus the hidden ones,
        nothing else — the audit's own acceptance condition."""
        default_body = _body(client, f"/map?profile_id={world['pid']}")
        count, href = _note(default_body)
        revealed = _markers(_body(client, href))
        assert revealed == _markers(default_body) | {
            world["ids"]["fail_mapped_1"],
            world["ids"]["fail_mapped_2"],
            world["ids"]["fail_elsewhere"],
        }
        assert len(revealed) - len(_markers(default_body)) == count, (
            "the number the note states is the number its own link adds"
        )

    def test_the_lift_keeps_the_other_filters(self, client, world):
        """criteria=all is stated; municipality survives the link, so the
        lift widens ONE axis and not the whole map."""
        body = _body(client, f"/map?profile_id={world['pid']}&municipality=Cedeira")
        count, href = _note(body)
        assert count == 2, "fail_elsewhere is filtered out by municipality"
        assert "criteria=all" in href and "municipality=Cedeira" in href
        revealed = _markers(_body(client, href))
        assert world["ids"]["fail_elsewhere"] not in revealed
        assert world["ids"]["fail_mapped_1"] in revealed

    def test_an_explicit_mode_draws_no_note(self, client, world):
        """`criteria=all` and `criteria=fail` are the caller's own choice —
        nothing was hidden by a rule nobody asked for."""
        for mode in ("all", "fail"):
            body = _body(client, f"/map?profile_id={world['pid']}&criteria={mode}")
            assert _note(body) is None, mode
        fails_only = _markers(
            _body(client, f"/map?profile_id={world['pid']}&criteria=fail")
        )
        assert world["ids"]["pass_1"] not in fails_only

    def test_a_map_with_nothing_hidden_draws_no_note(self, client, world):
        """Absent at zero, present above: with `favorites=on` the only row is
        the favorited fail, which the default reading never hides."""
        body = _body(client, f"/map?profile_id={world['pid']}&favorites=on")
        assert _markers(body) == {world["ids"]["fail_favorited"]}
        assert _note(body) is None, "a note saying 0 hidden is noise"

    def test_a_world_without_criteria_draws_no_note(self, client, app):
        plain = SearchProfile(name="Asturias", is_active=True)
        db.session.add(plain)
        db.session.commit()
        _fails(plain.id)
        body = _body(client, f"/map?profile_id={plain.id}")
        assert _note(body) is None
        assert len(_markers(body)) == 1, "no criteria, nothing hidden"
