"""The five attribute filters of the listing surfaces, importable by all of them.

`inv_metr`, `sea_view`, `sea_dist`, `build` and `measured` narrow the same
table on the same vocabulary wherever they appear, and until 2026-09-01 the
readings were private to `routes/main_routes.py` — which is exactly how
`GET /api/properties` came to accept all five and apply none: the closing
audit of the criteria work measured `?profile_id=24&sea_view=likely`
answering `scope.total: 393` against a page showing 18, `measured=full`
393 against 3, `sea_dist=800` 393 against 66. That is #445's regression —
a filter one surface keeps and another drops disagrees about which listings
exist — and the same structural cause the criteria reading had before #519
moved it into `services/subscription_criteria.py`. The remedy is the same:
one home, imported, never re-derived.

Two contracts every helper here keeps, because callers rely on both:

* **An unknown value hands back the *same* query object.** `/properties`'
  `filter_bar_active` reads object identity, so `sea_view=banana` must not
  count as a narrowing that never happened — and `GET /api/properties` reads
  the same identity to say out loud that a value it was sent did not narrow
  the answer.
* **Absence never reads as a measurement** (#98). Every expression is NULL
  where nobody measured — a threshold filter can never read absence as
  nearness, and "unknown coverage" must not pass as "full".
"""

from sqlalchemy import case, func

from services import sea_view_service

INVESTMENT_RATING_ORDER = ("BELOW", "MODERATE", "GOOD", "EXCELLENT")


def investment_rating_expr(model):
    """Upper-cased `ai_analysis.rental_market_analysis.investment_rating`.

    Shared by /lands and /properties, and by both CSV exports, so the filter
    and the sort cannot drift apart between the two models.
    """
    return func.upper(
        func.coalesce(
            model.ai_analysis["rental_market_analysis"][
                "investment_rating"
            ].as_string(),
            "",
        )
    )


def filter_by_investment_rating(query, model, raw_value):
    """Keep only rows whose investment rating starts with `raw_value`."""
    wanted = (raw_value or "").strip().upper()
    if wanted not in INVESTMENT_RATING_ORDER:
        return query
    return query.filter(investment_rating_expr(model).like(f"{wanted}%"))


def investment_rating_rank(model):
    """Sortable rank for the investment rating; NULL when there is none."""
    rating = investment_rating_expr(model)
    return case(
        *[
            (rating.like(f"{label}%"), position)
            for position, label in enumerate(INVESTMENT_RATING_ORDER, start=1)
        ],
        else_=None,
    )


# Sea view is a four-state verdict, not a flag -- see services/sea_view_service
# for why. The filter offers two useful cuts of it: the corroborated rows, and
# everything geometry or the listing text says is plausible.
SEA_VIEW_FILTER_VALUES = {
    "yes": ("yes",),
    "likely": ("yes", "likely"),
    # A bookmark from the retired /lands page says sea_view=on and means the
    # old boolean. Reading it as "yes or likely" is the closest honest
    # translation of what it used to select.
    "on": ("yes", "likely"),
}


# The spellings of a legacy `true` as a JSON leaf read as text: PostgreSQL's
# `->>` renders a JSON boolean as `true`, SQLite's json_extract as `1`. The
# SQL reading below casts with `as_boolean()`; the Python reading in
# services/favorite_similarity.py parses the text and imports this list so
# the two cannot disagree about what a legacy flag says.
LEGACY_SEA_VIEW_TRUE_TEXT = ("true", "1")


def sea_view_state_expr(model):
    """Effective sea-view state, with the mirrored `Land` boolean folded in.

    Legacy rows keep their flag at enrichment.legacy_land.environment.sea_view.
    It came from the same weak keyword pass the new verdict replaces, so a
    legacy `true` reads as `likely` and never as `yes`; a legacy `false` is not
    evidence of anything and stays absent.

    Only the four known states are recognised at the computed path. Anything
    else there -- a boolean the pre-verdict environment endpoint might have
    left behind -- falls through to NULL, which reads as `unknown`: the
    conservative answer, and the only one both dialects agree on, since a JSON
    boolean casts to `true` on PostgreSQL and `1` on SQLite.
    """
    computed = model.enrichment["environment"]["sea_view"].as_string()
    legacy = model.enrichment["legacy_land"]["environment"]["sea_view"].as_boolean()
    return case(
        (computed.in_(sea_view_service.VALID_STATES), computed),
        (legacy.is_(True), "likely"),
        else_=None,
    )


def score_coverage_share_expr(model):
    """`scoring.coverage.share` as a float; NULL where it was never recorded.

    #379. Recorded by `PropertyScoringService` from this change on; a row
    scored before it has no value here and therefore does not pass a
    "fully measured" filter until it is rescored -- the honest reading, since
    the SQL cannot re-derive the share the way `score_coverage()` does in
    Python, and "unknown coverage" must not pass as "full".
    """
    return model.scoring["coverage"]["share"].as_float()


def filter_by_measured(query, model, raw_value):
    """`measured=full` keeps rows whose every enabled criterion answered."""
    if (raw_value or "").strip().lower() != "full":
        return query
    return query.filter(score_coverage_share_expr(model) >= 0.999)


def filter_by_sea_view(query, model, raw_value):
    """Keep only rows whose sea-view verdict is in the requested bucket."""
    wanted = SEA_VIEW_FILTER_VALUES.get((raw_value or "").strip().lower())
    if not wanted:
        return query
    return query.filter(sea_view_state_expr(model).in_(wanted))


# Walking-reach cuts of the sea distance, in metres over the ground at ~5 km/h:
# 400 m is five minutes, 800 m ten, 1600 m twenty. Straight-line metres, so the
# real walk is never shorter than the label -- the option text says both.
SEA_DISTANCE_FILTER_VALUES = {
    "400": 400.0,
    "800": 800.0,
    "1600": 1600.0,
}


def sea_distance_m_expr(model):
    """Metres to the coastline as measured; NULL without a measurement.

    Reads the parcel-grade figure first (`distance_m`, written only for a
    precise coordinate) and falls back to the centroid figure
    (`origin_distance_m`, what an approximate row's measurement is really
    about, #358) -- the same two numbers the property page and the plot badge
    already show, captioned. The two never coexist in one payload
    (services/sea_distance_service.py writes `distance_m: None` beside
    `origin_distance_m`), so the coalesce is a fallback, not a preference.
    JSON null reads as SQL NULL, so a refusal, a measured "no coastline within
    the radius" and a row nobody measured all stay NULL -- a threshold filter
    can never read absence as nearness (#98).
    """
    return func.coalesce(
        model.enrichment["sea"]["distance_m"].as_float(),
        model.enrichment["sea"]["origin_distance_m"].as_float(),
    )


def filter_by_sea_distance(query, model, raw_value):
    """Keep only rows measured within the requested distance of the sea.

    Unknown values hand back the *same* query object -- `filter_bar_active`
    reads object identity, and `sea_dist=banana` must not count as a
    narrowing that never happened.
    """
    threshold = SEA_DISTANCE_FILTER_VALUES.get((raw_value or "").strip())
    if threshold is None:
        return query
    return query.filter(sea_distance_m_expr(model) <= threshold)


# `attributes.land_classification` is a *curated* field: hand-run curation
# scripts write it from planning documents, portal claims and research sheets,
# and ingestion deliberately preserves it
# (tests/test_ingest_preserves_curated_fields.py). Its vocabulary, measured in
# production rather than invented here:
#   urbano_solar        - urban/solar, a dwelling may be built now
#   urbanizable         - buildable only after urbanisation/gestion completes
#   residential_claimed - the seller claims buildability; no document seen
# A row with no value was never curated -- it is offered as its own bucket and
# never folded into any of the three (#98: absence is not a classification).
LAND_CLASSIFICATION_FILTER_VALUES = {
    "solar": ("urbano_solar",),
    "urbanizable": ("urbanizable",),
    "claimed": ("residential_claimed",),
}


def land_classification_expr(model):
    """The curated classification as text; NULL where nobody curated one."""
    return model.attributes["land_classification"].as_string()


def filter_by_land_classification(query, model, raw_value):
    """Keep only rows whose curated buildability matches the request.

    `classified` keeps any curated row (IS NOT NULL rather than an IN-list,
    so a new vocabulary value is not silently dropped from its own bucket);
    the named buckets match exactly. Unknown values hand back the same query
    object, the `filter_bar_active` identity contract.
    """
    wanted = (raw_value or "").strip().lower()
    if wanted == "classified":
        return query.filter(land_classification_expr(model).isnot(None))
    values = LAND_CLASSIFICATION_FILTER_VALUES.get(wanted)
    if not values:
        return query
    return query.filter(land_classification_expr(model).in_(values))


# Every attribute filter in one place, in the order the filter bar draws them,
# for a surface that applies them as a set and has to SAY which of the values
# it was sent did not narrow anything. The page applies each behind its own
# `if`, so the table is additive -- a filter added here without a reader is
# dead weight, and one added to a route without being added here cannot occur,
# because the route imports its applier from this table's own module.
ATTRIBUTE_FILTERS = (
    ("inv_metr", filter_by_investment_rating),
    ("sea_view", filter_by_sea_view),
    ("sea_dist", filter_by_sea_distance),
    ("build", filter_by_land_classification),
    ("measured", filter_by_measured),
)

# What each parameter accepts, for the sentence a surface owes a caller whose
# value narrowed nothing. `classified` and `full` are spelled here rather than
# derived because they are readings, not dictionary keys.
ATTRIBUTE_FILTER_VOCABULARY = {
    "inv_metr": INVESTMENT_RATING_ORDER,
    "sea_view": tuple(SEA_VIEW_FILTER_VALUES),
    "sea_dist": tuple(SEA_DISTANCE_FILTER_VALUES),
    "build": tuple(LAND_CLASSIFICATION_FILTER_VALUES) + ("classified",),
    "measured": ("full",),
}
