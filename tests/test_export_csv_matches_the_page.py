"""A bare `/properties/export.csv` exports what a bare `/properties` shows.

The owner's decision of 2026-08-09 is that a bare listing surface shows every
live subscription at once, rather than one picked for the reader. `/properties`
applies it; `/properties/export.csv` did not, while its own comment claimed it
did -- it resolved a bare export to a single auto-selected profile, and that
helper preferred the catch-all whenever the catch-all held anything at all.

Measured on production 2026-08-31: the page showed 386 listings and the bare
export handed over **2**, the catch-all's whole contents, with nothing on
either surface saying they disagreed. The page's own Export button carries
`profile_id=all`, so this only ever bit the bare URL -- the one a person types
or bookmarks.

These tests compare the two surfaces against each other rather than against a
number, because a fixture's count is a fact about the fixture and the contract
is that the two agree.
"""

import csv
import io
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        catch_all = SearchProfile(
            name="Default",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        busy = SearchProfile(
            name="Galicia · costa",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([catch_all, busy])
        db.session.commit()

        # The production shape: the catch-all holds a little, the real
        # subscription holds most of it. The old fallback preferred the
        # catch-all *because* it was non-empty, so a fixture with an empty
        # catch-all would pass under the defect.
        _add(catch_all, "catch", 2)
        _add(busy, "galicia", 7)

        with app.test_client() as test_client:
            yield test_client
        db.drop_all()


def _add(profile, tag, count):
    for index in range(count):
        db.session.add(
            Property(
                source_email_id=f"{tag}:{index}",
                url=f"https://www.idealista.com/inmueble/{abs(hash(tag)) % 1000}{index}/",
                title=f"Casa {tag} {index}",
                municipality="Vigo",
                price=200000 + index,
                area=200 + index,
                search_profile_id=profile.id,
            )
        )
    db.session.commit()


def _csv_rows(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    body = response.get_data(as_text=True)
    return list(csv.reader(io.StringIO(body)))[1:]


def _page_count(client, url):
    response = client.get(url)
    assert response.status_code == 200, "the template did not render"
    found = re.search(r"(\d+) properties found", response.get_data(as_text=True))
    assert found, "the page did not report a count"
    return int(found.group(1))


def test_the_bare_export_carries_every_live_subscription(client):
    """9, not the catch-all's 2."""
    assert len(_csv_rows(client, "/properties/export.csv")) == 9


def test_the_bare_export_agrees_with_the_bare_page(client):
    """The contract, stated as the two surfaces rather than as a number."""
    assert len(_csv_rows(client, "/properties/export.csv")) == _page_count(
        client, "/properties"
    )


def test_the_bare_export_agrees_with_an_explicit_all(client):
    """`all` was already right; the bare URL is what disagreed with it."""
    assert len(_csv_rows(client, "/properties/export.csv")) == len(
        _csv_rows(client, "/properties/export.csv?profile_id=all")
    )


def test_naming_one_subscription_still_narrows_the_export(client):
    """The fix widens the *fallback* and must not disable the parameter."""
    busy = SearchProfile.query.filter_by(name="Galicia · costa").first()
    rows = _csv_rows(client, f"/properties/export.csv?profile_id={busy.id}")
    assert len(rows) == 7


def test_a_hidden_subscription_stays_out_of_the_widened_export(client):
    """`all` means visible and active -- widening the fallback must not leak."""
    hidden = SearchProfile(
        name="Hidden",
        is_active=True,
        is_default=False,
        is_hidden=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(hidden)
    db.session.commit()
    _add(hidden, "hidden", 4)

    assert len(_csv_rows(client, "/properties/export.csv")) == 9
    assert len(_csv_rows(client, "/properties/export.csv")) == _page_count(
        client, "/properties"
    )
