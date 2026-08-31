"""`/properties` says how much of the number it sorts by was actually measured.

#379 made the score honest per row: a criterion nobody could measure scores
`None`, the branch average renormalises without it, and the payload records the
share of enabled weight that answered. What it could not say is anything about
the **set** — and on this table the set is where the surprise is.

Measured on production 2026-08-26 (#493), of 948 located rows: 678 carry
`travel: approximate_origin` and 628 carry `sea: approximate_origin`. For
roughly 70% of them the drive times and the sea distance are measured, stored,
rendered on the page — and scored by nothing, leaving `value` + `size` carrying
`score_total` alone. Every abstention is correct on its own terms; what was
missing is that a 0–100 silently meaning one thing here and another thing there
looks like a composite ranking while being a single-axis one.

So a third coverage line, beside the listing-status and hazard-scan ones it is
modelled on. Three things these tests pin, and the third is the one that makes
it a disclosure rather than a second wrong number:

* it counts over the **filtered** set, because the total beside it is filtered
  too and two numbers on one line have to be about the same rows;
* it reads the **same predicate** `measured=full` filters on, so the header and
  the filter cannot disagree;
* a row whose share was **never recorded** is not counted as full — that is
  `_score_coverage_share_expr`'s own rule ("unknown coverage must not pass as
  full"), and counting it would be #98 inside the line written to prevent #98.
"""

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
        profile = SearchProfile(
            name="Galicia · costa",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        app.config["TEST_PROFILE_ID"] = profile.id
        with app.test_client() as test_client:
            yield test_client
        db.drop_all()


def _row(app_client, *, share, municipality="Vigo", **kwargs):
    """One listing whose stored payload records `share` (None = never recorded)."""
    from flask import current_app

    scoring = {"profiles": {}}
    if share is not None:
        scoring["coverage"] = {"share": share}
    prop = Property(
        source_email_id=f"row:{id(scoring)}",
        url=f"https://www.idealista.com/inmueble/{abs(id(scoring)) % 100000}/",
        title="Casa en venta",
        municipality=municipality,
        price=200000,
        area=200,
        scoring=scoring,
        search_profile_id=current_app.config["TEST_PROFILE_ID"],
        **kwargs,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _line(client, url="/properties"):
    response = client.get(url)
    assert response.status_code == 200, "the template did not render"
    body = response.get_data(as_text=True)
    assert "properties found" in body, "not the listing page"
    found = re.search(r'id="score-basis-coverage".*?</span>', body, re.S)
    return found.group(0) if found else None


def test_the_line_counts_only_the_rows_that_rest_on_everything(client):
    _row(client, share=1.0)
    _row(client, share=1.0)
    _row(client, share=0.4)

    line = _line(client)
    assert line is not None, "the disclosure did not render"
    assert "2 of 3" in line


def test_a_row_whose_coverage_was_never_recorded_is_not_counted_as_full(client):
    """`share` absent is unknown, and unknown must not pass as measured."""
    _row(client, share=1.0)
    _row(client, share=None)

    assert "1 of 2" in _line(client)


def test_the_line_is_counted_over_the_filtered_set_not_the_table(client):
    """Two numbers on one line have to be about the same rows."""
    _row(client, share=1.0, municipality="Vigo")
    _row(client, share=0.4, municipality="Vigo")
    _row(client, share=1.0, municipality="Boiro")

    assert "1 of 2" in _line(client, "/properties?municipality=Vigo")


def test_the_header_agrees_with_the_filter_it_shares_a_predicate_with(client):
    """`measured=full` must return exactly the rows this line counts."""
    _row(client, share=1.0)
    _row(client, share=0.4)
    _row(client, share=None)

    assert "1 of 3" in _line(client)

    filtered = client.get("/properties?measured=full")
    assert filtered.status_code == 200
    body = filtered.get_data(as_text=True)
    assert re.search(r"\b1 properties found", body), (
        "the filter and the header disagree about which rows rest on everything"
    )
