"""Publications of the same home fold into one row on every surface (#633).

What this file pins, and why each is a separate fact:

* a confirmed or probable group is ONE row on the list (table and cards), the
  CSV, `/api/properties` and the map; a possible pair is not folded;
* a protected member (favourite, decision, outstanding action, timeline entry)
  is never folded, and the owner's word (`same` / `different`) outranks the rule;
* `duplicates=show` and `duplicate_of=<id>` are the two ways back, the second
  is not remembered by the filter memory;
* the fold sits BEFORE pagination, so the numbers on a page add up: rows across
  every page plus the folded count equal the unfolded total, and no page is short;
* the cost is a constant number of statements, whatever the selection's size,
  and a selection over the cap is not folded, and says so;
* a decision is written to the clicked row only (the group is not fanned out).
"""

import csv
import io
import json
import re

import pytest
from sqlalchemy import event
from werkzeug.datastructures import MultiDict

from app import create_app, db
from models import Property, PropertyActivity, SearchProfile
from services import duplicate_fold, property_duplicates as dup
from tests import setup_test_environment
from utils import listing_filter_memory

IDEALISTA = "https://www.idealista.com/inmueble/{n}/"
YAENCONTRE = "https://www.yaencontre.com/venta/casa/inmueble-777-{n}"
FOTOCASA = "https://www.fotocasa.es/es/comprar/vivienda/x/{n}/d"


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    application.config["RATELIMIT_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Galicia", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


_counter = {"n": 0}


def _row(profile, slug, **kwargs):
    _counter["n"] += 1
    values = dict(
        source_email_id=f"fold:{slug}",
        title=f"Casa {slug}",
        municipality="Malpica",
        search_profile_id=profile.id,
        price=100000 + _counter["n"] * 1000,
        area=100 + _counter["n"],
        location_lat=43.0 + _counter["n"] * 0.01,
        location_lon=-8.0,
        location_accuracy="precise",
    )
    values.update(kwargs)
    row = Property(**values)
    db.session.add(row)
    db.session.commit()
    return row


def _confirmed_pair(profile, number, **second):
    """An idealista advert and its yaencontre re-publication (same listing id).

    Each pair has its own price and point, so two pairs are never one home.
    """
    base = dict(
        price=200000 + (number % 100000),
        area=150,
        location_lat=43.0 + (number % 1000) / 1000.0,
        location_lon=-8.5 - (number % 997) / 1000.0,
    )
    first = _row(profile, f"i{number}", url=IDEALISTA.format(n=number), **base)
    other = dict(base, url=YAENCONTRE.format(n=number))
    other.update(second)
    return first, _row(profile, f"y{number}", **other)


def _probable_pair(profile, tag, **second):
    """Two adverts on one portal: the three fields and the same point."""
    base = dict(
        municipality="Cee",
        price=180000,
        area=95,
        location_lat=42.9,
        location_lon=-9.1,
        location_accuracy="precise",
    )
    first = _row(profile, f"pa{tag}", url=FOTOCASA.format(n=f"1{tag}"), **base)
    other = dict(base, url=FOTOCASA.format(n=f"2{tag}"))
    other.update(second)
    return first, _row(profile, f"pb{tag}", **other)


def _possible_pair(profile):
    """The three fields with nothing to corroborate them."""
    base = dict(
        municipality="Muros",
        price=130000,
        area=80,
        location_lat=None,
        location_lon=None,
        location_accuracy=None,
        title="Casa",
    )
    first = _row(profile, "poa", url=FOTOCASA.format(n=3001), **base)
    return first, _row(profile, "pob", url=FOTOCASA.format(n=3002), **base)


def _shown(body):
    seen = []
    for found in re.findall(r'id="listing-(\d+)"', body):
        if int(found) not in seen:
            seen.append(int(found))
    return seen


def _page(client, profile, **query):
    query = {"profile_id": profile.id, "mode": "combined", **query}
    return client.get("/properties", query_string=query).get_data(as_text=True)


def _found(body):
    return int(re.search(r"<strong>(\d+) properties found</strong>", body).group(1))


def _folded_line(body):
    match = re.search(r'id="duplicates-folded-coverage".*?</span>', body, re.S)
    return match.group(0) if match else None


def _csv(client, profile, **query):
    query = {"profile_id": profile.id, "hide_removed": "off", **query}
    response = client.get("/properties/export.csv", query_string=query)
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))


def _api(client, profile, **query):
    query = {"profile_id": profile.id, "limit": 500, **query}
    return client.get("/api/properties", query_string=query).get_json()


def _markers(client, profile, **query):
    body = client.get(
        "/map", query_string={"profile_id": profile.id, **query}
    ).get_data(as_text=True)
    return {
        m["id"] for m in json.loads(re.search(r"const markers = (\[.*?\]);", body)[1])
    }


class TestOneRowPerHome:
    def test_a_confirmed_pair_is_one_row_on_every_surface(self, client, profile):
        first, second = _confirmed_pair(profile, 11111111)
        lone = _row(profile, "lone", url=FOTOCASA.format(n=9001))

        for view in ("list", "cards"):
            body = _page(client, profile, view_type=view)
            assert set(_shown(body)) == {lone.id, first.id}
            assert second.id not in _shown(body)
        rows = _csv(client, profile)
        assert {int(r["ID"]) for r in rows} == {first.id, lone.id}
        payload = _api(client, profile)
        assert {p["id"] for p in payload["properties"]} == {first.id, lone.id}
        assert payload["scope"]["total"] == 2
        assert payload["scope"]["duplicates_folded"] == 1
        assert _markers(client, profile) == {first.id, lone.id}

    def test_a_probable_pair_folds(self, client, profile):
        first, second = _probable_pair(profile, 1)
        assert dup.explain(first, second)["tier"] == dup.PROBABLE
        assert _shown(_page(client, profile)) == [first.id]

    def test_a_possible_pair_does_not(self, client, profile):
        first, second = _possible_pair(profile)
        assert dup.explain(first, second)["tier"] == dup.POSSIBLE
        assert set(_shown(_page(client, profile))) == {first.id, second.id}

    def test_the_representative_names_the_group(self, client, profile):
        first, second = _confirmed_pair(profile, 22222222, price=285000)
        body = _page(client, profile)
        assert 'data-duplicate-group-size="2"' in body
        assert "2 publications" in body
        # The other portal is named, linked, and "show all" opens the group.
        assert "yaencontre.com/venta/casa/inmueble-777-22222222" in body
        assert f"duplicate_of={first.id}" in body
        # Prices differ: the range sits beside the price, in the page's format.
        assert f"{float(first.price):,.0f}&ndash;285,000€" in body
        # The representative's own price is what the page sorts by.
        assert f"<strong>{float(first.price):,.0f}€</strong>" in body

    def test_equal_prices_show_no_range(self, client, profile):
        _confirmed_pair(profile, 33333333)
        assert (
            "duplicate-price-range"
            not in _page(client, profile).split("</thead>", 1)[-1]
        )

    def test_unsafe_member_urls_render_no_href(self, client, profile):
        first, second = _probable_pair(profile, 2, url="javascript:alert(1)//inmueble")
        body = _page(client, profile)
        assert second.id not in _shown(body)
        assert 'href="javascript:' not in body
        assert "javascript:alert" not in body

    def test_withdrawn_members_are_counted_and_the_live_one_represents(
        self, client, profile
    ):
        first, second = _confirmed_pair(profile, 44444444)
        first.listing_status = "removed"
        db.session.commit()
        body = _page(client, profile, hide_removed="off")
        assert _shown(body) == [second.id]
        assert "2 publications &middot; 1 withdrawn" in body


class TestProtection:
    @pytest.mark.parametrize(
        "protection",
        [
            {"is_favorite": True},
            {"owner_verdict": "interested"},
            {"next_action": "Call the agent"},
        ],
        ids=["favourite", "decision", "action"],
    )
    def test_a_protected_member_is_never_folded(self, client, profile, protection):
        # Both rows carry the protection: a favourite or a decision wins the
        # representative's seat, so only a second one proves the other is not
        # folded away. An action does not, and is protected on its own.
        first, second = _confirmed_pair(profile, 55555555, **protection)
        for key, value in protection.items():
            setattr(first, key, value)
        db.session.commit()
        assert set(_shown(_page(client, profile, hide_removed="off"))) == {
            first.id,
            second.id,
        }

    def test_a_timeline_entry_protects(self, client, profile):
        first, second = _confirmed_pair(profile, 66666666)
        db.session.add(
            PropertyActivity(property_id=second.id, kind="note", body="phoned them")
        )
        db.session.commit()
        assert set(_shown(_page(client, profile))) == {first.id, second.id}

    def test_a_protected_row_that_is_not_the_representative_stays_beside_it(
        self, client, profile
    ):
        first, second = _confirmed_pair(profile, 77777777, next_action="Ask")
        assert set(_shown(_page(client, profile))) == {first.id, second.id}
        assert set(_shown(_page(client, profile, duplicates="show"))) == {
            first.id,
            second.id,
        }


class TestTheOwnersWord:
    def test_different_unfolds_a_confirmed_pair(self, client, profile):
        first, second = _confirmed_pair(profile, 88888888)
        dup.set_link(first.id, second.id, verdict="different")
        assert set(_shown(_page(client, profile))) == {first.id, second.id}

    def test_same_folds_a_possible_pair(self, client, profile):
        first, second = _possible_pair(profile)
        dup.set_link(first.id, second.id, verdict="same")
        assert _shown(_page(client, profile)) == [first.id]


class TestTheWayBack:
    def test_show_lifts_the_fold_and_the_line(self, client, profile):
        first, second = _confirmed_pair(profile, 12121212)
        folded = _page(client, profile)
        assert _folded_line(folded) and "1 publication of the same home" in folded
        shown = _page(client, profile, duplicates="show")
        assert set(_shown(shown)) == {first.id, second.id}
        assert _folded_line(shown) is None
        # The reveal is that link and nothing else, and it names the chip.
        assert "duplicates=show" in folded
        assert "filter-chip" in shown and "Same-home publications" in shown

    def test_duplicate_of_shows_exactly_one_group(self, client, profile):
        first, second = _confirmed_pair(profile, 13131313)
        a, b = _probable_pair(profile, 3)
        _row(profile, "lone", url=FOTOCASA.format(n=9002))
        for named in (first.id, second.id):
            body = _page(client, profile, duplicate_of=named)
            assert set(_shown(body)) == {first.id, second.id}
            assert _folded_line(body) is None
            assert "Same home as listing" in body

    def test_an_unknown_or_unusable_id_is_the_empty_state(self, client, profile):
        _confirmed_pair(profile, 14141414)
        for value in ("999999", "abc", "0", "-3", "1e3"):
            body = _page(client, profile, duplicate_of=value)
            assert _found(body) == 0, value

    def test_duplicate_of_is_not_remembered_and_show_is(self, client, profile):
        first, _ = _confirmed_pair(profile, 15151515)
        client.get(
            "/properties",
            query_string={
                "profile_id": profile.id,
                "mode": "combined",
                "view_type": "list",
                "duplicates": "show",
                "duplicate_of": first.id,
            },
        )
        recalled = client.get("/properties")
        assert recalled.status_code == 302
        assert "duplicates=show" in recalled.headers["Location"]
        assert "duplicate_of" not in recalled.headers["Location"]

        class Memory(dict):
            permanent = False

        memory = Memory()
        listing_filter_memory.remember(
            memory, MultiDict([("mode", "combined"), ("duplicate_of", "5")])
        )
        assert "duplicate_of" not in memory.get(listing_filter_memory.SESSION_KEY, "")

    def test_a_bare_cookie_less_page_and_an_empty_subscription_render(
        self, client, profile
    ):
        assert client.get("/properties").status_code == 200
        body = _page(client, profile)
        assert _found(body) == 0
        assert _folded_line(body) is None


class TestTheNumbersAddUp:
    def test_pages_plus_folded_equal_the_unfolded_total(self, client, profile):
        for n in range(12):
            _confirmed_pair(profile, 20000000 + n)
        for n in range(6):
            _row(profile, f"single{n}", url=FOTOCASA.format(n=7000 + n))
        everything = _page(client, profile, duplicates="show", per_page=100)
        total = _found(everything)
        assert total == 30

        seen, pages = [], []
        for page in (1, 2):
            body = _page(client, profile, per_page=10, page=page)
            pages.append(len(_shown(body)))
            seen.extend(_shown(body))
        assert _found(body) == 18
        # A page is full: folding happened before paging, not after.
        assert pages == [10, 8]
        assert len(set(seen)) == 18
        line = _folded_line(body)
        assert "12 publications of the same homes are folded" in line
        assert _found(body) + 12 == total

    def test_sort_order_of_the_unfolded_rows_is_unchanged(self, client, profile):
        for n in range(4):
            _confirmed_pair(profile, 30000000 + n)
        for n in range(5):
            _row(profile, f"s{n}", url=FOTOCASA.format(n=8000 + n))
        folded = _shown(_page(client, profile, sort="price", order="asc"))
        unfolded = _shown(
            _page(client, profile, sort="price", order="asc", duplicates="show")
        )
        assert folded == [i for i in unfolded if i in set(folded)]
        assert len(unfolded) - len(folded) == 4


class TestCsvAndApiCarryTheGroup:
    def test_rows_say_which_group_they_stand_for(self, client, profile):
        first, second = _confirmed_pair(profile, 40404040)
        lone = _row(profile, "lone", url=FOTOCASA.format(n=9003))
        rows = {int(r["ID"]): r for r in _csv(client, profile)}
        assert rows[first.id]["duplicate_group_size"] == "2"
        assert rows[first.id]["duplicate_member_ids"] == f"{first.id} {second.id}"
        assert rows[first.id]["duplicate_tier"] == "confirmed"
        assert rows[lone.id]["duplicate_group_size"] == "1"
        assert rows[lone.id]["duplicate_member_ids"] == ""
        assert rows[lone.id]["duplicate_tier"] == ""

        by_id = {p["id"]: p for p in _api(client, profile)["properties"]}
        assert by_id[first.id]["duplicate_member_ids"] == [first.id, second.id]
        assert by_id[first.id]["duplicate_tier"] == "confirmed"
        assert by_id[lone.id]["duplicate_group_size"] == 1
        full = _api(client, profile, full=1)["properties"]
        assert {p["id"]: p for p in full}[first.id]["duplicate_group_size"] == 2

    def test_show_and_duplicate_of_reach_the_export_api_and_map(self, client, profile):
        first, second = _confirmed_pair(profile, 50505050)
        _row(profile, "lone", url=FOTOCASA.format(n=9004))
        both = {first.id, second.id}
        assert {int(r["ID"]) for r in _csv(client, profile, duplicates="show")} >= both
        assert both <= {
            p["id"] for p in _api(client, profile, duplicates="show")["properties"]
        }
        assert both <= _markers(client, profile, duplicates="show")
        assert {
            int(r["ID"]) for r in _csv(client, profile, duplicate_of=second.id)
        } == both
        assert {
            p["id"] for p in _api(client, profile, duplicate_of=second.id)["properties"]
        } == both
        assert _markers(client, profile, duplicate_of=first.id) == both

    def test_a_focused_pin_is_never_folded_away(self, client, profile):
        first, second = _confirmed_pair(profile, 60606060)
        assert second.id not in _markers(client, profile)
        assert second.id in _markers(client, profile, focus=second.id)


class TestTheCostIsBounded:
    def _statements(self, app):
        seen = []

        def count(conn, cursor, statement, *_):
            seen.append(statement)

        event.listen(db.engine, "before_cursor_execute", count)
        try:
            duplicate_fold.compute(Property.query)
        finally:
            event.remove(db.engine, "before_cursor_execute", count)
        return len(seen)

    def test_three_statements_whatever_the_size(self, app, profile):
        for n in range(3):
            _confirmed_pair(profile, 70000000 + n)
        small = self._statements(app)
        for n in range(3, 40):
            _confirmed_pair(profile, 70000000 + n)
            _row(profile, f"x{n}", url=FOTOCASA.format(n=6000 + n))
        large = self._statements(app)
        assert small == large <= 3

    def test_a_selection_over_the_cap_is_not_folded_and_says_so(
        self, client, profile, monkeypatch
    ):
        _confirmed_pair(profile, 80808080)
        monkeypatch.setattr(duplicate_fold, "FOLD_MAX_ROWS", 1)
        body = _page(client, profile)
        assert _found(body) == 2
        assert 'id="duplicates-fold-skipped"' in body
        assert _folded_line(body) is None


class TestADecisionStaysOnItsRow:
    def test_the_rest_of_the_group_is_not_decided_and_stays_in_new(
        self, client, profile
    ):
        first, second = _confirmed_pair(profile, 90909090)
        response = client.post(
            f"/api/property/{first.id}/review", json={"decision": "interested"}
        )
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Property, second.id).owner_verdict is None
        # Folded under the decided, protected row on the plain list ...
        assert _shown(_page(client, profile)) == [first.id]
        # ... and still in the New queue, which excludes the decided row.
        assert _shown(_page(client, profile, verdict="undecided")) == [second.id]
        assert "not decided" in _page(client, profile)
