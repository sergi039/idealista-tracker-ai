"""The CSV export holds the rows the page it was taken from is showing.

The export button sits beside the result count, exports what that count is
counting, and its title says "Download filtered data as CSV". It rebuilds the
filter chain by hand, and on 2026-08-20 one filter had never arrived:
`measured` was wired into `/properties` and into that page's links (#377-#380)
and reached neither `export_properties_csv()` nor the export href. Measured on
production that day at `profile_id=all`, the page showed **72** listings and
the button exported **471** -- the whole table, under a promise of the filtered
one. #439 fixed it.

**This file adds no production change and finds no defect that is live today.**
It is a regression net, and it is worth having for one reason: every filter in
this codebase is currently guarded by a *named* test, written by whoever added
that filter. Measured rather than assumed -- dropping the export's `verdict`
clause and running the whole suite with this file excluded turns exactly one
test red, `tests/test_owner_review_propagation.py::TestBothFiltersTogether::
test_the_csv_export_applies_the_pair`, so that coverage is real and this file
does not replace it. What no named test can do is cover the filter whose author
forgets to write one, and that is the failure that actually happened, five
times in one day: `source`, `advertiser`, `measured`, `verdict` and `action`
each reached some surfaces and not others, and a person found every one.

So the question asked here is not "is `measured` exported". It is the property
the export owes for **every** filter at once: *for one URL, the export and the
page hold the same rows.*

`test_the_sweep_covers_every_filter_the_page_has` is what makes that claim true
rather than aspirational. `FILTERS` below is hand-written, and a hand-written
list is the very defect this file is about, so it is checked instead of
trusted: `current_filters` is read out of the rendered page and every key must
be swept or excused by name. A filter added to `/properties` fails here until
somebody gives it a value.

That guard exists because its absence was caught in review, in this file's
sibling: `tests/test_map_and_list_agree_on_the_filters.py` claimed to walk "the
whole vocabulary" while walking 10 of 12, and disabling the map's `sea_view`
clause left every test green. The first version of this file made the same
unchecked claim.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# A value per filter, chosen to really split the fixture: one that selected
# everything would let a missing filter pass unnoticed, which
# `test_the_matrix_bites` keeps honest.
FILTERS = [
    ("category", "land"),
    ("subtype", "plot"),
    ("municipality", "Castrillon"),
    ("source", "fotocasa"),
    ("advertiser", "owner"),
    ("search", "Findable"),
    ("inv_metr", "EXCELLENT"),
    ("sea_view", "yes"),
    ("measured", "full"),
    ("favorites", "on"),
    ("verdict", "rejected"),
    ("action", "overdue"),
    ("sea_dist", "800"),
]

# Combinations, because a filter can be applied on both sides and still be
# combined differently -- and because `hide_removed` is resolved through
# `utils/listing_status_scope.py` (#439) rather than standing alone.
COMBINATIONS = [
    "profile_id=all",
    "profile_id=all&measured=full&hide_removed=on",
    "profile_id=all&measured=full&hide_removed=off",
    "profile_id=all&advertiser=owner&measured=full",
    "profile_id=all&category=land&search=Findable",
    # No `profile_id`: both routes then resolve the subscription through their
    # own auto-selection fallback, and this fixture holds exactly one
    # subscription so both land on it. If a second is ever added here, this
    # entry must gain an explicit `profile_id` rather than be deleted -- the
    # fallbacks differ, and that is a separate question from filter parity.
    "measured=full",
]

# Keys of `current_filters` that are not filters, and why.
NOT_A_FILTER = {
    "profile_id": "the subscription selection, replaced rather than narrowed",
    "sort_by": "ordering",
    "order": "ordering",
    "page": "pagination",
    "per_page": "pagination",
    "mode": "which score is emphasised",
    "active_mode": "derived from the applied sort, never sent",
    "view_type": "cards or table",
    "hide_removed": "swept through COMBINATIONS, in both spellings",
}


def _csv_ids(body: str) -> set[int]:
    rows = list(csv.reader(io.StringIO(body)))
    assert rows, "the export produced no output at all"
    assert rows[0][0] == "ID", f"expected an ID column first, got {rows[0][:3]}"
    return {int(row[0]) for row in rows[1:] if row and row[0].strip()}


def _page_ids(client, query: str) -> set[int]:
    """Every listing the page shows for this query, across its pages."""
    ids: set[int] = set()
    page = 1
    while True:
        body = client.get(f"/properties?{query}&per_page=100&page={page}").get_data(
            as_text=True
        )
        ids |= {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}
        if 'rel="next"' not in body:
            break
        page += 1
    return ids


def _export_ids(client, query: str) -> set[int]:
    return _csv_ids(
        client.get(f"/properties/export.csv?{query}").get_data(as_text=True)
    )


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
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
        source_email_id=f"exp_{key}",
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
def listings(app):
    """One listing per way of failing each filter."""
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        pid = profile.id

        rows = {
            "other_category": _make(pid, "other_category", category="housing"),
            "other_subtype": _make(pid, "other_subtype", subtype="house"),
            "other_municipality": _make(
                pid, "other_municipality", municipality="Gijon"
            ),
            "other_source": _make(pid, "other_source", site="idealista"),
            "agency": _make(pid, "agency", seller="agency", site="idealista"),
            "unfindable": _make(pid, "unfindable", title="Nothing matches here"),
            "half_measured": _make(pid, "half_measured", share=0.5),
            # No recorded coverage at all: must not read as fully measured.
            "no_coverage": _make(pid, "no_coverage", share=None),
            "favorite": _make(pid, "favorite", is_favorite=True),
            "rejected": _make(pid, "rejected", owner_verdict="rejected"),
            "overdue": _make(
                pid,
                "overdue",
                next_action="Ask the agency for the cadastral reference",
                next_action_due_on=date(2020, 1, 1),
            ),
            "sea_yes": _make(
                pid, "sea_yes", enrichment={"environment": {"sea_view": "yes"}}
            ),
            # The one row `sea_dist=800` keeps: every other row carries no sea
            # block, so the cut bites without a "far" twin.
            "near_sea": _make(
                pid,
                "near_sea",
                enrichment={"sea": {"status": "ok", "distance_m": 350.0}},
            ),
            "excellent": _make(
                pid,
                "excellent",
                ai_analysis={
                    "rental_market_analysis": {"investment_rating": "EXCELLENT"}
                },
            ),
            # Withdrawn: the one row `hide_removed` moves.
            "removed": _make(pid, "removed", listing_status="removed"),
        }
        db.session.add_all(list(rows.values()))
        db.session.commit()
        return {name: row.id for name, row in rows.items()}


class TestOneUrlOneRowSet:
    @pytest.mark.parametrize("name,value", FILTERS)
    def test_each_filter_alone(self, client, listings, name, value):
        query = f"profile_id=all&{name}={value}"
        page, export = _page_ids(client, query), _export_ids(client, query)

        assert export == page, (
            f"for ?{name}={value} the page holds {len(page)} listings and the "
            f"export {len(export)}; only exported: {sorted(export - page)}, "
            f"only on the page: {sorted(page - export)}"
        )

    @pytest.mark.parametrize("query", COMBINATIONS)
    def test_combinations(self, client, listings, query):
        page, export = _page_ids(client, query), _export_ids(client, query)

        assert export == page, (
            f"for ?{query} the page holds {len(page)} listings and the export "
            f"{len(export)}; only exported: {sorted(export - page)}, only on "
            f"the page: {sorted(page - export)}"
        )


class TestTheSweepIsReallyASweep:
    def test_the_sweep_covers_every_filter_the_page_has(self, app, client, listings):
        """`FILTERS` is hand-written, so it is checked against the page.

        A filter added to `/properties` fails here until somebody gives it a
        value or names it in `NOT_A_FILTER`. Without this the file would make
        the same unchecked claim its sibling was caught making in review --
        walking part of the vocabulary while saying it walked all of it."""
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            assert client.get("/properties?profile_id=all").status_code == 200
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
            f"these filters are applied by /properties and not swept here: "
            f"{missing}. Add a value to FILTERS, or name it in NOT_A_FILTER "
            "with the reason it is not a filter."
        )

    def test_the_matrix_bites(self, client, listings):
        """Every filter must actually exclude something here, or a filter
        missing from the export would satisfy the sweep while doing nothing."""
        everything = _page_ids(client, "profile_id=all")
        toothless = [
            f"{name}={value}"
            for name, value in FILTERS
            if _page_ids(client, f"profile_id=all&{name}={value}") == everything
        ]

        assert not toothless, f"these filters excluded nothing: {toothless}"

    def test_hide_removed_moves_a_row_in_both_spellings(self, client, listings):
        """`hide_removed` is excused from FILTERS because COMBINATIONS sweeps
        it; this is what makes that excuse true rather than a way out."""
        hidden = _page_ids(client, "profile_id=all&measured=full&hide_removed=on")
        shown = _page_ids(client, "profile_id=all&measured=full&hide_removed=off")

        assert listings["removed"] in shown - hidden


class TestTheButtonSendsWhatThePageIsApplying:
    @pytest.mark.parametrize("name,value", FILTERS)
    def test_the_export_href_carries_the_filter(self, client, listings, name, value):
        """The route honouring a filter is half of it. On 2026-08-20 the other
        half was the defect: the button on a `measured=full` page built a URL
        without it."""
        body = client.get(f"/properties?profile_id=all&{name}={value}").get_data(
            as_text=True
        )
        href = re.search(r'href="(/properties/export\.csv\?[^"]*)"', body)
        assert href, "no export link on the page"

        assert f"{name}={value}" in href.group(1).replace("&amp;", "&"), (
            f"the export button dropped ?{name}={value}, which the page is applying"
        )
