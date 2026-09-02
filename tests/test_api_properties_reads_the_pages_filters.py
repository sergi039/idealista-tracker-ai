"""`GET /api/properties` reads every filter the page reads, and says so.

The closing audit of the criteria-disclosure work (2026-09-01) measured the
endpoint accepting five of the page's filter parameters and applying none:
`?profile_id=24&sea_view=likely` answered `scope.total: 393` against a page
showing 18, `measured=full` 393 against 3, `sea_dist=800` 393 against 66,
`build=solar` and `inv_metr=GOOD` 393 against 0. That is #445's regression —
a filter one surface keeps and another drops disagrees about which listings
exist — in the endpoint #519 had just fixed ONE parameter of, and the scope
block #519 added described only the `criteria` fields, so a consumer reading
it concluded the disclosure was complete.

Two repairs, both pinned here:

* The five readings moved to `services/listing_attribute_filters.py` and the
  endpoint applies them — the same remedy #519 applied to `criteria`, whose
  reading was private to `routes/main_routes.py`, "which is exactly why
  `routes/api_routes.py` grew a fifth, silently-ignoring answer".
* The scope block now reports what was READ (`scope.filters_read`, from the
  record of the reads, `utils/listing_filters.FilterArgs`) and what arrived
  without being read (`scope.params_ignored`) — measured, not maintained, so
  the next filter cannot go missing while the block reads as complete.
  Since #534 `filters_read` is per request — the filters THIS request
  carried and the endpoint read with a value — so the two lists partition
  what the caller sent; the endpoint's whole vocabulary is named in the note
  an ignored parameter earns, and nowhere as a field. The same ticket
  silenced the `criteria` note at zero: the count stays, the sentence goes.

The acceptance condition follows `tests/test_map_and_list_agree_on_the_filters
.py`: **one URL, one set**, walked over the whole filter vocabulary rather
than over the five names — and the vocabulary itself is checked against the
rendered page's `current_filters` rather than trusted, because a hand-written
list of filters is the thing that keeps going stale (#445).
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# A value for every filter, chosen to really split the fixture. Checked below
# against the page rather than trusted: `test_the_sweep_covers_every_filter_
# the_page_has` reads `current_filters` out of the rendered `/properties` and
# fails on anything neither here nor excused by name.
FILTERS = [
    ("category", "land"),
    ("subtype", "plot"),
    ("municipality", "Castrillon"),
    ("source", "fotocasa"),
    ("advertiser", "owner"),
    ("search", "Findable"),
    ("measured", "full"),
    ("criteria", "fail"),
    ("favorites", "on"),
    ("verdict", "rejected"),
    ("action", "overdue"),
    ("sea_view", "yes"),
    ("inv_metr", "EXCELLENT"),
    ("sea_dist", "800"),
    ("build", "solar"),
    # Likeness to the subscription's favorites (services/favorite_similarity
    # .py). `match_all` is the favorite, so it is the reference the rest are
    # measured against; a row of another kind (`other_subtype`) is never
    # similar, and a row nobody can place is never counted as similar.
    ("similar", "70"),
    # Honored by both surfaces, in both spellings: an explicit value wins on
    # the page (utils/listing_status_scope.py) and in the endpoint's own
    # parse. `off` *widens* rather than narrows, which the agreement test is
    # indifferent to — one URL, one set, whichever way the set moved.
    ("hide_removed", "off"),
]

# Keys of the page's `current_filters` that are not filters of this endpoint,
# and why. Anything else the page carries must appear in FILTERS above.
NOT_A_FILTER = {
    "profile_id": (
        "the endpoint's own parameter — one integer id, disclosed via "
        "profile_id_source; the page's all/repeated spellings are its own"
    ),
    "sort_by": "ordering, not membership",
    "order": "ordering, not membership",
    "page": "the page's pagination; the API's is limit/offset, and a sent "
    "`page` is disclosed in scope.params_ignored (pinned below)",
    "per_page": "same as `page`",
    "mode": "which score the page emphasises",
    "active_mode": "derived from the applied sort, never sent",
    "view_type": "cards or table",
}


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


def _make(profile_id, key, **kw):
    share = kw.pop("share", 1.0)
    seller = kw.pop("seller", "owner")
    site = kw.pop("site", "fotocasa")
    campaign = "particular" if seller == "owner" else "professional"
    url = (
        f"https://www.fotocasa.es/es/comprar/terreno/aviles/{abs(hash(key)) % 90000}/d"
        if site == "fotocasa"
        else f"https://www.idealista.com/inmueble/{abs(hash(key)) % 90000}/"
        f"?utm_campaign=express_newAd_sale_{campaign}"
    )
    return Property(
        source_email_id=f"api445_{key}",
        title=kw.pop("title", f"Findable plot {key}"),
        municipality=kw.pop("municipality", "Castrillon"),
        property_category=kw.pop("category", "land"),
        property_subtype=kw.pop("subtype", "plot"),
        price=40000,
        url=url,
        scoring=({"coverage": {"share": share}} if share is not None else None),
        search_profile_id=profile_id,
        listing_status=kw.pop("listing_status", "active"),
        **kw,
    )


@pytest.fixture
def world(app):
    """One listing per way of failing each filter, under one subscription.

    The subscription carries criteria so `criteria=fail` has a verdict to
    select — without them the sweep row is toothless and the bites test
    rightly says so.
    """
    profile = SearchProfile(
        name="Land at Norte",
        is_active=True,
        is_default=True,
        criteria={"min_house_m2": 150},
    )
    db.session.add(profile)
    db.session.commit()
    pid = profile.id

    rows = {
        "match_all": _make(pid, "match_all", is_favorite=True),
        "other_category": _make(pid, "other_category", category="housing"),
        "other_subtype": _make(pid, "other_subtype", subtype="house"),
        "other_municipality": _make(pid, "other_municipality", municipality="Gijon"),
        "other_source": _make(pid, "other_source", site="idealista"),
        "agency": _make(pid, "agency", seller="agency", site="idealista"),
        "unfindable": _make(pid, "unfindable", title="Nothing matches here"),
        "half_measured": _make(pid, "half_measured", share=0.5),
        "not_favorite": _make(pid, "not_favorite"),
        "rejected": _make(pid, "rejected", owner_verdict="rejected"),
        # The one row criteria=fail keeps — and the one the DEFAULT view
        # hides, so the two surfaces disagree exactly here if either drops
        # the parameter.
        "criteria_fail": _make(pid, "criteria_fail", area=100.0, area_type="built"),
        "sea_yes": _make(
            pid, "sea_yes", enrichment={"environment": {"sea_view": "yes"}}
        ),
        "near_sea": _make(
            pid, "near_sea", enrichment={"sea": {"status": "ok", "distance_m": 350.0}}
        ),
        "buildable": _make(
            pid, "buildable", attributes={"land_classification": "urbano_solar"}
        ),
        "excellent": _make(
            pid,
            "excellent",
            ai_analysis={"rental_market_analysis": {"investment_rating": "EXCELLENT"}},
        ),
        "overdue": _make(
            pid,
            "overdue",
            next_action="Ask for the cadastral reference",
            next_action_due_on=date(2020, 1, 1),
        ),
        # What `hide_removed=off` adds back — and what both defaults hide.
        "withdrawn": _make(pid, "withdrawn", listing_status="removed"),
    }
    db.session.add_all(list(rows.values()))
    db.session.commit()
    return {"pid": pid, "ids": {name: row.id for name, row in rows.items()}}


def _api(client, query: str) -> dict:
    response = client.get(f"/api/properties?{query}")
    assert response.status_code == 200, query
    payload = response.get_json()
    assert payload["success"] is True, query
    return payload


def _api_ids(client, query: str) -> set[int]:
    return {prop["id"] for prop in _api(client, f"{query}&limit=200")["properties"]}


def _listed(client, query: str) -> set[int]:
    body = client.get(f"/properties?{query}&per_page=100").get_data(as_text=True)
    return {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}


class TestOneUrlOneSet:
    """The API's answer is the page's answer, over the whole vocabulary."""

    @pytest.mark.parametrize("name,value", FILTERS)
    def test_the_api_narrows_exactly_as_the_page_does(self, client, world, name, value):
        query = f"profile_id={world['pid']}&{name}={value}"
        assert _api_ids(client, query) == _listed(client, query), (
            f"?{name}={value} selects different listings on the two surfaces"
        )

    def test_the_matrix_bites(self, client, world):
        """Each value must move the API's own baseline, or a parameter both
        surfaces dropped would satisfy the agreement test above."""
        everything = _api_ids(client, f"profile_id={world['pid']}")
        toothless = [
            f"{name}={value}"
            for name, value in FILTERS
            if _api_ids(client, f"profile_id={world['pid']}&{name}={value}")
            == everything
        ]
        assert not toothless, f"these filters moved nothing: {toothless}"

    def test_the_sweep_covers_every_filter_the_page_has(self, app, client, world):
        """`FILTERS` is a hand-written table, so it is checked, not trusted:
        every key of the rendered page's `current_filters` must be swept above
        or excused by name — a filter added to `/properties` fails here until
        somebody gives it a value, which is the failure this file exists to
        produce (#445)."""
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            assert client.get("/properties").status_code == 200
        finally:
            template_rendered.disconnect(record, app)

        assert seen, "properties.html did not render"
        swept = {name for name, _ in FILTERS}
        missing = sorted(
            key
            for key in seen[-1]["current_filters"]
            if key not in swept and key not in NOT_A_FILTER
        )
        assert not missing, (
            f"these filters are applied by /properties and not swept against "
            f"the API here: {missing}. Add a value to FILTERS, or name it in "
            "NOT_A_FILTER with the reason."
        )

    def test_scope_total_is_the_narrowed_population(self, client, world):
        """The audit's own reproduction, by name: `sea_view` narrowed the page
        to a fraction while `scope.total` stayed the whole subscription."""
        pid = world["pid"]
        assert _api(client, f"profile_id={pid}&sea_view=likely")["scope"]["total"] == 1
        assert _api(client, f"profile_id={pid}&measured=full")["scope"]["total"] == len(
            _listed(client, f"profile_id={pid}&measured=full")
        )


class TestTheScopeBlockStatesWhatWasReadAndWhatWasNot:
    """#519's disclosure said only the criteria fields, so it read as
    complete while five parameters were dropped in silence."""

    def test_every_filter_the_endpoint_reads_is_in_filters_read(self, client, world):
        """Sent all at once, because `filters_read` is per request (#534): a
        bare request reads no filter and reports none, so the check that
        every swept filter is reported has to send every swept filter — and
        it asserts the whole list by value, so a sixteenth name that is not
        in the sweep is as loud as a swept one that is missing."""
        every = "&".join(f"{name}={value}" for name, value in FILTERS)
        reported = _api(client, f"profile_id={world['pid']}&{every}")["scope"][
            "filters_read"
        ]
        assert reported == sorted(name for name, _ in FILTERS)

    def test_filters_read_is_what_this_request_carried(self, client, world):
        """By value, on exactly two filters and on none (#534). Before this
        the field named all sixteen filters the endpoint knows on a request
        that sent no filter at all — a capability list under a name that
        reads as "these narrowed your result". A presence-only assertion
        passes either way and proves neither, which is why both lists are
        compared whole."""
        pid = world["pid"]
        two = _api(client, f"profile_id={pid}&sea_view=yes&category=land")["scope"]
        assert two["filters_read"] == ["category", "sea_view"]
        assert two["params_ignored"] == []

        none = _api(client, f"profile_id={pid}&limit=5")["scope"]
        assert none["filters_read"] == []
        assert none["params_ignored"] == []

    def test_filters_read_and_params_ignored_partition_what_was_sent(
        self, client, world
    ):
        """Every key the caller sent is in exactly one of the two lists, less
        the endpoint's own parameters — so a caller accounts for each
        parameter they sent from the structured block alone, without the
        prose. A blank value is the page's spelling for "no filter" and is in
        neither: it was read, and it applied nothing."""
        pid = world["pid"]
        scope = _api(
            client,
            f"profile_id={pid}&limit=5&category=land&advertiser=owner"
            "&page=3&municipality=",
        )["scope"]
        assert scope["filters_read"] == ["advertiser", "category"]
        assert scope["params_ignored"] == ["page"]
        # And the note that names an ignored parameter is where the whole
        # vocabulary now lives, since the field no longer carries it.
        note = next(line for line in scope["notes"] if "page" in line)
        for name, _ in FILTERS:
            assert name in note, (name, note)

    def test_the_criteria_note_is_silent_when_nothing_was_hidden(self, client, world):
        """The count stays — `criteria_hidden_by_default: 0` is data — and
        the sentence goes: a note saying nothing was hidden, on a request
        where nothing was, is noise, and noise is how a true disclosure stops
        being read (#534). Pinned against the SAME fixture in both states, so
        a suppression that also silenced the non-zero case is caught here
        and not only in test_criteria_on_every_surface.py."""
        pid = world["pid"]
        narrowed = _api(client, f"profile_id={pid}&sea_view=yes")["scope"]
        assert narrowed["criteria_applied"] == "default"
        assert narrowed["criteria_hidden_by_default"] == 0
        assert not any(
            "hidden by the default reading" in line for line in narrowed["notes"]
        ), narrowed["notes"]

        bare = _api(client, f"profile_id={pid}")["scope"]
        assert bare["criteria_hidden_by_default"] == 1
        assert any("criteria: 1 listing(s)" in line for line in bare["notes"]), bare[
            "notes"
        ]

    def test_a_parameter_nothing_reads_is_named_ignored(self, client, world):
        payload = _api(client, f"profile_id={world['pid']}&page=2&banana=1")
        assert payload["scope"]["params_ignored"] == ["banana", "page"]
        notes = " ".join(payload["scope"]["notes"])
        assert "banana" in notes and "page" in notes, (
            "an ignored parameter is disclosed in prose too, because `page` "
            "silently ignored reads as page 2"
        )
        # And the ignored parameters really were ignored: same answer.
        assert (
            payload["scope"]["total"]
            == (_api(client, f"profile_id={world['pid']}")["scope"]["total"])
        )

    def test_params_ignored_is_one_enumeration_of_the_request(self, client, world):
        """A parameter the page grows tomorrow, which this endpoint has never
        heard of, is named without anyone editing the endpoint — because
        `params_ignored` is what the request carried minus what was read
        (`utils/listing_filters.RecordedArgs.unread`), never a constant
        listing today's parameters. Filters and the endpoint's own
        parameters sit on the read side of that subtraction alike."""
        pid = world["pid"]
        payload = _api(
            client,
            f"profile_id={pid}&sea_view=yes&limit=5&order=asc"
            "&tomorrows_page_filter=1&another_one=2",
        )
        assert payload["scope"]["params_ignored"] == [
            "another_one",
            "tomorrows_page_filter",
        ]
        assert payload["scope"]["filters_read"] == ["sea_view"]

    def test_the_endpoint_reads_the_request_through_the_record_only(self):
        """Structural: `get_properties` touches the request's argument
        mapping exactly once — to wrap it. A read that bypassed the record
        would be reported as ignored while being honored, the false
        disclosure in the other direction, and no black-box request can
        tell that from an honest one."""
        import inspect

        from routes import api_routes

        source = inspect.getsource(api_routes.get_properties)
        assert source.count("request.args") == 1, source.count("request.args")

    def test_the_endpoints_own_parameters_are_not_called_ignored(self, client, world):
        payload = _api(
            client,
            f"profile_id={world['pid']}&limit=5&offset=0&sort=price&order=asc&full=1",
        )
        assert payload["scope"]["params_ignored"] == []

    def test_an_unrecognized_value_is_measured_and_named(self, client, world):
        """`sea_view=banana` narrows nothing — the helper's own same-object
        contract — and an unfiltered answer with nothing saying so reads as
        'banana matched everything'."""
        pid = world["pid"]
        baseline = _api(client, f"profile_id={pid}")
        payload = _api(client, f"profile_id={pid}&sea_view=banana")
        assert payload["scope"]["total"] == baseline["scope"]["total"]
        note = next(
            (line for line in payload["scope"]["notes"] if "sea_view" in line), None
        )
        assert note is not None, "a value that narrowed nothing must be named"
        assert "banana" in note and "likely" in note, (
            "the note names the value and the vocabulary the filter reads"
        )
        # A refused value is still a READ: the parameter stays in
        # `filters_read` (it is in neither list otherwise, and the two are
        # meant to partition what was sent) and the note beside it says what
        # the value did, which is nothing.
        assert payload["scope"]["filters_read"] == ["sea_view"]
        assert payload["scope"]["params_ignored"] == []

    def test_a_recognized_value_gets_no_unrecognized_note(self, client, world):
        payload = _api(client, f"profile_id={world['pid']}&sea_view=yes")
        assert not any(
            "sea_view" in line and "not a value" in line
            for line in payload["scope"]["notes"]
        )
