import logging
import math
import os
import re
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import NamedTuple
import hashlib

from flask import (
    Blueprint,
    abort,
    current_app,
    render_template,
    request,
    redirect,
    send_from_directory,
    session,
    url_for,
    flash,
    jsonify,
)

# get_or_404 raises HTTPException, and the blanket `except Exception` handlers
# below would answer 500 for it: every one of them re-raises it first so an
# unknown id stays a 404 (issue #136).
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer
from models import Land, Property, SearchProfile
from app import db, limiter
from services import sea_view_service
from services import subscription_criteria
from services.listing_attribute_filters import (
    filter_by_investment_rating,
    filter_by_land_classification,
    filter_by_measured,
    filter_by_sea_distance,
    filter_by_sea_view,
    investment_rating_rank,
    score_coverage_share_expr,
)
from services import taste_service
from services import favorite_similarity
from services.coordinate_quality import shared_coordinate_peers
from services import advertiser
from services.hazard_service import (
    complete_expression as hazard_complete_expression,
    read_verdict as hazard_verdict,
)
from services import attachments as attachments_service
from services import owner_review
from services.listing_verification import (
    read_verdict as listing_verdict,
    verified_expression,
)
from services.profile_selection import (
    MAX_SELECTED_PROFILE_IDS,
    PROFILE_UNASSIGNED_SENTINEL,
    ProfileSelection,
    ProfileSelectionState,
    apply_profile_filter,
    empty_profile_selection,
    parse_profile_selection,
    resolve_profile_selection,
)
from utils.google_spend import (
    CAP_INGEST_GEOCODE,
    authorized_spend,
    cap_for_rows,
)
from utils.i18n import t
from utils.listing_filters import (
    CLEARED_NOT_ABSENT,
    NON_FILTERS,
    FilterArgs,
    rebuilt_from,
)
from utils.listing_search import interpret_search, listing_search_clause
from utils.listing_source import source_filter_clause
from utils.listing_status_scope import resolve_hide_removed
from utils.municipality_grouping import (
    group_key,
    group_municipalities,
    municipality_filter_clause,
)
from utils.redirects import safe_referrer_redirect

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)

# Working-page controls carried over from /lands by issue #105.
PROPERTY_MODE_SORT_DEFAULTS = {
    "combined": "score_total",
    "investment": "score_investment",
    "lifestyle": "score_lifestyle",
    # The owner's taste (issue #498). A fourth display mode rather than a
    # twelfth table column: the score cell shows the taste score, so the
    # documented tablet layout keeps its width.
    "taste": "taste_score",
}
PROPERTY_VIEW_TYPES = ("cards", "list")
# The table is what a bare /properties opens on (owner decision, 2026-08-09):
# it puts price, area, travel and date side by side, which the cards cannot.
DEFAULT_PROPERTY_VIEW_TYPE = "list"

# How many links one import may carry. Not a database limit -- it bounds how
# long somebody else's server is asked to serve us in one press: at the three
# second courtesy pace in `services/fotocasa_source.py`, a hundred links is
# five minutes of fetching. Past this the answer is two imports, which costs
# the owner one extra paste and keeps every run reviewable on one screen.
MAX_IMPORT_URLS = 100
# A bare /properties must open on the freshest listings, so the mode default
# only applies once the user actually picks a mode.
DEFAULT_PROPERTY_SORT = "created_at"

# What the Type / Subtype filters send for "classified as nothing at all".
# A query-string sentinel, never a stored value.
UNCLASSIFIED_FILTER = "__none__"

# Listing statuses the "Hide removed" default leaves out of every listing
# surface. Named because the map has to tell a reader that this, and not a
# filter they set, is why the listing they asked to focus is absent (#287).
DELISTED_LISTING_STATUSES = ("removed", "sold")


SCORING_FIELD_PREFIX = "scoring"


def _scoring_field_name(category: str, section: str, key: str) -> str:
    """One naming scheme for the per-subscription scoring inputs (#239)."""
    return f"{SCORING_FIELD_PREFIX}__{category}__{section}__{key}"


def _scoring_form_model(profile) -> list:
    """What the subscription page renders: every editable weight, its stored
    override and the default it falls back to.

    Built from `PropertyScoringService`, never from a copy of its numbers — the
    page and the scoring cannot drift apart that way. Categories the
    subscription actually holds listings in come first, so the ones that matter
    are the ones on screen.
    """
    from models import Property
    from services.property_scoring_service import PropertyScoringService

    service = PropertyScoringService()
    stored = profile.scoring_config if isinstance(profile.scoring_config, dict) else {}
    stored_categories = (
        stored.get("categories") if isinstance(stored.get("categories"), dict) else {}
    )

    counts = dict(
        db.session.query(Property.property_category, db.func.count(Property.id))
        .filter(Property.search_profile_id == profile.id)
        .group_by(Property.property_category)
        .all()
    )

    model = []
    for category in service.known_categories():
        defaults = service.defaults_for(category)
        cat_stored = (
            stored_categories.get(category)
            if isinstance(stored_categories.get(category), dict)
            else {}
        )
        sections = []
        for section, keys in service.EDITABLE_SECTIONS.items():
            section_stored = (
                cat_stored.get(section)
                if isinstance(cat_stored.get(section), dict)
                else {}
            )
            sections.append(
                {
                    "name": section,
                    "fields": [
                        {
                            "key": key,
                            "input_name": _scoring_field_name(category, section, key),
                            "value": section_stored.get(key),
                            "default": defaults.get(section, {}).get(key),
                        }
                        for key in keys
                    ],
                }
            )
        model.append(
            {
                "category": category,
                "listing_count": int(counts.get(category) or 0),
                "sections": sections,
                "overridden": bool(cat_stored),
            }
        )

    model.sort(key=lambda entry: (-entry["listing_count"], entry["category"]))
    return model


def _unmanaged_scoring_keys(profile) -> list:
    """Anything stored in `scoring_config` the form does not render.

    A hand-written key is kept on save rather than dropped; naming it here
    keeps it from being invisible instead.
    """
    from services.property_scoring_service import PropertyScoringService

    service = PropertyScoringService()
    stored = profile.scoring_config if isinstance(profile.scoring_config, dict) else {}
    known_sections = service.EDITABLE_SECTIONS
    unmanaged = []

    for key in stored:
        if key != "categories":
            unmanaged.append(key)

    categories = stored.get("categories")
    if not isinstance(categories, dict):
        return sorted(unmanaged)

    for category, cat_cfg in categories.items():
        if category not in service.known_categories():
            unmanaged.append(f"categories.{category}")
            continue
        if not isinstance(cat_cfg, dict):
            continue
        for section, values in cat_cfg.items():
            if section not in known_sections:
                unmanaged.append(f"categories.{category}.{section}")
                continue
            if isinstance(values, dict):
                for key in values:
                    if key not in known_sections[section]:
                        unmanaged.append(f"categories.{category}.{section}.{key}")
    return sorted(unmanaged)


def _unusable_scoring_numbers(config: dict) -> list:
    """Every scoring_config value that claims to be a number and is not.

    Returns "categories.land.investment.price = 'high'"-shaped strings, so the
    message names the key rather than saying the document is wrong somewhere.
    """
    numeric_sections = ("investment", "lifestyle", "travel_minutes", "combined_mix")
    problems = []

    categories = config.get("categories")
    if not isinstance(categories, dict):
        return problems

    for category, cat_cfg in categories.items():
        if not isinstance(cat_cfg, dict):
            continue
        for section in numeric_sections:
            values = cat_cfg.get(section)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if value is None:
                    continue
                try:
                    float(value)
                except (TypeError, ValueError):
                    problems.append(
                        f"categories.{category}.{section}.{key} = {value!r}"
                    )
    return problems


def _partial_save_message(kind: str, kept: int, dropped: list) -> tuple:
    """The flash for a save that stored some of what was submitted.

    A blanket "saved" over a partial save is what let a mistyped rule vanish
    without a word (#241). `warning` is the category this module already uses
    for a partial outcome.
    """
    if not dropped:
        return (f"{kind.capitalize()} saved", "success")
    positions = ", ".join(f"#{index}" for index in dropped)
    return (
        f"Saved {kept} of {kept + len(dropped)} {kind}; "
        f"dropped {len(dropped)} invalid ({positions}). "
        "Check the pattern and the required fields.",
        "warning",
    )


def _nearest_beach_minutes(model):
    """Drive minutes to the nearest measured beach; NULL without one.

    Items are nearest-first (services/property_travel_service.py sorts and
    dedupes them), so element 0 IS the nearest beach. Shared by the list sort
    and the CSV export so the two allow-lists cannot drift apart. Live again
    per issue #271 — the #98 placeholder said "not one row holds a travel
    time", and the Phase-2 backfill made that false.
    """
    return model.travel["beaches"]["items"][0]["duration_min"].as_float()


def _clear_filters_url():
    """The narrowing note's "clear filters" link: every filter dropped,
    the subscription selection, the sort, the view state and the toolbar
    switches kept. `criteria` is dropped too (its absence is the default
    hide the note's baseline is counted under), which is why this does NOT
    apply `CLEARED_NOT_ABSENT` the way `_empty_state_scope` does."""
    args = rebuilt_from(request.args, keep=NON_FILTERS | {"favorites", "hide_removed"})
    args["page"] = 1
    return url_for("main.properties", **args)


def _similarity_scope(profile_selection):
    """The subscriptions a listing surface may be asked to count under the
    similarity clause: every visible one (live and archived, both carry a
    count in the menu) and every one the URL named, hidden or not."""
    from services.search_profile_service import SearchProfileService

    scope = {
        profile.id
        for profile in SearchProfileService.list_visible_profiles(active_only=False)
    }
    scope.update(int(pid) for pid in (profile_selection.filter_ids or ()))
    return sorted(scope)


def _criteria_context():
    """This page family's criteria clauses, over `Property`.

    The reading itself lives in `services/subscription_criteria.py` — the
    same module `routes/api_routes.py` reaches, because a filter one surface
    keeps and another drops is #445's regression and that is precisely what
    `GET /api/properties` was doing with this parameter.
    """
    return subscription_criteria.profile_context(Property)


# The filter bar's dimensions, in the order the page applies them. One home
# for the page's own rows AND for every count drawn beside its controls: a
# counted option is a promise about the page that picking it opens, and the
# only way that promise cannot drift is for the count to be taken by the same
# function that will narrow that page -- with the option's own dimension
# skipped, because picking it replaces that dimension's value (closing-audit
# findings 2 and 3, and the reviewer's second-order case below).
FILTER_BAR_DIMENSIONS = (
    "category",
    "subtype",
    "municipality",
    "source",
    "advertiser",
    "verdict",
    "action",
    "search",
    "inv_metr",
    "sea_view",
    "sea_dist",
    "build",
    "measured",
    "similar",
)


def _apply_filter_bar(query, bar, review_today, skip=None, similarity_ctx=None):
    """Narrow `query` by every filter-bar value in `bar`, except `skip`.

    `bar` maps each of FILTER_BAR_DIMENSIONS to its raw request value. Every
    clause hands back the *same* query object when its value is empty or
    unknown (the `filter_bar_active` identity contract on /properties), and
    this function keeps that property: it returns `query` itself when
    nothing narrowed.

    Which site (utils/listing_source.py), who is selling
    (services/advertiser.py), what the owner decided and what is still
    outstanding (services/owner_review.py, applied together -- a surface that
    keeps one of the pair and drops the other is the regression the two are
    tested against together), a pasted listing URL or bare id as a search
    (utils/listing_search.py), the five attribute filters
    (services/listing_attribute_filters.py) and the likeness to the
    subscription's favorites (services/favorite_similarity.py, read through
    `similarity_ctx`, the request's one reading of it) all keep their one
    home; this function only applies them in one order.
    """

    def wanted(name):
        return "" if name == skip else (bar.get(name) or "")

    category_filter = wanted("category")
    if category_filter:
        if category_filter == UNCLASSIFIED_FILTER:
            query = query.filter(_unclassified_clause(Property.property_category))
        else:
            query = query.filter(Property.property_category == category_filter)
    subtype_filter = wanted("subtype")
    if subtype_filter:
        if subtype_filter == UNCLASSIFIED_FILTER:
            query = query.filter(_unclassified_clause(Property.property_subtype))
        else:
            query = query.filter(Property.property_subtype == subtype_filter)
    municipality_filter = wanted("municipality")
    if municipality_filter:
        query = query.filter(municipality_filter_clause(municipality_filter))
    source_clause = source_filter_clause(Property, wanted("source"))
    if source_clause is not None:
        query = query.filter(source_clause)
    advertiser_clause = advertiser.filter_clause(Property, wanted("advertiser"))
    if advertiser_clause is not None:
        query = query.filter(advertiser_clause)
    verdict_clause = owner_review.decision_filter_clause(Property, wanted("verdict"))
    if verdict_clause is not None:
        query = query.filter(verdict_clause)
    action_clause = owner_review.action_filter_clause(
        Property, wanted("action"), review_today
    )
    if action_clause is not None:
        query = query.filter(action_clause)
    search_clause = listing_search_clause(Property, wanted("search"))
    if search_clause is not None:
        query = query.filter(search_clause)
    investment_metrics_filter = wanted("inv_metr")
    if investment_metrics_filter:
        query = filter_by_investment_rating(query, Property, investment_metrics_filter)
    sea_view_filter = wanted("sea_view")
    if sea_view_filter:
        query = filter_by_sea_view(query, Property, sea_view_filter)
    sea_distance_filter = wanted("sea_dist")
    if sea_distance_filter:
        query = filter_by_sea_distance(query, Property, sea_distance_filter)
    build_filter = wanted("build")
    if build_filter:
        query = filter_by_land_classification(query, Property, build_filter)
    measured_filter = wanted("measured")
    if measured_filter:
        query = filter_by_measured(query, Property, measured_filter)
    similar_filter = wanted("similar")
    if similar_filter:
        query = favorite_similarity.apply_filter(
            query, Property, similarity_ctx, similar_filter
        )
    return query


def _shows_rows_the_default_hides(query, ctx):
    """Whether the rows on screen include any that the DEFAULT criteria
    reading hides — the reading the "clear filters" link restores.

    This is the honest form of the question the "N of M shown" note asks,
    because that sentence claims the N rows are part of the M the link lands
    on. It reads the rows rather than the `criteria` parameter: the spelling
    of the mode does not decide whether the two sets nest, the subscription's
    own criteria do. A scope with no criteria at all nests under every mode.
    """
    if ctx is None:
        return False
    return query.filter(ctx["hidden_default"]).first() is not None


# The `criteria` parameter's vocabulary AS A URL SPEAKS IT. The reading itself
# lives in `services.subscription_criteria` — that is what #519 moved it there
# for, because `routes/api_routes.py` had no way to reach it while it was
# private here and grew a fifth answer instead. What stays here is the URL
# side of the same rule, and the two vocabularies are deliberately not the
# same word: the service spells the default `default`, a URL spells it by
# saying nothing at all, and `criteria_mode()` is the one place that
# translation happens so a link never carries a redundant spelling.
CRITERIA_MODES = tuple(
    mode for mode in subscription_criteria.FILTER_MODES if mode != "default"
)

# What a control offers, in the order a reader meets them: the default first
# (the empty string IS the default reading, not a fifth mode), then everything,
# then the verdicts from most to least welcome. The order is a display choice
# and differs from CRITERIA_MODES, so the two are tied by an assertion rather
# than by hope — a template that grew a fifth option nobody applies, or lost
# one nobody could reach, is the disagreement this pair exists to prevent.
CRITERIA_CHOICES = ("", "all", "pass", "unknown", "fail")
assert set(CRITERIA_CHOICES) == {""} | set(CRITERIA_MODES), (
    "the control must offer exactly the modes the code applies"
)


def criteria_mode(raw_value):
    """The canonical spelling of the `criteria` parameter for a URL, `""` for
    default. One reading — `subscription_criteria.read_filter_mode` — with the
    service's `default` rendered as absence, which is how a link says it."""
    mode, _ = subscription_criteria.read_filter_mode(raw_value)
    return "" if mode == "default" else mode


def _apply_criteria_filter(query, ctx, raw_value, count_hidden=False):
    """The list, the map, the CSV and /municipalities, through the one reading.

    A thin bind over `subscription_criteria.apply_filter` and nothing else:
    `GET /api/properties` reaches the same function directly, which is the
    whole point of it living in the service. Returning `query` **itself** when
    nothing was applied is relied upon by `_split_by_criteria`, which reads it
    as "this reading excluded nothing" and skips a COUNT. It is NOT what
    `/properties`' `filter_bar_active` tests: that is computed before this runs,
    deliberately, because the default hide is the page's standing policy and
    not a filter-bar action.
    """
    return subscription_criteria.apply_filter(query, ctx, raw_value, count_hidden)


def _split_by_criteria(query, ctx, raw_value):
    """A scope's rows, split into the ones this criteria reading counts and
    the ones it does not.

    For a surface that has to *disclose* what it narrowed rather than only
    apply it: /municipalities re-states every count and median over the kept
    rows, and says how many listings — and how many municipalities — the
    reading is not counting. The excluded rows are what that sentence is
    counted from.

    The split is made by the SAME expression `_apply_criteria_filter` applies,
    read back as a set of ids, and never by a second reading of the rule in
    Python: `hidden_by_default_expression` lives in SQL only (its favorited /
    reviewed / open-action exemptions with it), and a Python twin written here
    would be a second home for it — the defect this codebase keeps naming.

    `_apply_criteria_filter` returns the query object *itself* when nothing is
    applied (no subscription carries criteria, or the mode is `all`), and this
    function is that identity test's only consumer — `/properties`'
    `filter_bar_active` is a different test taken earlier, over the filter bar
    alone. That branch costs no extra query and reports nothing excluded,
    which is the truth: the reading excluded nothing.
    """
    kept_query, _ = _apply_criteria_filter(query, ctx, raw_value)
    rows = query.all()
    if kept_query is query:
        return rows, []
    kept_ids = {row[0] for row in kept_query.with_entities(Property.id)}
    return (
        [prop for prop in rows if prop.id in kept_ids],
        [prop for prop in rows if prop.id not in kept_ids],
    )


def _map_auto_profile_id(default_profile, profiles, focus_property=None):
    """The map's own fallback when the request names no profile.

    A `focus=<id>` settles it before any of the fallbacks below are consulted:
    the caller asked for one listing, and only the subscription holding that
    listing can answer. The fallbacks are about which subscription is most
    useful to open cold, which is routinely not that one -- issue #287, where
    the map icon on a property page linked here with `focus` and no
    `profile_id`, the biggest subscription won, and the listing was quietly
    left out of the marker set while the page fitted bounds over everything
    else. An inactive subscription is a valid answer here: its listings are
    real, and a link that names one asked for it explicitly.

    Deliberately different from `/properties` and `/properties/export.csv`,
    which both rewrite a bare request to `all`: a map is useless without
    coordinates, so the profile with the most mappable rows wins. When nothing
    has coordinates yet it falls back to the most recently active profile,
    then the default, then the first one. (This paragraph said "which takes
    the richest active profile" until 2026-08-31 -- true of the list until the
    owner's 2026-08-09 decision, and never re-read afterwards. The helper it
    named, `resolve_richest_active_profile_id`, outlived its last caller by
    the same inattention and is gone.)

    Every candidate is a *visible* subscription. A hidden one is still a valid
    answer when the link names it -- the `focus` branch above reaches one, and
    so does `profile_id=<id>` -- but opening a bare /map on the subscription
    the owner took off the screens is the one thing hiding is supposed to
    prevent (#403). The two queries below filtered on `is_active` alone, so
    the busiest hidden subscription won a cold map.

    Because the two pages resolve differently, whatever this returns has to
    travel in the link back to the list (`ResolvedProfileSelection.
    link_values`) or the user lands on a different subscription than the map
    just showed -- and the focused listing may not even be loaded there.
    """
    from services.search_profile_service import SearchProfileService

    if focus_property is not None and focus_property.search_profile_id is not None:
        return int(focus_property.search_profile_id)

    mappable = (
        db.session.query(
            Property.search_profile_id,
            func.count(Property.id).label("cnt"),
            func.max(Property.created_at).label("latest"),
        )
        .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
        .filter(SearchProfile.is_active.is_(True))
        .filter(SearchProfileService.visible_clause())
        .filter(Property.location_lat.isnot(None), Property.location_lon.isnot(None))
        .filter(Property.listing_status.notin_(DELISTED_LISTING_STATUSES))
        .group_by(Property.search_profile_id)
        .order_by(func.count(Property.id).desc(), func.max(Property.created_at).desc())
        .first()
    )
    if mappable and mappable[0] is not None:
        return int(mappable[0])

    recent = (
        db.session.query(
            Property.search_profile_id,
            func.max(Property.created_at).label("latest"),
        )
        .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
        .filter(SearchProfile.is_active.is_(True))
        .filter(SearchProfileService.visible_clause())
        .group_by(Property.search_profile_id)
        .order_by(func.max(Property.created_at).desc())
        .first()
    )
    if recent and recent[0] is not None:
        return int(recent[0])
    if default_profile:
        return default_profile.id
    if profiles:
        return profiles[0].id
    return None


# Widest id PostgreSQL accepts in the `properties.id` integer column. A larger
# number in `?focus=` is not a listing anybody can have linked to, and handing
# it to the driver raises rather than returning nothing.
MAX_FOCUS_ID = 2_147_483_647

# ASCII digits only, and spelled out rather than `\d`, which matches every
# decimal digit in Unicode. `str.isdigit()` is wider still -- it accepts `²`
# and `①`, which `int()` then rejects. See `_parse_focus_id`.
_FOCUS_ID_TOKEN = re.compile(r"[0-9]+")


def _parse_focus_id(raw):
    """`?focus=` as a listing id, or None when the request names none.

    Anything that is not a plain positive in-range integer reads as no focus
    at all -- the same shape as the rest of the query string, where an
    unparseable value falls back to the page default instead of failing. That
    contract is only kept if *nothing* here raises: `map_view` wraps its body
    in a blanket `except Exception` that flashes an error and renders an empty
    map, so a `ValueError` in this parser would turn `?focus=<junk>` into a
    broken page rather than a page without a focus. Both of `int()`'s refusals
    are therefore handled here, the same way
    `services.profile_selection.parse_profile_selection` handles them.
    """
    token = str(raw if raw is not None else "").strip()
    if not _FOCUS_ID_TOKEN.fullmatch(token):
        return None
    try:
        value = int(token)
    except ValueError:
        # Longer than CPython's 4300-digit conversion limit. Still not an id.
        return None
    if not 0 < value <= MAX_FOCUS_ID:
        return None
    return value


def _map_focus_link(profile_id, keep_filters=True):
    """A `/map` URL for the same focused listing under a different subscription.

    Rebuilt from `request.args` so the reader keeps everything else they had
    -- including `focus` itself, which is already in there. Only `profile_id`
    is replaced; with `keep_filters=False` the narrowing filters go too, which
    is the only honest offer when one of *them* is what hid the listing.
    """
    # Clearing is expressed as "keep the non-filters", never as a list of the
    # filters to remove. The list version named seven and had gone stale by
    # four -- `source`, `advertiser`, `verdict`, `action` -- so the one link
    # whose entire promise is that clearing works re-issued the filter that had
    # hidden the listing and landed on the identical notice (#445). Inverted,
    # a filter added tomorrow is unknown here and therefore dropped, which is
    # the right answer without anyone maintaining anything.
    args = rebuilt_from(
        request.args,
        drop={"profile_id"},
        keep=None if keep_filters else NON_FILTERS - {"profile_id"},
    )
    if not keep_filters:
        # ...except for the filters whose ABSENCE is not their off position:
        # dropping `criteria` re-issues the default hide, so the link that
        # promises to show the listing has to say `criteria=all` out loud
        # (`utils/listing_filters.CLEARED_NOT_ABSENT`). Without this the
        # notice for a criteria-hidden listing linked to itself.
        args.update(CLEARED_NOT_ABSENT)
    return url_for("main.map_view", profile_id=profile_id, **args)


def _map_focus_notice(focus_id, focus_property, props, query_without_profile):
    """Why the listing the caller asked to focus is not among the markers.

    The page cannot stay silent about this. `templates/map.html` falls back to
    fitting the bounds of every marker when the focused id is missing, which
    looks exactly like a map that merely declined to zoom -- so in issue #287
    the owner found the defect instead of us, and the two red dots they were
    looking at belonged to a subscription the listing was never in.

    The reasons are checked in the order the query applies them, so each one
    is the *first* thing that excluded the listing rather than any true
    statement about it. Returns None when there is nothing to explain: no
    `focus` in the request, or the listing is on the map.
    """
    if focus_id is None or any(prop.id == focus_id for prop in props):
        return None

    label = f"Listing #{focus_id}"
    if focus_property is None:
        return {
            "reason": "unknown",
            "text": f"{label} was not found, so the map has nothing to focus on.",
        }
    if focus_property.location_lat is None or focus_property.location_lon is None:
        return {
            "reason": "no_coordinates",
            "text": (
                f"{label} has no coordinates yet, so it cannot be placed on the map."
            ),
        }
    if focus_property.listing_status in DELISTED_LISTING_STATUSES:
        return {
            "reason": "delisted",
            "text": (
                f"{label} is marked {focus_property.listing_status}, "
                "and the map leaves those out."
            ),
        }

    # Every filter but the subscription one lets it through, so the
    # subscription is the whole reason -- and the offer can keep the reader's
    # filters, which are now proven not to exclude this listing.
    if query_without_profile.filter(Property.id == focus_id).first() is not None:
        profile_id = focus_property.search_profile_id
        profile = (
            db.session.get(SearchProfile, profile_id)
            if profile_id is not None
            else None
        )
        if profile_id is None:
            return {
                "reason": "other_subscription",
                "text": f"{label} has no subscription, and this map is not showing those.",
                "href": _map_focus_link(PROFILE_UNASSIGNED_SENTINEL),
                "link_text": "Show it anyway",
            }
        name = (profile.name if profile else None) or f"subscription #{profile_id}"
        return {
            "reason": "other_subscription",
            "text": (
                f'{label} is in the "{name}" subscription, '
                "which this map is not showing."
            ),
            "href": _map_focus_link(profile_id),
            "link_text": "Show it there",
        }

    # Coordinates, status and subscription are all fine, so one of the
    # narrowing filters is hiding it. Clearing them is guaranteed to work.
    return {
        "reason": "filtered",
        "text": f"{label} is hidden by the filters on this map.",
        "href": _map_focus_link(
            focus_property.search_profile_id
            if focus_property.search_profile_id is not None
            else PROFILE_UNASSIGNED_SENTINEL,
            keep_filters=False,
        ),
        "link_text": "Clear the filters and show it",
    }


def _listing_reveal_link(prop, search_query):
    """A `/properties` URL guaranteed to put `prop` on the page.

    Built the way `_map_focus_link(keep_filters=False)` is, and for the same
    reason: clearing is expressed as "keep the non-filters", never as a list
    of filters to remove, so a filter added tomorrow is unknown here and
    therefore dropped (#445). `search` is then restated deliberately -- it is
    what names the listing, exactly as `focus` does on the map, and the row
    matches it by construction.

    Two values are stated out loud because their ABSENCE is not their off
    position. `criteria` is the one `utils/listing_filters.CLEARED_NOT_ABSENT`
    exists for: dropping it re-issues the default hide, which is how a
    criteria-hidden listing came to be offered a link back to the notice that
    sent the reader. `hide_removed` is the second: it defaults ON, and
    `NON_FILTERS` carries `mode` and `view_type` across, which is precisely
    what `resolve_hide_removed` reads as "this came from the filter form" and
    answers OFF to -- so leaving it unsaid makes the promise depend on which
    link the reader arrived by. It is relaxed only for a row that is actually
    withdrawn, because `off` also puts every other delisted listing back.
    """
    args = rebuilt_from(
        request.args,
        drop={"profile_id"},
        keep=NON_FILTERS - {"profile_id"},
    )
    args.update(CLEARED_NOT_ABSENT)
    # `on` is safe only for a row the ON filter provably KEEPS, and that is a
    # narrower set than "not delisted": the page filters with
    # `Property.listing_status.notin_(DELISTED_LISTING_STATUSES)`, and under
    # SQL's three-valued logic `NULL NOT IN (...)` is NULL, so a NULL-status
    # row is dropped by the very setting a Python `not in` test calls safe.
    # A link that promises to reveal a listing must not rest on the two
    # languages disagreeing about NULL -- the same trap
    # `subscription_criteria` records for its own definite clauses. Production
    # holds no NULL status today (measured: 1536 active, 1 removed), but direct
    # SQL is a supported workflow here, and a reader that refuses a shape only
    # because today's writer cannot produce it is a reader that trusts the
    # writer.
    survives_hide_removed = (
        prop.listing_status is not None
        and prop.listing_status not in DELISTED_LISTING_STATUSES
    )
    args["hide_removed"] = "on" if survives_hide_removed else "off"
    args["search"] = search_query
    args["page"] = 1
    profile_id = (
        prop.search_profile_id
        if prop.search_profile_id is not None
        else PROFILE_UNASSIGNED_SENTINEL
    )
    return url_for("main.properties", profile_id=profile_id, **args)


def _search_reveal_notice(read, shown_total, profile_selection):
    """What an empty result really means when the query named a listing.

    "0 properties found" under a listing id has two readings, and the page
    printed only one of them: `search_read_as_listing` says "nothing here
    carries that id", which is a claim about the TABLE made from a FILTERED
    query. Measured on production 2026-08-31:
    `/properties?search=35241157` printed it for property 1458 four lines
    above its own "Criteria: 1 failing hidden", and
    `/properties?search=112408790&municipality=Gijón` printed it for property
    1537, which no criteria hide at all. So this is not a criteria defect --
    ANY narrowing produces it, and the fix is to ask the table.

    Returns None when there is nothing to explain (rows on screen, or a query
    that names no listing). `absent` keeps the old sentence, which is true
    there. `hidden` names the row and carries a link that reveals it.
    """
    if shown_total or read is None or not read.is_listing_reference:
        return None
    clause = listing_search_clause(Property, read.query)
    prop = (
        Property.query.filter(clause).order_by(Property.id.asc()).first()
        if clause is not None
        else None
    )
    if prop is None:
        return {"state": "absent"}

    # Whether the subscription selection is what excludes it, asked through
    # the page's own rule rather than by re-deriving one -- a second reading
    # of "is this row in the selection" is how two surfaces come to disagree.
    in_selection = (
        apply_profile_filter(
            Property.query.filter(Property.id == prop.id),
            Property.search_profile_id,
            profile_selection,
        ).first()
        is not None
    )
    return {
        "state": "hidden",
        "property_id": prop.id,
        "by_subscription": not in_selection,
        "href": _listing_reveal_link(prop, read.query),
    }


def _empty_state_scope(profile_selection, criteria_ctx):
    """What the reader's subscription selection holds with nothing narrowing.

    The empty state named one remedy -- ingest more listings -- for a page
    whose rows were already in the table: measured on production 2026-08-31,
    `/properties?profile_id=24&search=Brantuas` offered "run a manual sync"
    for a phrase out of property 995's own title. So the page asks how many
    listings clearing everything would land on, and says so only when that is
    a real offer. Counted on the zero-result path alone, where an extra
    COUNT(*) costs nothing anybody is waiting for.

    Returns (total, url). The count is taken under `criteria=all` and with
    both toolbar switches off, which is exactly what the link states.
    """
    query = apply_profile_filter(
        Property.query, Property.search_profile_id, profile_selection
    )
    query, _ = _apply_criteria_filter(query, criteria_ctx, "all")
    args = rebuilt_from(request.args, keep=NON_FILTERS)
    args.update(CLEARED_NOT_ABSENT)
    # Named for the same reason as in `_listing_reveal_link`: `hide_removed`
    # is on unless something says otherwise, and "clear every filter" has to
    # mean the switches too or the count and the page it lands on disagree.
    args["hide_removed"] = "off"
    args["page"] = 1
    return query.count(), url_for("main.properties", **args)


class ProfileListingCount(NamedTuple):
    """What the subscription controls need to know about one profile's rows.

    `live` is the number a human is shown. `held` decides whether the option
    is offered at all, and the two are different facts on purpose: an active
    subscription whose every listing is delisted has `live == 0`, but
    `profile_id=all` still renders its rows the moment Hide removed is
    switched off, so dropping its chip and its checkbox on `live` alone left
    rows on screen that no subscription control could reach (the #472 review
    finding, reproduced). "Shows nothing under the default switch" and "can
    only ever return an empty page" are not the same claim.
    """

    live: int
    held: int


EMPTY_PROFILE_COUNT = ProfileListingCount(live=0, held=0)


def _listing_counts_by_profile(live_query=None):
    """Per-profile `ProfileListingCount`, `None` keyed for the unassigned.

    One group-by shared by the menu and by the hidden-subscription note, which
    used to ask the same table twice per render of /properties -- once for the
    options and once, through a join, for the disclosure line.

    Withdrawn and sold listings are out of `live` (owner decision, 2026-08-21,
    #470): the badge sits beside a result count whose default scope hides
    them, and a chip saying 4 over a page saying 3 was the narrower survivor
    of the confusion #469 fixed. The exclusion is the same expression the
    Hide removed switch applies, so the two agree row for row -- including on
    a NULL status, which `notin_` drops from both. It is deliberately
    unconditional: the badge answers "how many live listings does this
    subscription hold", the way the portal's own saved search would, and does
    not follow the switch -- with Hide removed off the page can show more
    rows than the badge, which the switch's own pressed state already
    explains. /map's hidden-subscription note reads this helper too, and that
    surface excludes delisted listings unconditionally, so the live count is
    the matching one there. /profiles keeps its own raw inventory count on
    purpose, and /municipalities keeps its "what picking it would show" count
    -- three different questions, each answered where it is asked.

    `live` is counted over `live_query` -- the page's own narrowed query with
    only its subscription filter left open -- so the badge on a chip is the
    size of the page its own href opens: the href carries every filter and
    the criteria mode (`base_args`, and the form markers that keep the Hide
    removed state), so the count is taken under all of them too. Measured on
    production 2026-09-01: the chip said "Galicia · costa 543" while its link
    opened 478 (the 65 criteria-hidden rows) -- #518's overstatement, on the
    page everything was fixed for -- and the mirror was one filter away: a
    chip counting the bare subscription over a page with `search=` typed
    promises more than clicking it shows. So the count follows the current
    criteria mode rather than the default reading (under `criteria=all` the
    chip is exact at 543, under the default at 478), and it follows the
    other filters, because that is what its link keeps.

    One exception, #470's owner decision, and it is unconditional: the
    delisted rows a `hide_removed=off` page shows are never counted. The
    switch's own visible state explains that gap, and the badge answers
    "how many live listings", the way the portal's saved search would.
    `held` stays raw: it decides whether the option is offered at all, and a
    subscription whose every row the current view excludes is still one
    click away. A caller with no page of its own (`live_query` None) gets
    the standing policy: live rows under the DEFAULT criteria reading.
    """
    held = dict(
        db.session.query(Property.search_profile_id, func.count(Property.id)).group_by(
            Property.search_profile_id
        )
    )
    if live_query is None:
        live_query, _ = subscription_criteria.apply_filter(
            Property.query, _criteria_context(), ""
        )
    live = dict(
        live_query.filter(Property.listing_status.notin_(DELISTED_LISTING_STATUSES))
        .with_entities(Property.search_profile_id, func.count(Property.id))
        .group_by(Property.search_profile_id)
    )
    return {
        profile_id: ProfileListingCount(live=live.get(profile_id, 0), held=held_count)
        for profile_id, held_count in held.items()
    }


def _profile_dropdown_options(profiles, resolved, counts=None, include_hidden=False):
    """Rows for the subscription filter: the live subscriptions first, the
    retired ones after them, each with how many listings it holds.

    A *hidden* subscription (owner request, 2026-08-17) is in neither group:
    it is left out of the menu entirely, archive included, which is what
    separates hiding from retiring. The one exception is the selection --
    a hidden id named in the URL still gets its checkbox, for the same reason
    an unknown id does, and it renders under its own heading rather than
    under `Archive`, which would say something false about why it is there.

    `include_hidden` is that exception made general, for a caller whose own
    population already contains the hidden subscriptions (MUNIC-002). Hiding
    takes a subscription off the screens that *offer* it; `/municipalities`
    does not offer subscriptions, it compares municipalities over every stored
    listing, and its Scope line already counts the hidden ones. A menu that
    left them out there would disclose a population its own control could not
    reach -- which is the defect this control exists to remove, one axis
    along. It defaults to `False` so `/properties` is untouched.

    The owner has exactly two saved searches on idealista.com; everything else
    in `search_profiles` is a subscription that stopped (the Alicante ones), a
    mirror of the frozen `lands` table, or the unnamed catch-all. Listing all
    of them side by side as if they were equals is what made the old dropdown
    unreadable, so an inactive profile is still offered -- its listings are
    real and have to stay reachable -- but as an archive row, labelled and
    sorted below the live ones.

    A profile that holds nothing *and* is not selected is left out entirely:
    the catch-all `Default` exists for routing, not for filtering, and an
    option that can only ever return an empty page is noise.

    Whatever the selection names is always included, active or not, empty or
    not. That is not cosmetic: the page's own script recomputes the state from
    the checkboxes, so a selected id with no checkbox reads as "nothing
    ticked", and the next Apply would silently widen the view.
    """
    counts = _listing_counts_by_profile() if counts is None else counts

    active_ids = {profile.id for profile in profiles}
    selected = set(resolved.checked_ids)

    # One query for the archive too -- inactive profiles are not in `profiles`,
    # which `SearchProfileService.list_visible_profiles()` filtered. Hidden
    # ones are filtered out of this query as well, and come back below only
    # when the selection names them.
    from services.search_profile_service import SearchProfileService

    known = {profile.id: profile for profile in profiles}
    archived = SearchProfile.query.filter(SearchProfile.is_active.isnot(True))
    if not include_hidden:
        archived = archived.filter(SearchProfileService.visible_clause())
    for profile in archived.all():
        known.setdefault(profile.id, profile)
    for profile in SearchProfile.query.filter(
        SearchProfile.id.in_(selected - set(known))
    ).all():
        known.setdefault(profile.id, profile)

    options = []
    for profile_id, profile in known.items():
        count = counts.get(profile_id, EMPTY_PROFILE_COUNT)
        # Offered on `held`, displayed as `live`: a subscription whose every
        # listing is delisted shows 0 and keeps its chip, because its rows
        # are still reachable with Hide removed off (#472 review finding).
        if not count.held and profile_id not in selected:
            continue
        options.append(
            {
                "id": profile_id,
                "name": profile.name,
                "is_active": profile_id in active_ids,
                "is_hidden": bool(profile.is_hidden),
                "count": count.live,
            }
        )

    # An id the selection names that no longer exists at all: it still needs a
    # checkbox, or the selection silently widens on the next Apply.
    for profile_id in selected - set(known):
        options.append(
            {
                "id": profile_id,
                "name": t("unknown_profile") % profile_id,
                "is_active": False,
                "is_hidden": False,
                "count": counts.get(profile_id, 0),
            }
        )

    # Live subscriptions first, then the archive, then whatever hidden ones
    # the selection dragged back in; alphabetical inside each group so the
    # order does not shuffle when a listing arrives.
    options.sort(
        key=lambda option: (
            option["is_hidden"],
            not option["is_active"],
            option["name"].lower(),
        )
    )
    return options


def _hidden_subscription_note(resolved, counts=None):
    """What the subscription controls are not showing, or None.

    Hiding is the owner's own choice, so this is a disclosure and not a
    warning: the menu offers no way to reach these, and a page that simply
    stopped mentioning several subscriptions would leave "5 of 14" looking
    like the whole table. It counts the listings too, because the number that
    actually moved is the row count, not the subscription count.

    A hidden subscription the selection names is on screen already, so it is
    not part of what is being withheld. `counts` is the shared group-by when
    the caller already has one -- /properties builds the menu from it.
    """
    from services.search_profile_service import SearchProfileService

    counts = _listing_counts_by_profile() if counts is None else counts
    hidden_ids = [
        profile_id
        for (profile_id,) in db.session.query(SearchProfile.id).filter(
            SearchProfileService.hidden_clause()
        )
    ]
    shown = set(resolved.checked_ids)
    withheld = [profile_id for profile_id in hidden_ids if profile_id not in shown]
    if not withheld:
        return None
    return {
        "profiles": len(withheld),
        "listings": sum(
            counts.get(profile_id, EMPTY_PROFILE_COUNT).live for profile_id in withheld
        ),
    }


def _travel_display_targets(profile_ids, include_custom=False):
    """Travel columns to render for the profiles currently on screen.

    A single profile shows everything it is configured for, presets and its
    own custom destinations. Anything wider shows the presets only, and
    `include_custom` is the caller's answer to "is this really one
    subscription?" -- one profile *plus the unassigned listings* is not, and
    a custom target id belongs to one profile, so a union would label a
    column with a destination most rows were never measured against. Presets
    are global keys with fixed labels ("nearest airport"), so they mean the
    same thing in every profile and survive the union.
    """
    from services.search_profile_service import SearchProfileService

    icon_map = {
        "airport": "fa-plane",
        "train_station": "fa-train",
        "hospital": "fa-hospital",
        "police": "fa-shield-halved",
        "supermarket": "fa-cart-shopping",
        "school": "fa-school",
    }

    profiles = []
    for profile_id in profile_ids:
        try:
            profile = db.session.get(SearchProfile, profile_id)
        except SQLAlchemyError:
            logger.warning("Could not load profile %s for travel targets", profile_id)
            profile = None
        if profile is not None:
            profiles.append(profile)
    if not profiles:
        return []

    configs = [
        SearchProfileService.get_travel_targets_config(profile) for profile in profiles
    ]

    targets = []
    for preset in SearchProfileService.get_travel_preset_defs():
        key = preset.get("key")
        if not key:
            continue
        enabled = False
        for config in configs:
            presets_cfg = config.get("presets") if isinstance(config, dict) else {}
            entry = (presets_cfg or {}).get(key) or {}
            if bool(entry.get("enabled", True)):
                enabled = True
                break
        if not enabled:
            continue
        targets.append(
            {
                "key": key,
                "label": preset.get("label") or key,
                "icon": icon_map.get(key) or "fa-route",
                "kind": "preset",
            }
        )

    if not include_custom or len(profiles) != 1:
        return targets

    config = configs[0]
    for item in (config.get("custom") or []) if isinstance(config, dict) else []:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not target_id or not name:
            continue
        targets.append(
            {
                "key": f"custom:{target_id}",
                "label": name,
                "icon": "fa-location-dot",
                "kind": "custom",
            }
        )
    return targets


def _unclassified_clause(column):
    """Rows the classifier never labelled: NULL or an empty string."""
    return or_(column.is_(None), column == "")


def _visible_distinct_values(column, profile_selection, extra_filter=None):
    """Distinct non-empty values of `column` within the subscriptions shown."""
    query = apply_profile_filter(
        db.session.query(column).distinct(),
        Property.search_profile_id,
        profile_selection,
    )
    if extra_filter is not None:
        query = query.filter(extra_filter)
    return sorted({row[0] for row in query.all() if row and row[0]})


def _selection_has_unclassified(column, profile_selection, extra_filter=None):
    """Whether the subscriptions shown hold a row with no value in `column`."""
    query = apply_profile_filter(
        db.session.query(Property.id),
        Property.search_profile_id,
        profile_selection,
    ).filter(_unclassified_clause(column))
    if extra_filter is not None:
        query = query.filter(extra_filter)
    return bool(db.session.query(query.exists()).scalar())


def _keep_applied_choice(values, applied):
    """Keep an applied filter in its dropdown even when the selection lost it.

    Switching subscriptions with a filter on would otherwise leave the select
    reading "All types" over a page that is still filtered -- the control
    would disagree with the query it produced.
    """
    if applied and applied != UNCLASSIFIED_FILTER and applied not in values:
        values.append(applied)
        values.sort()
    return values


def _municipality_choices(base_query, applied=""):
    """One dropdown option per real municipality, however the rows spell it.

    `properties.municipality` is free text and the same place arrives under
    several spellings, so a dropdown built from the raw distinct values
    offered "Gijón" and "Gijon" separately: picking one showed 57 of 73
    listings and said nothing about the rest. The options are grouped by
    `utils.municipality_grouping` -- one entry per municipality, the most
    readable spelling as its label, and the *combined* count beside it.

    Two rules survive from issue #298 and are the grouping module's now: a
    truncated artifact ("Ovi...") has no group and is therefore not offered
    as a municipality of its own, and one that is already applied is put back
    below, so a hand-typed URL keeps agreeing with its dropdown.

    The count beside a name is the size of the page that picking the name
    opens: `base_query` is the page's own narrowing with the municipality
    dimension left open (`_apply_filter_bar(..., skip="municipality")`),
    under the subscriptions on screen and the current criteria mode. #518's
    Cedeira, alive on the page everything was fixed for -- "(24)" over a page
    finding 17 -- is what this closes, and a municipality whose every row the
    current view excludes is not offered, because under that view the option
    can only ever return an empty page.
    """
    rows = (
        base_query.with_entities(Property.municipality, func.count(Property.id))
        .group_by(Property.municipality)
        .all()
    )

    applied_key = group_key(applied)
    choices = [
        {
            "value": group.label,
            "label": group.label,
            "count": group.count,
            "selected": applied_key is not None and group.key == applied_key,
        }
        for group in group_municipalities(rows)
    ]
    if (
        applied
        and applied != UNCLASSIFIED_FILTER
        and not any(choice["selected"] for choice in choices)
    ):
        # Either a value the selection does not hold (switching subscriptions
        # with a filter on) or one that has no group at all (a truncated
        # artifact, a hand-typed prefix). Both must stay on screen: the
        # control has to agree with the query it produced. No count -- this
        # entry is outside the grouping the others were counted by.
        choices.append(
            {"value": applied, "label": applied, "count": None, "selected": True}
        )
        choices.sort(key=lambda choice: choice["label"])
    return choices


def _source_choices(base_query, applied=""):
    """Which sites the subscriptions on screen actually hold listings from.

    Built from the same selection as every other dropdown, for the same
    reason: a source holding nothing in view is an option that can only ever
    return an empty page.

    Counted by reading the URLs and applying `source_of_url` to each, rather
    than by grouping in SQL. Deliberate: the badge on the row, the filter
    clause and this count then all derive the source from one function, and a
    second reading written in SQL is how a filter comes to disagree with the
    badge beside it. The column is short and the selection is at most the
    whole table -- 730 rows on 2026-08-17. The rows the walk sees are the
    page's own, with the source dimension left open, so the count is the
    page picking that source opens: Idealista offered "(318)" over a page
    finding 263 before it did.
    """
    from utils.listing_source import SOURCES, source_label, source_of_url

    query = base_query.with_entities(Property.url)
    counts = {}
    for (url,) in query.all():
        key = source_of_url(url)
        counts[key] = counts.get(key, 0) + 1

    choices = [
        {"value": source, "label": source_label(source), "count": counts[source]}
        for source in SOURCES
        if counts.get(source)
    ]
    # An applied filter stays offered even when it now matches nothing, so the
    # control that produced the empty page can still be undone -- the rule
    # `_keep_applied_choice` already follows for the other dropdowns.
    if applied and applied not in {choice["value"] for choice in choices}:
        choices.append({"value": applied, "label": source_label(applied), "count": 0})
    return choices


def _advertiser_choices(base_query, applied=""):
    """How many of the listings on screen are sold by whom.

    Counted in SQL, unlike `_source_choices` above, and the difference is
    `enrichment`: the source reading needs `url`, which is short, while this
    one also needs a JSON column holding every measurement a row carries.
    Pulling that into Python for a count would be the most expensive query on
    the page. `services/advertiser.state_expression` is the SQL half of the
    same reading the badge uses, and `tests/test_advertiser.py` runs one matrix
    through both halves precisely because they are two pieces of code that must
    not drift.

    Every state that exists in the selection is offered, `unchecked`
    included -- the list badges only `owner`, so this dropdown is where the
    number of rows nobody could answer for is disclosed. Counted over the
    page's own narrowing with the seller dimension left open, so the number
    is the page picking that seller opens: "Not established" offered 430
    over a page finding 376 before it did.
    """
    state = advertiser.state_expression(Property)
    rows = base_query.with_entities(state, func.count()).group_by(state)
    counts = {key: total for key, total in rows.all() if key}

    choices = advertiser.options(counts)
    # An applied filter stays offered even when it now matches nothing, so the
    # control that produced the empty page can still be undone.
    if applied and applied not in {choice["value"] for choice in choices}:
        choices.append(
            {"value": applied, "label": advertiser.label(applied), "count": 0}
        )
    return choices


def _owner_verdict_choices(base_query, applied=""):
    """How many listings the owner decided what about.

    `undecided` is offered whenever it holds anything, and on this table it
    holds nearly everything. That is the point rather than noise: without it
    the three decided counts read as a tally of the whole page, and "nobody
    decided yet" is exactly the fact #98 says must not be folded into
    "rejected".

    Counted over the page's own narrowing with the decision dimension left
    open, under the current criteria mode. Under the default reading only
    `undecided` can move -- a decided listing is exempt from the hide by the
    same rule that makes it decided -- and it is exactly the count that
    moved: "Not decided yet (453)" over a page finding 391, the 62
    criteria-hidden rows entire (production 2026-09-01).
    """
    state = owner_review.decision_expression(Property)
    rows = base_query.with_entities(state, func.count()).group_by(state)
    counts = {key: total for key, total in rows.all() if key}

    choices = owner_review.decision_options(counts)
    if applied and applied not in {choice["value"] for choice in choices}:
        choices.append(
            {
                "value": applied,
                "label_key": owner_review.decision_label_key(applied),
                "count": 0,
            }
        )
    return choices


def _next_action_choices(base_query, applied="", on_date=None):
    """How many listings carry an outstanding action, and how many are late.

    `none` is not offered: it is most of the table, and an option that selects
    everything selects nothing anyone is looking for. The date is the caller's
    -- the same one the filter and the badges use, so the count cannot describe
    a different day from the rows under it.

    Counted over the page's own narrowing with the action dimension left
    open, like the other four. Under the DEFAULT reading that narrowing is a
    no-op here -- an open action is one of the hide's own exemptions
    (services/subscription_criteria.hidden_by_default_expression) -- but the
    counts follow the current mode, and under `criteria=pass` an actioned
    fail is not on the page its option opens.
    """
    state = owner_review.action_expression_portable(Property, on_date)
    rows = base_query.with_entities(state, func.count()).group_by(state)
    counts = {key: total for key, total in rows.all() if key}

    choices = owner_review.action_options(counts)
    if applied and applied not in {choice["value"] for choice in choices}:
        choices.append(
            {
                "value": applied,
                "label_key": owner_review.action_label_key(applied),
                "count": 0,
            }
        )
    return choices


def _property_filter_options(
    profile_selection,
    scope_query,
    bar,
    review_today,
    criteria_ctx,
    criteria_filter,
    similarity_ctx=None,
):
    """Type / Subtype / Municipality choices for the subscriptions on screen.

    These dropdowns used to be built from the whole `properties` table, so a
    saved search for land offered `apartment` and `developed` as well --
    values owned by other subscriptions, which can only ever return an empty
    page here. They now come from the same selection the listing is filtered
    by, and the subtypes narrow again to the chosen category.

    "Unclassified" is offered the way the "No subscription" box is: only when
    such rows exist in the selection, or when the filter is already on it.
    Ingestion can still produce a listing no classification rule matched, and
    hiding the only way to find those would trade dead UI for lost rows.

    Every counted dropdown (municipality, source, advertiser, verdict,
    action) is counted over `landing(dimension)`: `scope_query` -- the
    toolbar scope, Favorites and Hide removed as the page has them -- through
    `_apply_filter_bar` with that one dimension left open, under the
    subscriptions on screen and the current criteria mode. That is exactly
    the page an option opens, because the filter form carries every other
    field, the subscription checkboxes and the criteria select with it. The
    uncounted ones (category, subtype) offer values without numbers and are
    left alone.
    """
    category_filter = bar.get("category") or ""
    subtype_filter = bar.get("subtype") or ""

    def landing(dimension):
        narrowed = _apply_filter_bar(
            scope_query,
            bar,
            review_today,
            skip=dimension,
            similarity_ctx=similarity_ctx,
        )
        narrowed = apply_profile_filter(
            narrowed, Property.search_profile_id, profile_selection
        )
        narrowed, _ = subscription_criteria.apply_filter(
            narrowed, criteria_ctx, criteria_filter
        )
        return narrowed

    if category_filter == UNCLASSIFIED_FILTER:
        category_clause = _unclassified_clause(Property.property_category)
    elif category_filter:
        category_clause = Property.property_category == category_filter
    else:
        category_clause = None

    return {
        "categories": _keep_applied_choice(
            _visible_distinct_values(Property.property_category, profile_selection),
            category_filter,
        ),
        "subtypes": _keep_applied_choice(
            _visible_distinct_values(
                Property.property_subtype, profile_selection, category_clause
            ),
            subtype_filter,
        ),
        "municipalities": _municipality_choices(
            landing("municipality"), bar.get("municipality") or ""
        ),
        "sources": _source_choices(landing("source"), bar.get("source") or ""),
        "advertisers": _advertiser_choices(
            landing("advertiser"), bar.get("advertiser") or ""
        ),
        "verdicts": _owner_verdict_choices(
            landing("verdict"), bar.get("verdict") or ""
        ),
        "actions": _next_action_choices(
            landing("action"), bar.get("action") or "", review_today
        ),
        "has_unclassified_category": _selection_has_unclassified(
            Property.property_category, profile_selection
        ),
        "has_unclassified_subtype": _selection_has_unclassified(
            Property.property_subtype, profile_selection, category_clause
        ),
    }


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometers."""
    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return r * c


@main_bp.route("/")
def index():
    """Home page redirects to the properties listing (the working UI).

    Owner decision 2026-08-08 (issue #105), superseding the 2026-08-07 one
    that pointed here at /lands: the `lands` table stopped growing on
    2026-02-18, every fresh listing is ingested into `properties`, and the
    legacy `Land` model cannot represent the houses that now arrive.
    """
    return redirect(url_for("main.properties"))


@main_bp.route("/properties")
def properties():
    """Properties listing -- the working page since issue #105."""
    try:
        from services.search_profile_service import SearchProfileService

        # Default first, so a fresh install's auto-created profile is in the
        # dropdown that "all profiles" is defined against.
        SearchProfileService.get_default_profile(create=True)
        # Visible, not merely active: hiding a subscription takes its listings
        # out of `profile_id=all` along with its chip, which is the whole
        # difference between hiding one and retiring one (2026-08-17).
        profiles = SearchProfileService.list_visible_profiles(active_only=True)

        # `profile_id` is a repeated parameter since #104 -- auto | all |
        # selected(ids); see services/profile_selection.py for what each
        # state means and why "all" is never inferred from an empty tick list.
        #
        # A bare /properties means every live subscription (owner decision
        # 2026-08-09). It used to resolve to the single richest profile, which
        # made the one surface open on one saved search and hide the other --
        # the whole point of the page is that all of them share it.
        selection = parse_profile_selection(request.args)
        if selection.is_auto:
            selection = ProfileSelection(ProfileSelectionState.ALL)
        profile_selection = resolve_profile_selection(
            selection, [profile.id for profile in profiles]
        )
        # Profile-specific data (custom travel targets, the recalculate
        # actions) is only meaningful for exactly one profile.
        selected_profile_id = profile_selection.single_id

        # Filters
        category_filter = request.args.get("category", "")
        subtype_filter = request.args.get("subtype", "")
        municipality_filter = request.args.get("municipality", "")
        source_filter = request.args.get("source", "")
        advertiser_filter = request.args.get("advertiser", "")
        verdict_filter = request.args.get("verdict", "")
        action_filter = request.args.get("action", "")
        # One date for the whole request. `overdue` is a due date compared
        # against today, and the badge, the filter, the count beside its option
        # and both serializers have to compare against the *same* today or they
        # disagree for the few minutes a day nobody is watching
        # (services/owner_review.py).
        review_today = owner_review.today()
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        favorites_filter = request.args.get("favorites", "") == "on"
        sea_view_filter = request.args.get("sea_view", "")
        sea_distance_filter = request.args.get("sea_dist", "")
        build_filter = request.args.get("build", "")
        measured_filter = request.args.get("measured", "")
        # The least likeness to the subscription's favorites a row needs
        # (services/favorite_similarity.py). Kept as typed: an unknown value
        # narrows nothing and the control re-renders on its default.
        similar_filter = request.args.get("similar", "")
        # The filter bar as one mapping, for `_apply_filter_bar`: the page's
        # rows and every count beside its controls run the same function.
        bar = {
            "category": category_filter,
            "subtype": subtype_filter,
            "municipality": municipality_filter,
            "source": source_filter,
            "advertiser": advertiser_filter,
            "verdict": verdict_filter,
            "action": action_filter,
            "search": search_query,
            "inv_metr": investment_metrics_filter,
            "sea_view": sea_view_filter,
            "sea_dist": sea_distance_filter,
            "build": build_filter,
            "measured": measured_filter,
            "similar": similar_filter,
        }
        # Canonicalised at the boundary, for the reason /municipalities gives
        # (closing-audit finding 5, the LOW one): `apply_filter` normalises
        # internally, so `criteria=FAIL` APPLIED the fail mode — 62 rows on
        # production 2026-09-01 — while the template, handed the raw string,
        # matched no option and rendered the select on its default. The
        # applied mode and the rendered control read one value now, through
        # the one reading (`criteria_mode` over `read_filter_mode`), and the
        # links rebuilt from `current_filters` carry the canonical spelling.
        criteria_filter = criteria_mode(request.args.get("criteria", ""))

        # Hide removed: ON by default (similar to /lands), unless this request
        # came from the filter form with the box unticked.
        # utils/listing_status_scope.py owns that reading -- it used to be a
        # hand-written list of filter parameter names here and a second,
        # differently stale one in export_properties_csv() below.
        hide_removed_filter = resolve_hide_removed(request.args)

        # View state carried over from /lands (issue #105): cards vs table,
        # and the combined / investment / lifestyle scoring modes.
        mode = request.args.get("mode", "combined")
        if mode not in PROPERTY_MODE_SORT_DEFAULTS:
            mode = "combined"
        view_type = request.args.get("view_type", DEFAULT_PROPERTY_VIEW_TYPE)
        if view_type not in PROPERTY_VIEW_TYPES:
            view_type = DEFAULT_PROPERTY_VIEW_TYPE

        # Sorting. Picking a mode switches to that mode's score; a bare
        # /properties keeps its date order so the newest listings stay on top.
        similarity_cut = favorite_similarity.read_filter_cut(similar_filter)
        if request.args.get("mode"):
            default_sort = PROPERTY_MODE_SORT_DEFAULTS[mode]
        elif similarity_cut is not None:
            # A known similarity cut asks for the most alike rows, so with
            # neither a sort nor a mode named that is the order: most alike
            # first, in the units the control itself speaks. A chosen mode
            # stays the owner's, exactly as the page script leaves a chosen
            # sort alone. The filter form always submits its sort select, so
            # this decides hand-typed and cross-page links -- the map's List
            # View link carries the cut and no sort.
            default_sort = "similarity"
        else:
            default_sort = DEFAULT_PROPERTY_SORT
        sort_by = request.args.get("sort") or default_sort
        sort_order = request.args.get("order", "desc")

        # Pagination
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = request.args.get("per_page", 25, type=int)
        per_page = min(max(per_page, 10), 100)

        # The subscription filter is applied *last* (below, after every other
        # filter), so `unassigned_count` can branch off the same base query.
        # See the comment there for why the order matters.
        query = Property.query

        # Toolbar scope first -- Favorites and Hide removed are the switches on
        # the subscription row, not filter-bar fields -- so `filter_bar_scope`
        # below is the set the filter bar is about to narrow. The disclosure
        # beside the result count is diffed against it: the chips carry every
        # filter in their links (the page's own link contract, see `base_args`),
        # which is how "All subscriptions" came to read "23 properties found"
        # under a sticky sea-view filter with nothing on the page saying the
        # other 488 were filtered out, not missing. SQLAlchemy queries are
        # immutable, so branching here cannot drift from the filtered chain.
        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )

        filter_bar_scope = query

        # The filter bar, through the one function the counts beside its
        # controls also run (`_apply_filter_bar`): a counted option is the
        # size of the page picking it opens, and that holds only while the
        # page and the count narrow through the same code.
        # The likeness to the subscription's favorites, read ONCE for the
        # request (services/favorite_similarity.py). Under a cut or a
        # similarity sort it covers EVERY favorite-holding subscription: the
        # chip counts and the hidden-subscription note run this clause with
        # only the subscription left open, and a hidden subscription's rows
        # nobody scored would count as 0 while naming it opens them. On an
        # ordinary page it covers the subscriptions on screen -- live and
        # archived, since both carry a count in the menu -- and whatever the
        # URL selected by name, which is what the chip beside every score
        # and the line beside the count need. One object for the page's
        # rows, the counts beside its controls, the sort key and the chips.
        similarity_ctx = favorite_similarity.build_context(
            profile_ids=(
                None
                if similarity_cut is not None
                or request.args.get("sort") == "similarity"
                else _similarity_scope(profile_selection)
            )
        )
        query = _apply_filter_bar(
            query, bar, review_today, similarity_ctx=similarity_ctx
        )

        # Whether the filter bar narrowed anything. Object identity is the
        # honest reading: every applied clause produced a new query object,
        # and every helper above hands back the *same* object when its value
        # is empty or unknown -- so `sea_view=banana` does not count as a
        # filter, and the disclosure cannot describe a narrowing that was
        # never applied.
        filter_bar_active = query is not filter_bar_scope

        # Listings with no subscription at all (issue #111). They are invisible
        # to every profile selection including `all`, so the page has to say
        # whether its total covers them -- and the number it discloses has to
        # be the number the "show them" link lands on. That is why this counts
        # off the query with every *other* filter already applied and the
        # subscription filter not yet: SQLAlchemy queries are immutable, so the
        # two branches cannot drift the way a second hand-written filter chain
        # would.
        #
        # This is the page's only unassigned count: the "No subscription" entry
        # in the subscription dropdown (#104/#112) reads the same number, so
        # the option, the disclosure next to the total and the page the link
        # lands on cannot state three different figures under one filter.
        unassigned_count = query.filter(Property.search_profile_id.is_(None)).count()

        # The chips' numbers, taken over THIS page's own narrowing -- every
        # filter above and the criteria mode -- with only the subscription
        # left open: the set each chip's href opens, since the href carries
        # `base_args`. See `_listing_counts_by_profile` for the measurements
        # and for #470's one exception. One group-by, shared with the menu's
        # per-subscription counts and the hidden-subscription note.
        criteria_ctx = _criteria_context()
        chip_scope, _ = subscription_criteria.apply_filter(
            query, criteria_ctx, criteria_filter
        )
        listing_counts = _listing_counts_by_profile(live_query=chip_scope)
        query = apply_profile_filter(
            query, Property.search_profile_id, profile_selection
        )

        # The subscription's own criteria (#498 follow-up): by default a
        # measured fail the owner has not judged is hidden, and the count of
        # what was hidden renders beside the result count — a filter that
        # hides silently reads as "these listings do not exist". Applied
        # AFTER the narrowing check (the default hide is the page's standing
        # policy, not a filter-bar action) and AFTER the profile filter, so
        # the disclosure counts what was hidden FROM THIS SELECTION — a
        # count over other subscriptions' hidden rows would be a number
        # about a different page (the review's finding 6).
        query, criteria_hidden_count = subscription_criteria.apply_filter(
            query, criteria_ctx, criteria_filter, count_hidden=True
        )

        # What the same subscription selection holds *without* the filter bar
        # -- the number the "clear filters" link lands on, so the count line
        # can say "23 of 511" instead of presenting a narrowed set as the
        # whole one (#98's shape, at the level of the page's own total).
        # Counted only when a filter really applied: the extra COUNT is not
        # spent on the common unfiltered page.
        filter_bar_scope_total = None
        # "N of M shown" is a SUBSET claim: the N rows on screen are part of
        # the M the clear link lands on. That link resets `criteria` to its
        # default, so M is counted under the default reading — and the claim
        # holds only while every row on screen would survive that default.
        # `pass` and `unknown` rows do, so those modes keep the line and it
        # is true. `fail` rows do not, and `all` puts them on screen beside
        # the rest, so under those two the sets are not nested: measured on
        # production, `?criteria=fail&search=casa` said "54 of 377" while
        # none of the 54 was among the 377. A subset claim about sets that
        # are not nested is not a disclosure, it is a wrong number, so the
        # line stands down there — the criteria dropdown already shows its
        # own state, and the Clear control sits beside it.
        #
        # (A sibling session removed the mirror case, N > M under
        # `criteria=all`, in the template, and left this half to the author
        # on the grounds that it is a design decision rather than a review
        # fix. It is: `criteria` stays a filter the clear link resets, and
        # the note stands down when it cannot back its own arithmetic.)
        #
        # The question is asked of the ROWS and never of the parameter's
        # spelling. The first version of this guard tested `criteria` against
        # {"all", "fail"} and was wrong in both directions on production: a
        # subscription that carries NO criteria selects the default's own rows
        # under `criteria=all`, so a true sentence was silenced — and its
        # clear-filters link went with it, because the link lives inside the
        # note's own span. Conversely `criteria=fail` over a scope whose fails
        # the owner has all judged shows rows the default keeps, where the
        # sentence is true and the string test refuses it. Both are one
        # measurement: does anything on screen fall outside what the clear
        # link restores?
        if filter_bar_active and not _shows_rows_the_default_hides(query, criteria_ctx):
            scope_query = apply_profile_filter(
                filter_bar_scope, Property.search_profile_id, profile_selection
            )
            scope_query, _ = subscription_criteria.apply_filter(
                scope_query, criteria_ctx, ""
            )
            filter_bar_scope_total = scope_query.count()

        # How much of what the page is about to draw was ever verified against
        # idealista. `listing_status` is 'active' by default and nothing
        # verified that default, so a list of rows carrying it is not a list of
        # live listings -- and the reader cannot tell by looking, because a
        # never-checked row and a checked-yesterday row rendered identically.
        # Counted over the whole filtered result rather than the current page:
        # the total beside it is the filtered total too, and two numbers on one
        # line have to be about the same set of rows. The predicate comes from
        # the module the badges read, so the header and the ticks under it
        # cannot disagree (services/listing_verification.py).
        listing_verified_count = query.filter(verified_expression(Property)).count()

        # And the same disclosure for the hazard scan (#437). The badge is
        # drawn only for a row where something qualifies, so "no badge" covers
        # both "scanned, nothing there" and "nobody looked" -- and the second
        # of those is what this line exists to make visible. Same filtered
        # set, same predicate the badge reads.
        hazard_scanned_count = query.filter(
            hazard_complete_expression(Property)
        ).count()

        # And for the score itself (#493). The number this page sorts by is a
        # weighted average over the criteria that answered, renormalised past
        # the ones that did not (#379) -- so it stays honest per row and says
        # nothing about the *set*. Measured on production 2026-08-26, 678 of
        # 948 located rows carry `travel: approximate_origin` and 628 carry
        # `sea: approximate_origin`: for roughly 70% of them the drive times
        # and the sea distance are measured, stored, rendered, and scored by
        # nothing, leaving `value` + `size` carrying `score_total` alone.
        # Every abstention is right on its own terms; what was missing is that
        # a 0-100 silently meaning one thing here and another thing there
        # looks like a composite ranking while being a single-axis one.
        #
        # So the same disclosure the two lines above make, from the predicate
        # `measured=full` already filters on -- header and filter cannot
        # disagree. A row whose share was never recorded is NOT counted:
        # `score_coverage_share_expr` is NULL there, "unknown coverage must
        # not pass as full" is that helper's own rule, and counting it would
        # be #98 inside the line that exists to prevent #98.
        score_full_basis_count = query.filter(
            score_coverage_share_expr(Property) >= 0.999
        ).count()

        # And for the taste ranking (#498): the version once per request, so
        # every row is judged against the same profile, and the disclosure
        # count beside the result count -- "K of N scored against the current
        # profile" -- from the same predicate the sort reads, so the header
        # and the ordering cannot disagree. None means no profile exists yet
        # and the taste surfaces stay dormant.
        taste_version = taste_service.current_profile_version()
        taste_scored_count = (
            query.filter(
                taste_service.scored_current_expression(Property, taste_version)
            ).count()
            if taste_version is not None
            else None
        )

        # And for the similarity cut: how many rows of THIS page's set the cut
        # counts as similar, references aside, by the same predicate the
        # filter applied -- so the line beside the count and the rows under
        # it are one reading. None without a cut, and the line stays off.
        similar_count = favorite_similarity.similar_count(
            query, Property, similarity_ctx, similar_filter
        )
        # Under the Favorites switch the similar rows -- never favorites, by
        # definition -- cannot be on the page, and "Similar: 0 at >= 70"
        # would read as "nothing resembles them". So there the count is taken
        # with the switch lifted, through the same chain, and the line says
        # the switch is what hides them. One extra COUNT, only on that page.
        similar_hidden_by_favorites = False
        if similar_count is not None and favorites_filter:
            unswitched = Property.query
            if hide_removed_filter:
                unswitched = unswitched.filter(
                    Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
                )
            unswitched = _apply_filter_bar(
                unswitched, bar, review_today, similarity_ctx=similarity_ctx
            )
            unswitched = apply_profile_filter(
                unswitched, Property.search_profile_id, profile_selection
            )
            unswitched, _ = subscription_criteria.apply_filter(
                unswitched, criteria_ctx, criteria_filter
            )
            similar_count = favorite_similarity.similar_count(
                unswitched, Property, similarity_ctx, similar_filter
            )
            similar_hidden_by_favorites = True
        # What the rows of this page's set read as, counted by state -- the
        # line beside the count has to say what a missing chip means here
        # (cannot be placed, a different kind, no favorite to compare to),
        # and how many of the rankable rest on price, area and location
        # alone. One query for the ids; the reading is already in hand.
        similarity_summary = (
            similarity_ctx.summarize(
                row_id for (row_id,) in query.with_entities(Property.id)
            )
            if similarity_ctx is not None
            else None
        )

        # Sorting (safe allow-list). An unknown sort -- an old /lands bookmark
        # asking for travel_time_nearest_beach, say -- falls back to the
        # default *and says so*, so the page never claims an order it did not
        # apply.
        sort_columns = {
            "title": Property.title,
            "created_at": Property.created_at,
            "price": Property.price,
            "area": Property.area,
            "score_total": Property.score_total,
            "score_investment": Property.score_investment,
            "score_lifestyle": Property.score_lifestyle,
            "travel_time_nearest_beach": _nearest_beach_minutes(Property),
            # NULL for a row whose score is stale (older profile version) or
            # missing, so both sort last in either direction -- a v1 score
            # must not rank interleaved with v3 ones (issue #498).
            "taste_score": taste_service.sortable_score_expression(
                Property, taste_version
            ),
            # Likeness to the subscription's favorites: the references first,
            # then the rankable rows by score; a row that cannot be placed, a
            # different kind of listing and a row with no favorite to compare
            # against are NULL and sort last in both directions.
            "similarity": favorite_similarity.sort_expression(Property, similarity_ctx),
        }
        if sort_by not in sort_columns and sort_by != "investment_metrics":
            sort_by = default_sort

        if sort_by == "investment_metrics":
            rank = investment_rating_rank(Property)
            rank_order = rank.asc() if sort_order == "asc" else rank.desc()
            query = query.order_by(
                rank_order.nullslast(),
                Property.score_total.desc().nullslast(),
                Property.id.asc(),
            )
        else:
            sort_column = sort_columns[sort_by]
            if sort_order == "asc":
                query = query.order_by(sort_column.asc().nullslast(), Property.id.asc())
            else:
                query = query.order_by(
                    sort_column.desc().nullslast(), Property.id.asc()
                )

        # Derive the highlighted mode from the sort actually applied, the same
        # way /lands does, so the buttons cannot disagree with the ordering.
        if sort_by == "score_investment":
            active_mode = "investment"
        elif sort_by == "score_lifestyle":
            active_mode = "lifestyle"
        elif sort_by == "score_total":
            active_mode = "combined"
        elif sort_by == "taste_score":
            active_mode = "taste"
        else:
            active_mode = mode

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # A page past the end renders the last one instead of an empty table
        # under a correct "of N results" count. Reachable from the page-size
        # control (which used to carry `page` across) and from any stale link.
        if pagination.pages and page > pagination.pages:
            page = pagination.pages
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Nothing on screen: the two things the page used to get wrong about
        # its own emptiness. Both ask the table rather than the parameters,
        # and both run only here, where an extra query costs nothing.
        search_interpretation = interpret_search(search_query)
        search_reveal = _search_reveal_notice(
            search_interpretation, pagination.total, profile_selection
        )
        empty_state_scope_total = None
        empty_state_clear_url = None
        if not pagination.total:
            empty_state_scope_total, empty_state_clear_url = _empty_state_scope(
                profile_selection, criteria_ctx
            )

        # Travel columns for whatever subscriptions are on screen: presets for
        # all of them, plus the custom destinations when exactly one is shown.
        shown_profile_ids = (
            profile_selection.filter_ids
            if profile_selection.filter_ids is not None
            else [profile.id for profile in profiles]
        )
        travel_display_targets = _travel_display_targets(
            shown_profile_ids, include_custom=selected_profile_id is not None
        )

        # Which subscription a row belongs to, for the badge the list grows
        # when it is showing more than one of them.
        profile_names = {
            profile_id: name
            for profile_id, name in db.session.query(
                SearchProfile.id, SearchProfile.name
            )
        }

        # Dropdown choices for the subscriptions actually on screen -- see
        # `_property_filter_options` for why they are not global lists.
        filter_options = _property_filter_options(
            profile_selection,
            filter_bar_scope,
            bar,
            review_today,
            criteria_ctx,
            criteria_filter,
            similarity_ctx=similarity_ctx,
        )

        # The similarity control is offered only while a subscription on
        # screen holds a favorite -- with none there is nothing to be similar
        # to -- and the disclosure line says how many favorites the rows were
        # measured against. Both over the subscriptions actually shown.
        similarity_reference_count = (
            sum(
                similarity_ctx.reference_count_for(profile_id)
                for profile_id in shown_profile_ids
            )
            if similarity_ctx is not None
            else 0
        )

        return render_template(
            "properties.html",
            properties=pagination.items,
            pagination=pagination,
            profiles=profiles,
            profile_options=_profile_dropdown_options(
                profiles, profile_selection, counts=listing_counts
            ),
            hidden_subscription_note=_hidden_subscription_note(
                profile_selection, counts=listing_counts
            ),
            profile_names=profile_names,
            unassigned_count=unassigned_count,
            # None when the filter bar narrowed nothing -- the disclosure
            # beside the count renders only on a real narrowing.
            filter_bar_scope_total=filter_bar_scope_total,
            max_selected_profiles=MAX_SELECTED_PROFILE_IDS,
            selected_profile_id=selected_profile_id,
            profile_selection=profile_selection,
            travel_display_targets=travel_display_targets,
            listing_verified_count=listing_verified_count,
            hazard_scanned_count=hazard_scanned_count,
            score_full_basis_count=score_full_basis_count,
            taste_version=taste_version,
            taste_scored_count=taste_scored_count,
            # The request's one similarity reading, for the chip beside every
            # score; the control, only where a favorite exists to compare
            # against; and the cut's own disclosure numbers.
            similarity_ctx=similarity_ctx,
            # Offered where a favorite exists to compare against, and kept
            # on screen while a recognised cut is applied even with none,
            # so the control that produced an empty page can undo itself
            # (the `_keep_applied_choice` rule).
            similarity_enabled=similarity_reference_count > 0
            or similarity_cut is not None,
            similarity_reference_count=similarity_reference_count,
            similarity_cut=similarity_cut,
            similar_count=similar_count,
            similarity_summary=similarity_summary,
            # The clear-filters link, built from the record of what the
            # request carried rather than from a list of filter names -- the
            # same route's own `_empty_state_scope` precedent, because the
            # hand-written list is the repository's most frequent stale copy
            # (utils/listing_filters.py). It keeps the non-filters and the two
            # toolbar switches, and lands on page 1.
            clear_filters_url=_clear_filters_url(),
            similar_hidden_by_favorites=similar_hidden_by_favorites,
            criteria_enabled=criteria_ctx is not None,
            criteria_hidden_count=criteria_hidden_count,
            # How the search box entry was read, so an empty result can say
            # what it looked for instead of leaving "0 properties found" to
            # mean both "no such listing" and "not understood as you typed it".
            search_interpretation=search_interpretation,
            # And whether the listing it names is really absent or merely
            # filtered off this page -- two facts the one sentence claimed as
            # one. None when there is nothing to explain.
            search_reveal=search_reveal,
            # What clearing everything would land on, and the link that does
            # it. None unless the page is empty; 0 means the empty result is
            # genuine and no recovery is offered for it.
            empty_state_scope_total=empty_state_scope_total,
            empty_state_clear_url=empty_state_clear_url,
            # The page's one date. Every badge that asks whether an action is
            # late is handed this value; a template calling `date.today()` per
            # row would disagree with the query that selected the rows.
            review_today=review_today,
            **filter_options,
            current_filters={
                # A list, so `url_for` repeats the parameter instead of
                # stringifying it -- every in-page link is rebuilt from here.
                "profile_id": list(profile_selection.link_values),
                "category": category_filter,
                "subtype": subtype_filter,
                "municipality": municipality_filter,
                "source": source_filter,
                "advertiser": advertiser_filter,
                "verdict": verdict_filter,
                "action": action_filter,
                "search": search_query,
                "inv_metr": investment_metrics_filter,
                "sea_view": sea_view_filter,
                "sea_dist": sea_distance_filter,
                "build": build_filter,
                "measured": measured_filter,
                "similar": similar_filter,
                "criteria": criteria_filter,
                "favorites": favorites_filter,
                "hide_removed": hide_removed_filter,
                "sort_by": sort_by,
                "order": sort_order,
                "mode": mode,
                "active_mode": active_mode,
                "view_type": view_type,
                "page": page,
                "per_page": per_page,
            },
        )
    except Exception:
        logger.error("Failed to load properties page", exc_info=True)
        flash("An error occurred while loading properties. Check server logs.", "error")
        return render_template(
            "properties.html",
            properties=[],
            pagination=None,
            profiles=[],
            profile_options=[],
            # Same reason as the count below: the page failed before it could
            # ask what is hidden, so it says nothing rather than "0 hidden".
            hidden_subscription_note=None,
            profile_names={},
            # The page failed before it could count anything; claiming a number
            # here would be worse than the missing disclosure.
            unassigned_count=0,
            filter_bar_scope_total=None,
            max_selected_profiles=MAX_SELECTED_PROFILE_IDS,
            selected_profile_id=None,
            profile_selection=empty_profile_selection(),
            travel_display_targets=[],
            categories=[],
            subtypes=[],
            municipalities=[],
            has_unclassified_category=False,
            has_unclassified_subtype=False,
            current_filters={
                "mode": "combined",
                "active_mode": "combined",
                "view_type": DEFAULT_PROPERTY_VIEW_TYPE,
            },
        )


@main_bp.route("/lands")
def lands():
    """The archived lands listing folded into `/properties`.

    Owner decision 2026-08-09: one surface, not two. `/lands` used to render
    its own page over the frozen `lands` table (168 rows, nothing newer than
    2026-02-18) behind an archive banner nobody asked for. Those rows are
    mirrored into `properties` under the "Legacy Lands" subscription, so the
    working page already shows them -- selectable, like any other retired
    subscription, from the subscription filter.

    The route stays so old bookmarks and the deep links inside the legacy
    detail pages keep working; it just lands on the one surface now.
    """
    return redirect(url_for("main.properties"))


@main_bp.route("/properties/<int:property_id>")
def property_detail(property_id):
    """Detailed view of a specific property (new universal model)."""
    try:
        from config import Config
        from services.search_profile_service import SearchProfileService

        prop = db.get_or_404(Property, property_id)
        from models import PropertyAiAnalysisVariant

        openai_variant = (
            PropertyAiAnalysisVariant.query.filter_by(
                property_id=property_id, provider="openai"
            )
            .order_by(PropertyAiAnalysisVariant.created_at.desc())
            .first()
        )
        # The claude analysis itself renders from `property.ai_analysis`; the
        # variant row is fetched only for its provenance (model, date), which
        # every AI card now shows so a stale opinion reads as stale (D9).
        claude_variant = (
            PropertyAiAnalysisVariant.query.filter_by(
                property_id=property_id, provider="claude"
            )
            .order_by(PropertyAiAnalysisVariant.created_at.desc())
            .first()
        )

        # Profile-driven travel targets for consistent UI display (sidebar).
        travel_display_targets = []
        try:
            selected_profile = prop.search_profile
            travel_config = SearchProfileService.get_travel_targets_config(
                selected_profile
            )
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = (
                travel_config.get("presets") if isinstance(travel_config, dict) else {}
            )

            icon_map = {
                "airport": "fa-plane",
                "train_station": "fa-train",
                "hospital": "fa-hospital",
                "police": "fa-shield-halved",
                "supermarket": "fa-cart-shopping",
                "school": "fa-school",
            }

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = (
                    bool((presets_cfg.get(key) or {}).get("enabled", True))
                    if isinstance(presets_cfg, dict)
                    else True
                )
                if not enabled:
                    continue
                travel_display_targets.append(
                    {
                        "key": key,
                        "label": preset.get("label") or key,
                        "icon": icon_map.get(key) or "fa-route",
                        "kind": "preset",
                    }
                )

            for item in (
                (travel_config.get("custom") or [])
                if isinstance(travel_config, dict)
                else []
            ):
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {
                        "key": f"custom:{target_id}",
                        "label": name,
                        "icon": "fa-location-dot",
                        "kind": "custom",
                    }
                )
        except Exception:
            travel_display_targets = []

        shared_coordinate_ids, shared_coordinate_population = shared_coordinate_peers(
            prop
        )

        # Why the listing page may not be showing this row. Property 995 is
        # invisible on /properties, /map, the CSV and the API because it
        # misses its subscription's house bound by 13 m², and its own page
        # said nothing at all about that -- its only "criteria" was the
        # scoring-weights card, which is a different word for a different
        # thing. The verdict and the hide are both read from
        # services/subscription_criteria.py, never re-derived here, so this
        # card and the list's "N failing hidden" cannot disagree.
        criteria_bounds = subscription_criteria.read_criteria(prop.search_profile)
        criteria_reading = None
        if criteria_bounds:
            criteria_reading = {
                "bounds": criteria_bounds,
                "verdict": subscription_criteria.read_verdict(prop, criteria_bounds),
                "hidden": subscription_criteria.hidden_by_default(
                    prop, criteria_bounds
                ),
                "subscription": getattr(prop.search_profile, "name", None),
            }

        # The row's likeness to its own subscription's favorites, read
        # through the same context the list reads, built for this one
        # subscription (services/favorite_similarity.py). None when the row
        # has no subscription: there is nothing to compare against.
        similarity_ctx = (
            favorite_similarity.build_context(
                profile_ids=[prop.search_profile_id], candidate_ids=[prop.id]
            )
            if prop.search_profile_id is not None
            else None
        )
        # Always a reading: with no favorite in the subscription (or no
        # subscription at all) the card says there is nothing to compare
        # against, rather than vanishing -- an absent card reads as "not
        # similar", which is #98 by omission.
        similarity_reading = (
            similarity_ctx.read(prop.id)
            if similarity_ctx is not None
            else {
                "state": favorite_similarity.STATE_NO_REFERENCE,
                "score": None,
                "reference_count": 0,
            }
        )
        similarity_reference_count = (
            similarity_ctx.reference_count_for(prop.search_profile_id)
            if similarity_ctx is not None
            else 0
        )

        return render_template(
            "property_detail.html",
            property=prop,
            # None when the row's subscription carries no criteria: there is
            # no verdict to draw, and an empty card would invent one.
            criteria_reading=criteria_reading,
            # `no_reference` when the subscription holds no favorite: the card
            # then says so rather than drawing a number nothing was measured
            # against.
            similarity_reading=similarity_reading,
            similarity_reference_count=similarity_reference_count,
            openai_configured=bool(getattr(Config, "AI_BRIDGE_TOKEN", None)),
            openai_analysis=(openai_variant.analysis if openai_variant else None),
            openai_model=(openai_variant.model if openai_variant else None),
            # created_at is naive UTC; an offset-less ISO string is read as
            # *local* time by JS `new Date()`, shifting the provenance date
            # and the 30-day stale cutoff (diff review, 2026-08-13). The
            # explicit +00:00 makes the browser parse it as the UTC it is.
            openai_analysis_date=(
                openai_variant.created_at.replace(tzinfo=timezone.utc).isoformat()
                if openai_variant and openai_variant.created_at
                else None
            ),
            claude_model=(claude_variant.model if claude_variant else None),
            claude_analysis_date=(
                claude_variant.created_at.replace(tzinfo=timezone.utc).isoformat()
                if claude_variant and claude_variant.created_at
                else None
            ),
            travel_display_targets=travel_display_targets,
            # One row, one query, and only on this page: whether the stored
            # decision still matches the newest entry in its own log. The list
            # must never ask this -- it would be a query per row -- which is
            # why `owner_review.read_decision` stays a pure reader and this is
            # a separate call (services/owner_review.py).
            review_history_out_of_sync=owner_review.history_out_of_sync(prop),
            # The conversation, newest first. One query for the page; the list
            # never asks for this.
            activity_timeline=owner_review.timeline(prop),
            activity_channels=owner_review.CHANNELS,
            # Grouped by the entry they arrived with, in one query rather than
            # one per timeline row.
            attachments_by_entry=attachments_service.for_property(prop),
            sea_view_verdict=sea_view_service.read_verdict(prop),
            # One row, and only on this page: the list would run it per row.
            # It is evidence about the coordinate, next to the coordinate, and
            # nothing scores on it. The population travels with the ids
            # because the list is capped -- see UNIVERSE-001.
            shared_coordinate_ids=shared_coordinate_ids,
            shared_coordinate_population=shared_coordinate_population,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to load property detail %s", property_id, exc_info=True)
        flash(
            "An error occurred while loading property details. Check server logs.",
            "error",
        )
        return redirect(url_for("main.properties"))


@main_bp.route("/profiles")
def profiles():
    """List search profiles (MVP; editing comes later).

    Hidden subscriptions are listed here, and only here. This is the page
    that manages them, so a page that hid them from their own control would
    leave the owner no way back (2026-08-17).
    """
    try:
        from services.search_profile_service import SearchProfileService

        profiles = SearchProfileService.list_profiles(active_only=False)
        # Ensure at least one profile exists.
        SearchProfileService.get_default_profile(create=True)
        profiles = SearchProfileService.list_profiles(active_only=False)
        # Lightweight properties count per profile for display. The NULL bucket
        # is taken off the same group-by rather than dropped (issue #111): a
        # column of per-profile counts that silently sums to less than the
        # table is the page telling the reader something untrue.
        counts = {
            pid: cnt
            for pid, cnt in db.session.query(
                Property.search_profile_id, func.count(Property.id)
            )
            .group_by(Property.search_profile_id)
            .all()
        }
        unassigned_count = counts.pop(None, 0)
        return render_template(
            "profiles.html",
            profiles=profiles,
            property_counts=counts,
            unassigned_count=unassigned_count,
        )
    except Exception:
        logger.error("Failed to load profiles page", exc_info=True)
        flash("An error occurred while loading profiles. Check server logs.", "error")
        return render_template(
            "profiles.html", profiles=[], property_counts={}, unassigned_count=0
        )


@main_bp.route("/profiles/<int:profile_id>/visibility", methods=["POST"])
def set_profile_visibility(profile_id):
    """Take a subscription off the screens, or put it back (2026-08-17).

    Its own control, not a field in the profile editor, for the reason the
    /properties toolbar records: every control exists exactly once, and this
    one belongs where the owner is looking at all the subscriptions side by
    side and deciding which of them are worth a chip.

    The catch-all cannot be hidden. It receives every email that matches
    nothing else, so hiding it would take listings off the page as they
    arrive, with nothing on screen saying where they went -- the same reason
    `edit_profile` forces the default profile active.
    """
    profile = db.get_or_404(SearchProfile, profile_id)
    hidden = request.form.get("hidden") == "on"

    if hidden and profile.is_default:
        flash(t("profile_default_cannot_hide"), "error")
        return redirect(url_for("main.profiles"))

    profile.is_hidden = hidden
    db.session.commit()
    flash(
        t("profile_hidden_flash" if hidden else "profile_shown_flash") % profile.name,
        "success",
    )
    return redirect(url_for("main.profiles"))


@main_bp.route("/profiles/new", methods=["GET", "POST"])
def create_profile():
    """Create a new search profile."""
    from services.search_profile_service import SearchProfileService

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()
        is_active = request.form.get("is_active") == "on"
        make_default = request.form.get("is_default") == "on"

        if not name:
            flash("Name is required", "error")
            return redirect(url_for("main.create_profile"))

        if make_default and not is_active:
            # Keep defaults visible in the UI (active-only lists).
            is_active = True

        # Create with default travel targets.
        profile = SearchProfile(
            name=name[:120],
            description=description[:2000] if description else None,
            is_active=bool(is_active),
            is_default=False,
            travel_targets=SearchProfileService.get_travel_targets_config(None),
        )

        try:
            db.session.add(profile)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash(
                "An error occurred while creating profile. Check server logs.", "error"
            )
            return redirect(url_for("main.create_profile"))

        if make_default:
            try:
                SearchProfile.query.filter(SearchProfile.id != profile.id).update(
                    {SearchProfile.is_default: False}
                )
                profile.is_default = True
                profile.is_active = True
                db.session.commit()
            except Exception:
                db.session.rollback()

        flash("Profile created", "success")
        return redirect(url_for("main.edit_profile", profile_id=profile.id))

    return render_template("profile_new.html")


@main_bp.route("/profiles/<int:profile_id>/edit", methods=["GET", "POST"])
def edit_profile(profile_id):
    """Edit a search profile (MVP: travel presets + custom targets)."""
    from utils.geocoding import GeocodingService
    from services.search_profile_service import (
        SearchProfileService,
        normalize_travel_targets_config,
    )
    from services.settings_service import SettingsService

    profile = db.get_or_404(SearchProfile, profile_id)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        config = SearchProfileService.get_travel_targets_config(profile)

        if action == "save_profile_settings":
            description = (request.form.get("description") or "").strip()
            requested_active = request.form.get("is_active") == "on"
            requested_default = request.form.get("is_default") == "on"

            profile.description = description[:2000] if description else None

            if requested_default and profile.source_search_key:
                # The catch-all receives everything that matches nothing, so it
                # must not also be one specific saved search (#102). The
                # database rejects this too; refusing here turns a 500 into an
                # explanation.
                flash(
                    "This profile is tied to a specific Idealista saved search, "
                    "so it cannot be the default. The default profile is the "
                    "fallback for emails that match nothing else.",
                    "error",
                )
                requested_default = False
                requested_active = True

            if requested_default and profile.is_hidden:
                # The catch-all cannot be hidden (#533): it receives every
                # email that matches nothing else, which is why
                # `set_profile_hidden` refuses the toggle on the default. This
                # is the same pair from the other side, and the database
                # refuses it too (migration 028); refusing here turns a 500
                # into an explanation, the shape of the guard above.
                flash(t("profile_hidden_cannot_be_default"), "error")
                requested_default = False

            if requested_default:
                # Keep defaults visible in active-only UIs.
                profile.is_active = True
                SearchProfile.query.filter(SearchProfile.id != profile.id).update(
                    {SearchProfile.is_default: False}
                )
                profile.is_default = True
            else:
                # Do not allow "no default" state from the UI.
                if profile.is_default:
                    flash(
                        "At least one default profile is required. Set another profile as default first.",
                        "error",
                    )
                    profile.is_default = True
                    profile.is_active = True
                else:
                    profile.is_default = False
                    profile.is_active = bool(requested_active)

            db.session.commit()
            flash("Profile settings saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "save_presets":
            presets = dict(config.get("presets") or {})
            for preset in SearchProfileService.get_travel_preset_defs():
                key = preset.get("key")
                if not key:
                    continue
                enabled = request.form.get(f"preset_{key}") == "on"
                requested_mode = (
                    (request.form.get(f"preset_mode_{key}") or "").strip().lower()
                )
                prev = presets.get(key) if isinstance(presets.get(key), dict) else {}
                mode = requested_mode or (prev.get("mode") or "driving")
                presets[key] = {"enabled": enabled, "mode": mode}

            profile.travel_targets = normalize_travel_targets_config(
                {"presets": presets, "custom": config.get("custom") or []}
            )
            db.session.commit()
            flash("Travel presets saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "add_custom":
            name = (request.form.get("target_name") or "").strip()
            address = (request.form.get("target_address") or "").strip()
            lat_raw = (request.form.get("target_lat") or "").strip()
            lon_raw = (request.form.get("target_lon") or "").strip()
            mode = (
                request.form.get("target_mode") or "driving"
            ).strip().lower() or "driving"

            lat = None
            lon = None
            formatted_address = None

            if lat_raw and lon_raw:
                try:
                    lat = float(lat_raw)
                    lon = float(lon_raw)
                except Exception:
                    lat = None
                    lon = None

            if (lat is None or lon is None) and address:
                # One geocode, because the owner typed an address into the
                # travel-target form and pressed Save. A single unit, capped at
                # what the transport's retries can turn it into.
                with authorized_spend(
                    f"Travel target address geocoded on profile {profile_id}",
                    actor="main:edit_profile_target",
                    cap_units=CAP_INGEST_GEOCODE,
                ):
                    geo = GeocodingService().geocode_address(address)
                if geo:
                    lat = float(geo.get("lat")) if geo.get("lat") is not None else None
                    lon = float(geo.get("lng")) if geo.get("lng") is not None else None
                    formatted_address = geo.get("formatted_address")

            if not name:
                flash("Target name is required", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))
            if lat is None or lon is None:
                flash(
                    "Provide valid coordinates or an address that can be geocoded.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            custom = list(config.get("custom") or [])
            custom.append(
                {
                    "id": str(uuid.uuid4()),
                    "name": name[:120],
                    "lat": lat,
                    "lon": lon,
                    "mode": mode,
                    "address": address or None,
                    "formatted_address": formatted_address or None,
                }
            )

            profile.travel_targets = normalize_travel_targets_config(
                {"presets": config.get("presets") or {}, "custom": custom}
            )
            db.session.commit()
            flash("Custom target added", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "remove_custom":
            target_id = (request.form.get("target_id") or "").strip()
            custom = [
                t
                for t in (config.get("custom") or [])
                if str(t.get("id") or "") != target_id
            ]
            profile.travel_targets = normalize_travel_targets_config(
                {"presets": config.get("presets") or {}, "custom": custom}
            )
            db.session.commit()
            flash("Custom target removed", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "save_ai_context":
            market_context = (request.form.get("ai_market_context") or "").strip()
            ai_config = profile.ai_config if isinstance(profile.ai_config, dict) else {}
            if market_context:
                ai_config["market_context"] = market_context[:20000]
            else:
                ai_config.pop("market_context", None)

            profile.ai_config = ai_config or None
            db.session.commit()
            flash("AI context saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "save_scoring_weights":
            # The form owns a known set of numbers per category; anything else
            # in the stored config (a hand-written key, a category with no
            # scorer) is carried across untouched rather than dropped by a UI
            # that never knew about it (#239).
            from services.property_scoring_service import (
                WEIGHTLESS_SCORE_KEYS,
                PropertyScoringService,
            )

            service = PropertyScoringService()
            stored = (
                dict(profile.scoring_config)
                if isinstance(profile.scoring_config, dict)
                else {}
            )
            categories = {
                name: dict(values)
                for name, values in (stored.get("categories") or {}).items()
                if isinstance(values, dict)
            }

            rejected = []
            for category in service.known_categories():
                cat_cfg = {
                    section: dict(values)
                    for section, values in (categories.get(category) or {}).items()
                    if isinstance(values, dict)
                }
                for section, keys in service.EDITABLE_SECTIONS.items():
                    section_cfg = dict(cat_cfg.get(section) or {})
                    for key in keys:
                        field = _scoring_field_name(category, section, key)
                        raw_value = (request.form.get(field) or "").strip()
                        if not raw_value:
                            # Empty means "no override": the scorer's own
                            # default applies, and storing a copy of it would
                            # freeze today's default into the subscription.
                            section_cfg.pop(key, None)
                            continue
                        try:
                            section_cfg[key] = float(raw_value.replace(",", "."))
                        except ValueError:
                            rejected.append(
                                f"{category}.{section}.{key} = {raw_value!r}"
                            )
                    if section_cfg:
                        cat_cfg[section] = section_cfg
                    else:
                        cat_cfg.pop(section, None)
                if cat_cfg:
                    categories[category] = cat_cfg
                else:
                    categories.pop(category, None)

            if rejected:
                flash(
                    "Scoring not saved: " + "; ".join(rejected) + ". Use numbers.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            # `combined_mix` is the one section the scorer takes as a pair: it
            # applies the override only when both halves are present, and steps
            # over it otherwise. A form that saves half of it stores something
            # that looks set and does nothing (#255).
            half_filled = []
            for category, cat_cfg in categories.items():
                mix = cat_cfg.get("combined_mix") or {}
                if len(mix) == 1:
                    half_filled.append(
                        f"{category}: combined mix needs both investment and "
                        "lifestyle, or neither"
                    )
            if half_filled:
                flash("Scoring not saved: " + "; ".join(half_filled), "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if categories:
                stored["categories"] = categories
            else:
                stored.pop("categories", None)

            # The same check the JSON editor used to run, over the merged
            # result: a value the scorer cannot use never reaches the database.
            unusable = _unusable_scoring_numbers(stored)
            if unusable:
                flash(
                    "Scoring not saved: "
                    + "; ".join(unusable)
                    + ". Weights and thresholds must be numbers.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            # Turning a weightless criterion ON is the one save that
            # re-scores every listing in the subscription by design (proposal
            # D17, the agreed weight-0 shipping rule): it must not happen from
            # a save that merely *looked* like the others. The transition to a
            # positive weight therefore shows a dry-run preview first and
            # commits only on the explicit confirm below.
            #
            # It is a *set* of keys rather than `pool_score` alone since #437
            # added the second one. A criterion that ships at weight 0 and
            # rescores the table when it is raised belongs here; forgetting to
            # add it is a silent mass rescore that `git log` cannot explain,
            # which is the failure this whole path exists to prevent.
            def _weightless_criteria_enabled(cats: dict) -> set:
                """Which `(category, branch, key)` are switched on right now.

                A **set**, not a boolean. It was a boolean, and with the pool
                weight already positive the transition read `True -> True`, so
                turning the hazard criterion on skipped the preview entirely
                and re-scored the subscription on an ordinary save (codex
                review, 2026-08-20). Every level is isinstance-guarded:
                `categories` can hold a hand-written category whose branch is
                a scalar (#239 keeps unmanaged keys), and a crash here would
                take the whole save down (diff review, 2026-08-14).
                """
                enabled = set()
                if not isinstance(cats, dict):
                    return enabled
                for category, cat_cfg in cats.items():
                    if not isinstance(cat_cfg, dict):
                        continue
                    for branch in ("investment", "lifestyle"):
                        branch_cfg = cat_cfg.get(branch)
                        if not isinstance(branch_cfg, dict):
                            continue
                        for key in WEIGHTLESS_SCORE_KEYS:
                            weight = branch_cfg.get(key)
                            if (
                                isinstance(weight, (int, float))
                                and not isinstance(weight, bool)
                                and weight > 0
                            ):
                                enabled.add((category, branch, key))
                return enabled

            stored_before = (
                profile.scoring_config
                if isinstance(profile.scoring_config, dict)
                else {}
            )
            # Anything newly switched on needs the preview, whatever else was
            # already on.
            pool_turning_on = bool(
                _weightless_criteria_enabled(categories)
                - _weightless_criteria_enabled(stored_before.get("categories") or {})
            )

            if pool_turning_on:
                # Dry run: stage, rescore, measure, roll everything back.
                # Every score column is diffed, not just lifestyle: the
                # weight can be enabled on the investment branch, which moves
                # investment and the combined total while lifestyle sits
                # still — a preview reporting "0 would change" before a mass
                # rescore is worse than none (diff review, 2026-08-14).
                before = {
                    p.id: (p.score_investment, p.score_lifestyle, p.score_total)
                    for p in Property.query.filter_by(
                        search_profile_id=profile.id
                    ).all()
                }
                profile.scoring_config = stored or None
                preview_service = PropertyScoringService()
                changed = 0
                deltas = []
                for prop in Property.query.filter_by(
                    search_profile_id=profile.id
                ).all():
                    preview_service.calculate_for_property(prop, commit=False)
                    old = before.get(prop.id, (None, None, None))
                    new = (
                        prop.score_investment,
                        prop.score_lifestyle,
                        prop.score_total,
                    )
                    # A score appearing or disappearing is a change too: with
                    # the pool weight on, a listing whose other components
                    # were all unmeasured goes None → 100 (measured earlier
                    # by this very preview, which is what caught it).
                    #
                    # And the comparison is at the precision the column keeps,
                    # not a threshold over it. `abs(new - old) >= 0.05` stood
                    # here and answered *"0 of 4 listings would change"* for a
                    # save that then wrote 33.32 over 33.33 -- `score_total` is
                    # `Numeric(5, 2)`, so a cent is a real difference in the
                    # table, and a preview that under-reports is the thing this
                    # gate exists to prevent (review of #453, 2026-08-20). It
                    # did not even hold at its own boundary: on the Decimals it
                    # is handed, `float(50.05) - float(50.0)` is 0.049999999999
                    # and therefore not `>= 0.05`.
                    stored_score = PropertyScoringService.stored_score
                    row_changed = any(
                        stored_score(old_value) != stored_score(new_value)
                        for old_value, new_value in zip(old, new)
                    )
                    if row_changed:
                        changed += 1
                        # The combined total is the number the owner reads on
                        # the list, so it is what the mean shift reports.
                        if old[2] is not None and new[2] is not None:
                            deltas.append(float(new[2]) - float(old[2]))
                db.session.rollback()

                session["pending_scoring_config"] = stored
                session["pending_scoring_profile"] = profile.id
                # What the preview diffed against. A normal save between
                # preview and confirm would otherwise be silently reverted by
                # the stale snapshot (diff review, 2026-08-14).
                session["pending_scoring_baseline"] = stored_before or {}
                mean_delta = (sum(deltas) / len(deltas)) if deltas else 0.0
                flash(
                    "Scoring criterion preview: "
                    f"{changed} of {len(before)} listings would change score "
                    # Two decimals, because the count beside it is now taken
                    # at two: "4 of 4 would change (mean total shift +0.0)"
                    # reads as a contradiction, and the shift is the smaller
                    # of the two claims.
                    f"(mean total shift {mean_delta:+.2f}). Nothing is saved "
                    "yet — press «Confirm pool scoring» below to apply.",
                    "warning",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            # Staged, then rescored, then committed once. Committing the config
            # first meant a failure inside the loop left the weights stored, no
            # score recomputed, and a 500 telling the owner nothing was saved
            # (#256). The rescore reads the staged config through the session,
            # so it still sees the new weights.
            profile.scoring_config = stored or None

            # This save supersedes any pending pool preview: confirming it
            # afterwards would write the older snapshot over what was just
            # stored (diff review, 2026-08-14).
            session.pop("pending_scoring_config", None)
            session.pop("pending_scoring_profile", None)
            session.pop("pending_scoring_baseline", None)

            rescored = 0
            scoring_service = PropertyScoringService()
            for prop in Property.query.filter_by(search_profile_id=profile.id).all():
                if scoring_service.calculate_for_property(prop, commit=False):
                    rescored += 1
            db.session.commit()

            flash(
                f"Scoring saved; {rescored} listings in this subscription rescored.",
                "success",
            )
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "confirm_pool_scoring":
            from services.property_scoring_service import PropertyScoringService

            pending = session.pop("pending_scoring_config", None)
            pending_profile = session.pop("pending_scoring_profile", None)
            baseline = session.pop("pending_scoring_baseline", None)
            if not isinstance(pending, dict) or pending_profile != profile.id:
                flash(
                    "No pending pool-scoring preview for this subscription — "
                    "save the weights again to get one.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            # Belt and braces beside the pops above: if the stored config no
            # longer matches what the preview diffed against, something else
            # changed it and this snapshot would silently revert that change.
            current = (
                profile.scoring_config
                if isinstance(profile.scoring_config, dict)
                else {}
            )
            if isinstance(baseline, dict) and current != baseline:
                flash(
                    "The scoring changed since that preview — nothing applied. "
                    "Save the weights again to get a fresh preview.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            profile.scoring_config = pending or None
            rescored = 0
            scoring_service = PropertyScoringService()
            for prop in Property.query.filter_by(search_profile_id=profile.id).all():
                if scoring_service.calculate_for_property(prop, commit=False):
                    rescored += 1
            db.session.commit()
            flash(
                f"Scoring criterion enabled; {rescored} listings rescored.",
                "success",
            )
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "save_classification_rules":
            raw = (request.form.get("classification_rules_json") or "").strip()
            if not raw:
                profile.classification_rules = None
                db.session.commit()
                flash(
                    "Classification rules override cleared (global defaults apply)",
                    "success",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            import json

            try:
                parsed = json.loads(raw)
            except Exception:
                flash(
                    "Invalid JSON for classification rules. Check syntax and try again.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if not isinstance(parsed, list):
                flash("Classification rules must be a JSON array", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            from services.settings_service import _validate_classification_rule

            # A dropped entry is the interesting outcome: the owner typed it
            # and it is not stored. Count them, and say so below rather than
            # flashing an unqualified success over a partial save (#241).
            validated = []
            dropped = []
            for index, item in enumerate(parsed, start=1):
                valid = (
                    _validate_classification_rule(item)
                    if isinstance(item, dict)
                    else None
                )
                if valid:
                    validated.append(valid)
                else:
                    dropped.append(index)

            if not validated:
                flash(
                    "No valid rules found. Provide at least one rule with category/subtype/pattern/priority.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            validated.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
            profile.classification_rules = validated
            db.session.commit()
            flash(*_partial_save_message("rules", len(validated), dropped))
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == "save_email_matchers":
            raw = (request.form.get("email_matchers_json") or "").strip()
            if not raw:
                profile.email_matchers = None
                db.session.commit()
                flash(
                    "Email matchers cleared (saved-search name extraction will be used)",
                    "success",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            import json
            import re

            try:
                parsed = json.loads(raw)
            except Exception:
                flash(
                    "Invalid JSON for email matchers. Check syntax and try again.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if not isinstance(parsed, list):
                flash("Email matchers must be a JSON array", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            validated = []
            dropped = []
            for index, item in enumerate(parsed, start=1):
                if isinstance(item, str):
                    pattern = item.strip()
                elif isinstance(item, dict):
                    pattern = str(item.get("pattern") or "").strip()
                else:
                    dropped.append(index)
                    continue

                if not pattern:
                    dropped.append(index)
                    continue
                try:
                    re.compile(pattern)
                except re.error:
                    # An unusable regex here decides which saved search an
                    # unrecognised alert email is routed to; dropping it in
                    # silence sends future mail somewhere else (#241).
                    dropped.append(index)
                    continue

                if isinstance(item, str):
                    validated.append(pattern)
                    continue
                try:
                    priority = int(item.get("priority") or 0)
                except Exception:
                    priority = 0
                validated.append({"pattern": pattern, "priority": priority})

            if not validated:
                flash(
                    "No valid email matchers found. Provide regex strings or {pattern,priority} objects.",
                    "error",
                )
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            profile.email_matchers = validated
            db.session.commit()
            flash(*_partial_save_message("email matchers", len(validated), dropped))
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        flash("Unknown action", "error")
        return redirect(url_for("main.edit_profile", profile_id=profile_id))

    from services.search_profile_service import SearchProfileService

    config = SearchProfileService.get_travel_targets_config(profile)
    preset_defs = SearchProfileService.get_travel_preset_defs()
    travel_presets = []
    for preset in preset_defs:
        key = preset.get("key")
        if not key:
            continue
        enabled = bool((config.get("presets") or {}).get(key, {}).get("enabled", True))
        mode = str((config.get("presets") or {}).get(key, {}).get("mode") or "driving")
        travel_presets.append(
            {
                "key": key,
                "label": preset.get("label") or key,
                "enabled": enabled,
                "mode": mode,
            }
        )

    ai_market_context = ""
    if isinstance(getattr(profile, "ai_config", None), dict):
        ai_market_context = str(
            (profile.ai_config or {}).get("market_context") or ""
        ).strip()
    classification_rules_json = ""
    if isinstance(getattr(profile, "classification_rules", None), list):
        try:
            import json

            classification_rules_json = json.dumps(
                profile.classification_rules, indent=2, ensure_ascii=False
            )
        except Exception:
            classification_rules_json = ""

    email_matchers_json = ""
    if isinstance(getattr(profile, "email_matchers", None), list):
        try:
            import json

            email_matchers_json = json.dumps(
                profile.email_matchers, indent=2, ensure_ascii=False
            )
        except Exception:
            email_matchers_json = ""

    return render_template(
        "profile_edit.html",
        profile=profile,
        travel_presets=travel_presets,
        custom_targets=config.get("custom") or [],
        ai_market_context=ai_market_context,
        scoring_form=_scoring_form_model(profile),
        unmanaged_scoring_keys=_unmanaged_scoring_keys(profile),
        classification_rules_json=classification_rules_json,
        email_matchers_json=email_matchers_json,
        global_ai_market_context=SettingsService.get_ai_market_context(),
    )


@main_bp.route("/profiles/<int:profile_id>/travel/recalculate", methods=["POST"])
def recalculate_profile_travel(profile_id: int):
    """Recalculate travel for properties in a profile (bounded, best-effort)."""
    try:
        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 100)
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 500)

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(Property.travel.is_(None))

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_travel_service import (
            TRAVEL_STATE_UNAVAILABLE,
            PropertyTravelService,
            travel_api_state,
        )

        service = PropertyTravelService()
        updated = 0
        api_refused = 0
        not_located = 0
        # An owner-pressed recalculation over a bounded, already-selected
        # set of rows. The cap is arithmetic on that set rather than a round
        # number, so the ceiling moves with the scope the operator chose and
        # a runaway loop stops at what this many listings can honestly cost.
        with authorized_spend(
            f"Travel recalculation pressed for profile {profile.id} "
            f"({mode}, {len(properties)} listings)",
            actor="main:recalculate_profile_travel",
            cap_units=cap_for_rows(len(properties)),
        ):
            for prop in properties:
                try:
                    if service.calculate_for_property(prop, commit=True):
                        updated += 1
                    elif prop.location_lat is None or prop.location_lon is None:
                        # The run stopped before any request: geocoding could not
                        # place this listing, so there was no point to route from.
                        # Counting it as a refusal would send the operator hunting
                        # a Google outage that did not happen. (Until 2026-08-17 a
                        # *locality centroid* was counted here too; travel measures
                        # from one now, so the only row left unmeasured is one with
                        # no coordinate at all.)
                        not_located += 1
                    elif travel_api_state(prop) == TRAVEL_STATE_UNAVAILABLE:
                        api_refused += 1
                except Exception as inner:
                    logger.warning(
                        "Travel recalculation failed for property %s: %s",
                        prop.id,
                        inner,
                    )
                    db.session.rollback()
                    continue

        # A run where Google refused everything used to flash the same green
        # count as a real one (#98); the refusals get their own number now.
        summary = f"Recalculated travel for {updated} / {len(properties)} properties"
        if not_located:
            summary += (
                f"; {not_located} not measured (no coordinate — geocoding could "
                "not place the listing)"
            )
        if api_refused:
            summary += f"; {api_refused} skipped because Google was unavailable"
            logger.error(
                "Profile %s travel run: %s updated, %s refused by Google, %s total",
                profile_id,
                updated,
                api_refused,
                len(properties),
            )
        flash(summary, "warning" if (api_refused or not_located) else "success")
        return redirect(
            safe_referrer_redirect(url_for("main.properties", profile_id=profile_id))
        )
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Failed to recalculate travel for profile %s", profile_id, exc_info=True
        )
        flash(
            "An error occurred while recalculating profile travel. Check server logs.",
            "error",
        )
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


@main_bp.route("/profiles/<int:profile_id>/score/recalculate", methods=["POST"])
def recalculate_profile_scoring(profile_id: int):
    """Recalculate scoring for properties in a profile (bounded, best-effort)."""
    try:
        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 100)
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 500)

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(Property.score_total.is_(None))

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_scoring_service import PropertyScoringService

        service = PropertyScoringService()
        updated = 0
        for prop in properties:
            try:
                if service.calculate_for_property(prop, commit=True):
                    updated += 1
            except Exception as inner:
                logger.warning(
                    "Scoring recalculation failed for property %s: %s", prop.id, inner
                )
                db.session.rollback()
                continue

        flash(
            f"Recalculated scoring for {updated} / {len(properties)} properties",
            "success",
        )
        return redirect(
            safe_referrer_redirect(url_for("main.properties", profile_id=profile_id))
        )

    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Failed to recalculate scoring for profile %s", profile_id, exc_info=True
        )
        flash(
            "An error occurred while recalculating profile scoring. Check server logs.",
            "error",
        )
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


@main_bp.route(
    "/profiles/<int:profile_id>/classification/recalculate", methods=["POST"]
)
def recalculate_profile_classification(profile_id: int):
    """Re-run classification for properties in a profile (bounded, best-effort)."""
    try:
        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 200)
        except Exception:
            limit = 200
        limit = min(max(limit, 1), 1000)

        recalc_scoring = (request.form.get("recalc_scoring") or "").strip().lower() in (
            "1",
            "true",
            "on",
            "yes",
        )

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(
                or_(
                    Property.property_category.is_(None),
                    Property.property_subtype.is_(None),
                )
            )

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_classification_service import (
            PropertyClassificationService,
        )

        scoring_service = None
        if recalc_scoring:
            from services.property_scoring_service import PropertyScoringService

            scoring_service = PropertyScoringService()

        updated = 0
        changed_and_scored = 0
        for prop in properties:
            try:
                changed = PropertyClassificationService.apply_classification(
                    prop, profile
                )
                if changed:
                    updated += 1
                    if scoring_service:
                        if scoring_service.calculate_for_property(prop, commit=False):
                            changed_and_scored += 1

                db.session.commit()
            except Exception as inner:
                logger.warning(
                    "Profile classification failed for property %s: %s", prop.id, inner
                )
                db.session.rollback()
                continue

        if scoring_service:
            flash(
                f"Reclassified {updated} / {len(properties)} properties (scoring updated for {changed_and_scored})",
                "success",
            )
        else:
            flash(f"Reclassified {updated} / {len(properties)} properties", "success")
        return redirect(
            safe_referrer_redirect(url_for("main.edit_profile", profile_id=profile_id))
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to reclassify profile %s", profile_id, exc_info=True)
        flash(
            "An error occurred while reclassifying profile. Check server logs.", "error"
        )
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


# The property page's "Set status" form is gone (owner decision, 2026-08-09),
# and its POST handler went with it: leaving the endpoint behind would keep a
# state-changing route on an app that has no authentication. The listing status
# is what `check_property_status` observes on Idealista.


@main_bp.route("/lands/<int:land_id>")
def land_detail(land_id):
    """Detailed view of a specific land"""
    try:
        land = db.get_or_404(Land, land_id)

        # Backfill missing municipality from the title (common case when the email parser can't extract it).
        if not land.municipality and land.title:
            from utils.email_parser import EmailParser

            derived = EmailParser()._extract_municipality_from_title(land.title)
            if derived:
                land.municipality = derived
                db.session.commit()

        # Normalize property_details to dict format for template compatibility
        from utils.property_data import normalize_property_details

        land.property_details = normalize_property_details(land.property_details)

        # Get score breakdown from environment field
        score_breakdown = {}
        if land.environment and "scoring" in land.environment:
            score_breakdown = land.environment["scoring"]

        # Reference cities (configurable via Scoring Criteria page)
        reference_cities_for_view = []
        try:
            from services.settings_service import SettingsService

            cities = SettingsService.get_reference_cities()
            if land.location_lat and land.location_lon:
                lat = float(land.location_lat)
                lon = float(land.location_lon)

                for idx, city in enumerate(cities):
                    distance_km = round(
                        _haversine_km(lat, lon, float(city["lat"]), float(city["lon"]))
                    )
                    travel_time = (
                        land.travel_time_oviedo
                        if idx == 0
                        else land.travel_time_gijon
                        if idx == 1
                        else None
                    )
                    reference_cities_for_view.append(
                        {
                            "name": city["name"],
                            "distance_km": distance_km,
                            "travel_time_min": travel_time,
                        }
                    )
        except Exception as e:
            logger.warning("Failed to load reference cities for detail view: %s", e)

        # Latest ChatGPT/OpenAI structured analysis variant (if present)
        openai_analysis = None
        openai_model = None
        try:
            from models import AiAnalysisVariant

            openai_variant = (
                AiAnalysisVariant.query.filter_by(land_id=land.id, provider="openai")
                .order_by(AiAnalysisVariant.created_at.desc())
                .first()
            )
            if openai_variant:
                openai_analysis = openai_variant.analysis
                openai_model = openai_variant.model
        except Exception as e:
            logger.warning(
                "Failed to load OpenAI analysis variant for land %s: %s", land.id, e
            )

        return render_template(
            "land_detail.html",
            land=land,
            score_breakdown=score_breakdown,
            reference_cities=reference_cities_for_view,
            openai_analysis=openai_analysis,
            openai_model=openai_model,
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to load land detail %s", land_id, exc_info=True)
        flash(
            "An error occurred while loading land details. Check server logs.", "error"
        )
        return redirect(url_for("main.lands"))


@main_bp.route("/properties/<int:property_id>/advertiser", methods=["POST"])
def set_advertiser(property_id):
    """The owner's own reading of who is selling this listing.

    The last resort, and the only one for most of what it will be used on: 268
    rows here are idealista links carrying no alert campaign, and idealista
    answers a captcha to every request from this machine, so nothing automatic
    can ever establish them. A person with the page open in their own browser
    can.

    It outranks every computed reading and survives every recompute
    (`services/advertiser.enrich` refuses a hand-set row outright), which is
    the sea-view precedent -- "an owner who looked at the listing outranks both
    models". Clearing puts the row back on the computed path and restores the
    reading the hand-set verdict displaced, so using this on a fotocasa row and
    changing your mind does not throw away a page reading that cost a fetch.
    """
    from services import advertiser as advertiser_service

    prop = db.get_or_404(Property, property_id)
    wanted = (request.form.get("advertiser") or "").strip().lower()
    if wanted not in advertiser_service.HAND_SET_STATES + ("clear",):
        flash("Unknown advertiser action.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    if wanted == "clear":
        result = advertiser_service.set_by_hand(prop, None, commit=True)
        message = (
            "Cleared: the seller follows the listing again."
            if result["restored"]
            else "Cleared: the seller is no longer established for this listing."
        )
    else:
        advertiser_service.set_by_hand(prop, wanted, commit=True)
        message = (
            "Recorded: sold by its owner."
            if wanted == advertiser_service.OWNER
            else "Recorded: sold through an agency."
        )
    flash(message, "success")
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/taste/retrain", methods=["POST"])
def retrain_taste():
    """Rebuild the taste profile from the owner's comments, then re-score.

    Attended: the button carries a confirm naming the cost, and this is a
    form POST on main_bp so it rides CSRF — a drive-by page cannot spend the
    owner's bridge credit (the rule about new state-changing endpoints). The
    job is a singleton (`dedupe_key="taste_retrain"`): a second press
    attaches to the running job instead of doubling the spend, the same
    contract the enrichment button has. Retrain AND re-score in one job —
    a retrain that only marked scores stale would leave the ranking blank
    until somebody remembered a CLI (the plan-gate round-1 finding).
    """
    from services.background_jobs import enqueue_job

    app_obj = current_app._get_current_object()

    def _run():
        # Build AND rescore under one single-flight lock — a scheduler tick
        # landing mid-build must answer busy, not score against the old
        # profile (services/taste_service.retrain_and_rescore).
        rescored = taste_service.retrain_and_rescore()
        return {"success": rescored.get("status") == "ok", **rescored}

    try:
        job_id = enqueue_job(
            _run,
            job_type="taste_retrain",
            meta={},
            app=app_obj,
            dedupe_key="taste_retrain",
        )
        flash(
            "Retraining the taste profile and re-scoring in the background "
            f"(job {job_id[:8]}). Refresh in a few minutes.",
            "info",
        )
    except Exception:
        logger.error("Failed to queue the taste retrain", exc_info=True)
        flash("Could not start the taste retrain. Check server logs.", "error")
    return redirect(url_for("main.properties"))


@main_bp.route("/properties/<int:property_id>/review", methods=["POST"])
def set_review(property_id):
    """Record what the owner decided, and what is still outstanding.

    One dedicated route validating a small closed set, flashing a result and
    redirecting -- the `set_advertiser` / `set_pool_absence` idiom, and on
    `main_bp`, so the form carries a CSRF token. There is no JSON twin: every
    endpoint on `api_bp` is CSRF-exempt, and this writes the one thing in the
    application a person typed rather than a measurement.

    Both fields are submitted together because they are edited together, and
    because `services.owner_review.set_review` records one event describing the
    state it left the row in. Two routes would produce two events for one press
    and a timeline that reads like two decisions.

    A blank decision clears it, which is not the same as rejecting: the row
    goes back to `undecided`, the state a listing nobody has judged is in.
    """
    from services import owner_review as owner_review_service

    prop = db.get_or_404(Property, property_id)

    decision = (request.form.get("verdict") or "").strip().lower()
    if decision and decision not in owner_review_service.DECIDED_STATES:
        flash("Unknown verdict.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    raw_due = (request.form.get("due_on") or "").strip()
    due_on = None
    if raw_due:
        try:
            due_on = date.fromisoformat(raw_due)
        except ValueError:
            flash("The due date is not a date.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

    try:
        result = owner_review_service.set_review(
            prop,
            decision=decision or None,
            reason=request.form.get("reason"),
            action=request.form.get("next_action"),
            due_on=due_on,
            # The compact comment card does not manage the action; the
            # service reads the current one under its own lock, so a form
            # opened before another tab set the action cannot erase it.
            keep_action=bool(request.form.get("keep_action")),
        )
    except owner_review_service.ReviewError as exc:
        # A rejected write, not a crash: the message names the field.
        flash(str(exc), "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    if not result["changed"]:
        flash("Nothing changed.", "success")
    elif decision:
        flash("Recorded.", "success")
    else:
        flash("Cleared: this listing is undecided again.", "success")
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/activity", methods=["POST"])
def add_activity(property_id):
    """Add a note or one contact entry to a listing's timeline.

    One form with a kind toggle, because the two are the same act -- writing
    down something that happened -- and the fields a contact carries are the
    extra structure that act sometimes has. Two forms would mean two buttons
    for one intention.

    `happened_at` is when the exchange happened and defaults to now: an answer
    given on the phone yesterday is recorded today, and the feed is ordered by
    the first of those.
    """
    from services import owner_review as owner_review_service

    prop = db.get_or_404(Property, property_id)
    kind = (request.form.get("kind") or "").strip().lower()

    happened_at = None
    raw_when = (request.form.get("happened_on") or "").strip()
    if raw_when:
        try:
            happened_at = datetime.combine(date.fromisoformat(raw_when), time(12, 0))
        except ValueError:
            flash("That date is not a date.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

    try:
        if kind == owner_review_service.KIND_NOTE:
            entry = owner_review_service.add_note(
                prop, body=request.form.get("body"), happened_at=happened_at
            )
        elif kind == owner_review_service.KIND_CONTACT:
            entry = owner_review_service.add_contact(
                prop,
                channel=request.form.get("channel"),
                counterpart=request.form.get("counterpart"),
                asked=request.form.get("asked"),
                body=request.form.get("body"),
                happened_at=happened_at,
            )
        else:
            flash("Unknown entry type.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))
    except owner_review_service.ReviewError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    # The file rides on the entry it arrived with -- the ficha catastral
    # belongs to the WhatsApp exchange that delivered it, not to the listing in
    # general. Stored AFTER the entry exists, because the row it links to has
    # to be there first; a refused file therefore leaves the note behind rather
    # than losing what was typed with it.
    upload = request.files.get("attachment")
    if upload and upload.filename:
        from services import attachments as attachments_service

        try:
            attachments_service.attach(prop, upload, activity=entry)
        except attachments_service.AttachmentError as exc:
            flash(f"Recorded, but the file was refused: {exc}", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

    flash("Recorded.", "success")
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route(
    "/properties/<int:property_id>/activity/<int:entry_id>", methods=["POST"]
)
def edit_activity(property_id, entry_id):
    """Edit or soft-delete one entry.

    The entry is fetched **by both ids**: a URL naming another property's entry
    would otherwise edit it from this page, and the composite lookup is what
    makes that a 404 rather than a cross-property write.

    Verdict entries are refused here rather than merely hidden in the
    template. They are the record of a decision, written beside the columns
    they describe, and a control that could edit them could edit the log into
    disagreement with the state it is the history of.
    """
    from models import PropertyActivity
    from services import owner_review as owner_review_service

    entry = PropertyActivity.query.filter_by(
        id=entry_id, property_id=property_id
    ).first_or_404()

    action = (request.form.get("action") or "").strip().lower()
    if action not in ("save", "delete"):
        flash("Unknown action.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    try:
        if action == "delete":
            owner_review_service.soft_delete_entry(entry)
            flash("Removed from the timeline.", "success")
        else:
            owner_review_service.edit_entry(
                entry,
                body=request.form.get("body"),
                asked=request.form.get("asked"),
                counterpart=request.form.get("counterpart"),
                channel=request.form.get("channel") or entry.channel,
            )
            flash("Saved.", "success")
    except owner_review_service.ReviewError as exc:
        flash(str(exc), "error")

    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route(
    "/properties/<int:property_id>/attachments/<int:attachment_id>", methods=["GET"]
)
@limiter.limit("10 per minute")
def download_attachment(property_id, attachment_id):
    """Serve one attachment, addressed by id and never by path.

    The client names a row, not a file: the path comes from the database and
    was written by us from a hash, so there is nothing here for a `..` to act
    on. `send_from_directory` is still the primitive rather than `open()` --
    it is built on Werkzeug's `safe_join` and it handles conditional and range
    requests, which is what makes a 20 MB PDF resumable instead of restarting.

    Three things about the response are the security of it:

    * the `mimetype` is the **stored, sniffed** type. Left to Werkzeug it
      would be guessed from `download_name`, which is the name the *client*
      sent -- so a PDF uploaded as `photo.html` would be served as HTML;
    * `X-Content-Type-Options: nosniff`, so a browser cannot decide for itself
      that our declared type is wrong;
    * `as_attachment` unless the sniffed type is one of the raster formats a
      browser actually draws. SVG cannot arrive at all, so the question here
      is never "is this payload safe" -- it is "is this one of five image
      formats", which is a question with an answer.
    """
    from models import PropertyAttachment
    from services import attachments as attachments_service

    record = PropertyAttachment.query.filter_by(
        id=attachment_id, property_id=property_id, deleted_at=None
    ).first_or_404()

    root = attachments_service.attachments_dir()
    if not os.path.exists(os.path.join(root, record.storage_path)):
        # The write-then-commit order means this should be impossible. If it
        # happens anyway -- a restore from a database dump newer than the file
        # backup -- it must be loud rather than a plain 404, which reads as
        # "no such attachment".
        logger.error(
            "attachment %s has a row but no bytes at %s",
            record.id,
            record.storage_path,
        )
        abort(410)

    response = send_from_directory(
        root,
        record.storage_path,
        mimetype=record.content_type,
        as_attachment=not attachments_service.may_render_inline(record.content_type),
        download_name=record.original_filename or f"attachment-{record.id}",
        conditional=True,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@main_bp.route(
    "/properties/<int:property_id>/attachments/<int:attachment_id>/delete",
    methods=["POST"],
)
def delete_attachment(property_id, attachment_id):
    """Take an attachment off the page. Soft, like everything else here.

    The bytes stay until `utils/sweep_attachments.py` finds that no live row
    references that hash -- because one file can be linked from several rows,
    and because a document somebody removed by mistake is not recomputable.
    """
    from models import PropertyAttachment

    record = PropertyAttachment.query.filter_by(
        id=attachment_id, property_id=property_id
    ).first_or_404()
    record.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    flash("Removed.", "success")
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/cadastre", methods=["POST"])
@limiter.limit("5 per minute")
def set_cadastral_reference(property_id):
    """Record the parcel this listing sits on, and fetch what Catastro says.

    The first rate limit in this module, and it is here rather than in the
    idiom because this route reaches a third party. Catastro publishes no
    numeric limit and does publish an ~10-day IP ban for abuse, so the
    arithmetic has to be bounded at the door: three outbound requests per
    uncached press (`services/cadastre_service.py` runs them with no retries
    for exactly this reason), five presses a minute, fifteen requests a minute
    at the very worst. The same 5/minute the listing-status check carries, and
    for the same reason -- there is no authentication in front of any of it.

    Clearing is a separate action and makes no request: a reference typed
    wrongly has to be removable without a fetch.
    """
    from services import cadastre_service

    prop = db.get_or_404(Property, property_id)
    raw = (request.form.get("cadastral_reference") or "").strip()

    if not raw:
        # Clearing the field clears the column AND the block: leaving a
        # measurement of a parcel this listing no longer claims would be a
        # description of somewhere else.
        prop.cadastral_reference = None
        enrichment = dict(prop.enrichment or {})
        enrichment.pop(cadastre_service.ENRICHMENT_KEY, None)
        prop.enrichment = enrichment
        db.session.commit()
        flash("Cleared: no cadastral reference for this listing.", "success")
        return redirect(url_for("main.property_detail", property_id=property_id))

    normalized = cadastre_service.normalize_reference(raw)
    if not normalized:
        flash(
            "That is not a cadastral reference — it should be 14, 18 or 20 "
            "letters and digits.",
            "error",
        )
        return redirect(url_for("main.property_detail", property_id=property_id))

    try:
        block = cadastre_service.apply_to_property(prop, normalized, commit=True)
    except cadastre_service.CadastreError as exc:
        # A refusal is not a crash and not a silent success: the reference is
        # still worth storing, because the parcel it names is a fact about the
        # listing whether or not Catastro answered this minute.
        prop.cadastral_reference = normalized
        db.session.commit()
        flash(
            f"Recorded the reference; Catastro did not answer ({exc.state}).", "error"
        )
        return redirect(url_for("main.property_detail", property_id=property_id))

    run_state = block.get("run_state")
    if run_state == cadastre_service.RUN_OK:
        message = "Recorded, and the parcel was measured."
    elif run_state == cadastre_service.RUN_DEGRADED:
        message = "Recorded, and the parcel was measured — some details are missing."
    else:
        message = "Recorded; the parcel could not be measured this time."
    flash(
        message, "success" if run_state != cadastre_service.RUN_UNAVAILABLE else "error"
    )
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/pool-absence", methods=["POST"])
def set_pool_absence(property_id):
    """The owner's hand-set 'no pool here' verdict (proposal D17).

    The only path to a true pool-score 0: computed absence stays None
    because one Text Search proves nothing about completeness. The flag
    lives inside enrichment.pool, survives recomputes (pool_service keeps
    it), and clearing it puts the property back on the computed path.
    Rescores immediately so the flag never disagrees with the score.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from services.property_scoring_service import PropertyScoringService

    prop = db.get_or_404(Property, property_id)
    action = (request.form.get("pool_absence") or "").strip()
    if action not in ("set", "clear"):
        flash("Unknown pool-absence action.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))

    enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
    pool = (
        dict(enrichment.get("pool")) if isinstance(enrichment.get("pool"), dict) else {}
    )
    if action == "set":
        pool["owner_no_pool"] = {
            "set_at": datetime.now(timezone.utc).isoformat(),
            "source": "owner",
        }
        message = "Recorded: no usable pool near this property (score 0)."
    else:
        pool.pop("owner_no_pool", None)
        message = "Cleared: the pool score follows the measurements again."
    enrichment["pool"] = pool
    prop.enrichment = enrichment
    flag_modified(prop, "enrichment")

    PropertyScoringService().calculate_for_property(prop, commit=False)
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/municipalities")
def municipalities():
    """Compare the municipalities the search actually covers (proposal D22).

    Municipality facts (INE, SEPE) sit beside medians over that
    municipality's own listings — the owner's decision of 2026-08-14, taken
    over a capital-centroid basis because what he is choosing between is the
    listings, not the town halls. Every median carries its coverage.

    Since #513 this is the fifth surface that reads the subscription criteria
    (`services/subscription_criteria.py`), with the same parameter and the
    same default as the list, the map and the CSV — a filter one surface keeps
    and another drops is #445's regression, and this page was dropping it. It
    is the *counts* that move here rather than the link, because the medians
    are what the page is for and the excluded rows are concentrated at the
    cheap end: measured on production 2026-08-31, Camariñas ran 18 rows at a
    median €127,500 / €865 per m² against 11 rows at €200,000 / €584 under the
    default reading, which is the difference between the cheapest municipality
    on the page and an ordinary one. Seven houses the owner's own criteria rule
    out were producing that cheapness.

    Two things the criteria reading may not touch. **Municipality facts are
    not listing statistics** — INE renta, población and SEPE unemployment
    involve no listing at all, so they read identically under every mode, and
    the page's own contract keeps the two kinds apart. And **a narrowing with
    nothing saying what it excluded is #98's defect in a new place**, so the
    page states how many listings, and how many municipalities, this reading
    is not counting.
    """
    from services.municipality_comparison_service import (
        DEFAULT_SORT,
        SORT_KEYS,
        MunicipalityComparisonService,
        drilldown_args,
        drilldown_truncates,
    )
    from services.population import (
        Population,
        listings_by_profile,
        subscription_mix,
    )

    try:
        from services.search_profile_service import SearchProfileService

        include_archived = request.args.get("archived") == "on"
        favorites_only = request.args.get("favorites") == "on"
        # Canonicalised, unlike the list's raw read: this page's control is a
        # menu of links rather than a select, so an unrecognised value would
        # leave every option un-highlighted while the default reading was
        # quietly applied — a control saying "nothing is on" over a page that
        # is narrowed (#104's shape, in the criteria parameter).
        criteria_filter = criteria_mode(request.args.get("criteria", ""))
        criteria_ctx = _criteria_context()
        sort_by = request.args.get("sort") or DEFAULT_SORT
        if sort_by not in SORT_KEYS:
            sort_by = DEFAULT_SORT
        order = request.args.get("order") or (
            "asc" if sort_by == "municipality" else "desc"
        )

        # `profile_id` is the codebase's one spelling of "which subscriptions",
        # parsed by the module that owns it (MUNIC-002). What differs here is
        # the *fallback*, which is what `auto_profile_id` exists for: a bare
        # /properties is rewritten to `all` and a bare /map resolves to one
        # profile, while a bare /municipalities filters by nothing at all --
        # this page compares municipalities, not saved searches, so its own
        # population is every stored listing, retired and hidden subscriptions
        # included.
        #
        # That makes this the one surface where `all` is *narrower* than the
        # bare URL: `all` means "active and not hidden" here exactly as it does
        # on /properties, /map, the CSV export and the JSON API, and
        # redefining it to mean "everything" would be one spelling with two
        # meanings across four surfaces -- silently, since the token would look
        # identical. Measured on production 2026-08-19, adding `?profile_id=all`
        # to a bare /municipalities narrows it by 311 of 772 listings.
        offered_profiles = SearchProfileService.list_visible_profiles(active_only=True)
        profile_selection = resolve_profile_selection(
            parse_profile_selection(request.args),
            [profile.id for profile in offered_profiles],
            auto_profile_id=None,
        )

        query = Property.query
        if not include_archived:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )
        if favorites_only:
            query = query.filter(Property.is_favorite.is_(True))

        # How many listings each subscription holds *in this page's other
        # scopes* -- counted off the query with the status, favorites and
        # criteria filters applied and the subscription filter not yet, so the
        # number beside a name in the menu is the number picking it would
        # show. Same order, and the same reason, as `unassigned_count` on
        # /properties: SQLAlchemy queries are immutable, so the two branches
        # cannot drift. `live == held` on purpose: this page's count already
        # answers "what picking it would show", so the number displayed and
        # the number that decides whether the option is offered are the same
        # fact here.
        #
        # The criteria reading is on this branch too, and that is the whole
        # point of putting it before the menu: a count in the menu taken
        # without it would promise a subscription's 443 listings and open on
        # the 384 this page counts, which is the defect being fixed one
        # control over.
        counted, _ = _apply_criteria_filter(query, criteria_ctx, criteria_filter)
        listing_counts = {
            profile_id: ProfileListingCount(live=count, held=count)
            for profile_id, count in counted.with_entities(
                Property.search_profile_id, func.count(Property.id)
            ).group_by(Property.search_profile_id)
        }

        # The filter goes on the rows *entering* `build_rows`, never on the
        # rows leaving it. Everything the table says about a municipality --
        # its medians, every coverage count, `row["scope"]` and therefore the
        # drill-down link -- is derived from what that function was handed, so
        # filtering afterwards would leave all of it describing the
        # unfiltered set while the page claimed otherwise. That is #417
        # exactly, on a new axis.
        scope_query = apply_profile_filter(
            query, Property.search_profile_id, profile_selection
        )
        properties, criteria_excluded_rows = _split_by_criteria(
            scope_query, criteria_ctx, criteria_filter
        )

        # The menu's rows, from the helper /properties builds its own from --
        # live first, the archive after it, a hidden one only when the URL
        # names it, and nothing that holds no listings here. `counts` is the
        # pre-filter tally above, so the number beside a name says what
        # picking it would show rather than what is on screen now.
        # The menu lists the hidden subscriptions as well, unlike
        # /properties': this page's own population already contains them and
        # its Scope line already counts them, so leaving them out of the
        # control would disclose rows the reader cannot reach. `all` is
        # resolved against `offered_profiles` and stays "active and not
        # hidden", which is what it means on every other surface.
        scope_options = _profile_dropdown_options(
            SearchProfileService.list_profiles(active_only=True, include_hidden=True),
            profile_selection,
            counts=listing_counts,
            include_hidden=True,
        )
        unassigned_available = listing_counts.get(None, EMPTY_PROFILE_COUNT).live

        service = MunicipalityComparisonService()
        rows = service.build_rows(properties)
        rows = service.sort_rows(rows, sort_by, descending=(order == "desc"))

        # The link beside each number opens exactly the listings that number
        # was taken over -- the subscriptions that carried it, retired and
        # hidden ones included, the unassigned rows, this page's favorites
        # mode and its listing-status scope (#417). The scope travels with the
        # row from `build_rows`; nothing here asks the database a second
        # question about it, because a second query is how the two numbers
        # came to disagree in the first place.
        for row in rows:
            row["drilldown"] = drilldown_args(
                row,
                favorites_only=favorites_only,
                include_archived=include_archived,
                criteria=criteria_filter,
            )
            row["drilldown_truncated"] = drilldown_truncates(row)

        # What this criteria reading is not counting, in the two units the
        # page is written in. The listings are the rows `_split_by_criteria`
        # held back; the municipalities are the ones that disappear from the
        # table *entirely* -- every one of their listings excluded -- because
        # a municipality silently absent reads as a municipality with nothing
        # in it, which is #98 one level above the row. A municipality that
        # merely lost some of its listings is still on screen with its own
        # coverage counts beside every median.
        shown_keys = {row["key"] for row in rows}
        vanished = {
            key
            for key in (group_key(p.municipality) for p in criteria_excluded_rows)
            if key is not None and key not in shown_keys
        }
        criteria_excluded = {
            "listings": len(criteria_excluded_rows),
            "municipalities": len(vanished),
        }

        # Truncated email artifacts ("Ovi...", issue #298) count with the
        # unnamed listings: build_rows skips both, for the same reason -- a
        # value that names no municipality cannot be compared by one. Both
        # sides ask `group_key`, so the footnote cannot drift from the table
        # it is explaining.
        unnamed = sum(1 for p in properties if group_key(p.municipality) is None)

        # What the whole table is a comparison *of* (UNIVERSE-001). This page
        # spans every subscription -- 311 of production's 772 listings sit in
        # retired ones -- and said so nowhere, which left "87 municipalities ·
        # 772 listings" reading as the owner's live searches. The mix is
        # tallied off the rows the medians were computed from, the same rule
        # the drill-down link follows (#417).
        population = Population(
            label="stored_inventory",
            total=len(properties),
            returned=len(properties),
            # No `basis` here on purpose. This page states its adjustment
            # basis in the reader's own language, from `municipalities_basis_note`
            # -- a second, English, machine-readable copy of the same sentence
            # would be one more thing to keep in agreement and nothing renders
            # it. `basis` is for the surfaces whose reader is a machine or a
            # log.
            subscriptions=subscription_mix(listings_by_profile(properties)),
        )

        return render_template(
            "municipalities.html",
            rows=rows,
            population=population,
            sort_by=sort_by,
            order=order,
            include_archived=include_archived,
            favorites_only=favorites_only,
            profile_selection=profile_selection,
            scope_options=scope_options,
            unassigned_available=unassigned_available,
            listing_total=len(properties),
            unnamed_listings=unnamed,
            criteria_choices=CRITERIA_CHOICES,
            criteria_filter=criteria_filter,
            criteria_enabled=criteria_ctx is not None,
            criteria_excluded=criteria_excluded,
            sources=service.qol_service.reference_sources(),
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to build the municipality comparison", exc_info=True)
        flash(
            "An error occurred while comparing municipalities. Check server logs.",
            "error",
        )
        return redirect(url_for("main.properties"))


@main_bp.route("/construccion")
def construccion():
    """The building-rules reference: regional dossier + per-concejo overlay.

    The concejo is an explicit human choice, never derived from a listing --
    the design decision seven review rounds arrived at (services/
    concejo_legal.py has the history). A missing identity snapshot refuses
    the page outright: identity served from a fallback is the defect the
    snapshot exists to remove.
    """
    from services import concejo_legal
    from services.buildability_catalog import TOPICS, topics_for_chapter

    if request.args.get("view") == "full":
        body = concejo_legal.load_full_dossier()
        # ?concejo= is deliberately ignored here: the full document has no
        # per-concejo machinery, so no municipal context may exist on it.
        return render_template("construccion_full.html", body=body), (
            200 if body else 503
        )

    try:
        snapshot = concejo_legal.load_snapshot()
    except concejo_legal.SnapshotUnavailable:
        logger.exception("concejo identity snapshot unavailable")
        return (
            render_template(
                "construccion.html",
                snapshot_missing=True,
                chapters=[],
                selector=[],
                scope_codes=[],
                coverage=None,
                selected=None,
                code_rejected=False,
            ),
            503,
        )

    raw_code = request.args.get("concejo")
    code = concejo_legal.validate_code(raw_code, snapshot)
    code_rejected = bool(raw_code) and code is None

    selected = None
    if code:
        payload = concejo_legal.load_concejo(code)
        selected = {
            "code": code,
            "name": snapshot[code],
            "researched": concejo_legal.researched(payload),
            "cells": {t: concejo_legal.cell_for(payload, t) for t in TOPICS},
        }

    chapters = []
    for chapter in concejo_legal.load_chapters():
        chapter["local_topics"] = topics_for_chapter(chapter["file_id"])
        chapters.append(chapter)

    scope_codes = [c for c in concejo_legal.load_scope() if c in snapshot]
    selector = sorted(snapshot.items(), key=lambda kv: kv[1])
    return render_template(
        "construccion.html",
        snapshot_missing=False,
        chapters=chapters,
        selector=selector,
        scope_codes=scope_codes,
        coverage=concejo_legal.coverage(snapshot),
        selected=selected,
        code_rejected=code_rejected,
        topics=TOPICS,
    )


@main_bp.route("/agencies")
def agencies():
    """The curated agency table (owner request 2026-08-22).

    Which agencies hold the most detached houses up to 300 000 EUR in Asturias
    and Cantabria -- a dated measurement read from data/top_agencies.json
    (services/agency_directory.py), not a live feed. A missing or unreadable
    file refuses the page with 503 and says so: an empty table would read as
    "no agencies", which is the #98 defect in a reference file.
    """
    from services.agency_directory import AgencyDataUnavailable, load_top_agencies

    try:
        table = load_top_agencies()
    except AgencyDataUnavailable:
        logger.exception("agency table unavailable")
        return render_template("agencies.html", table=None), 503
    return render_template("agencies.html", table=table)


@main_bp.route("/map")
def map_view():
    """Interactive map view of all properties with coordinates"""
    try:
        from services.property_travel_service import (
            TRAVEL_STATE_APPROXIMATE_ORIGIN,
            effective_travel_state,
        )
        from services.search_profile_service import SearchProfileService

        default_profile = SearchProfileService.get_default_profile(create=True)
        # Same rule as /properties: a hidden subscription is not on the map
        # either, unless `profile_id` names it (2026-08-17).
        profiles = SearchProfileService.list_visible_profiles(active_only=True)

        # A `focus=<id>` is read before the subscription is resolved: it is
        # what the auto fallback answers to (#287).
        focus_id = _parse_focus_id(request.args.get("focus"))
        focus_property = (
            db.session.get(Property, focus_id) if focus_id is not None else None
        )

        # Same `profile_id` contract as /properties (#104): auto | all |
        # selected(ids). Only the auto fallback differs -- the map prefers the
        # subscription holding the focused listing, then the one with the most
        # mappable rows.
        selection = parse_profile_selection(request.args)
        profile_selection = resolve_profile_selection(
            selection,
            [profile.id for profile in profiles],
            auto_profile_id=(
                _map_auto_profile_id(default_profile, profiles, focus_property)
                if selection.is_auto
                else None
            ),
        )
        selected_profile_id = profile_selection.single_id

        query = Property.query.filter(
            Property.location_lat.isnot(None),
            Property.location_lon.isnot(None),
        )

        # Read through `FilterArgs` rather than straight off `request.args`:
        # this page has to hand its filters on to the List View link, and the
        # hand-written list that did so went stale twice in one day (#445 --
        # utils/listing_filters.py records which, and why naming the missing
        # ones is the fix that keeps failing). What is read here is what that
        # link carries; there is no second list to keep in step.
        filters = FilterArgs(request.args)
        category_filter = filters.get("category")
        subtype_filter = filters.get("subtype")
        municipality_filter = filters.get("municipality")
        source_filter = filters.get("source")
        advertiser_filter = filters.get("advertiser")
        verdict_filter = filters.get("verdict")
        action_filter = filters.get("action")
        # One date for the whole request. `overdue` is a due date compared
        # against today, and the badge, the filter, the count beside its option
        # and both serializers have to compare against the *same* today or they
        # disagree for the few minutes a day nobody is watching
        # (services/owner_review.py).
        review_today = owner_review.today()
        search_query = filters.get("search")
        investment_metrics_filter = filters.get("inv_metr")
        favorites_filter = filters.flag("favorites")
        sea_view_filter = filters.get("sea_view")
        sea_distance_filter = filters.get("sea_dist")
        build_filter = filters.get("build")
        # #445. This page ignored `measured` while /properties applied it, so
        # pressing Map on a narrowed list widened it again and said nothing:
        # measured on production 2026-08-20, the list found 72 listings and the
        # map plotted 470.
        measured_filter = filters.get("measured")
        # Likeness to the subscription's favorites, the same reading as the
        # list (services/favorite_similarity.py). Read through `FilterArgs`
        # like the rest, so the List View link carries it back.
        similar_filter = filters.get("similar")

        if category_filter:
            if category_filter == UNCLASSIFIED_FILTER:
                query = query.filter(_unclassified_clause(Property.property_category))
            else:
                query = query.filter(Property.property_category == category_filter)
        if subtype_filter:
            if subtype_filter == UNCLASSIFIED_FILTER:
                query = query.filter(_unclassified_clause(Property.property_subtype))
            else:
                query = query.filter(Property.property_subtype == subtype_filter)
        if municipality_filter:
            query = query.filter(municipality_filter_clause(municipality_filter))
        # Which site the listing is on. utils/listing_source.py owns the
        # reading, so the four surfaces cannot drift apart on it.
        source_clause = source_filter_clause(Property, source_filter)
        if source_clause is not None:
            query = query.filter(source_clause)
        # Who is selling. services/advertiser.py owns the reading, so the
        # badge, this filter and the count printed beside its option are one
        # answer rather than three.
        advertiser_clause = advertiser.filter_clause(Property, advertiser_filter)
        if advertiser_clause is not None:
            query = query.filter(advertiser_clause)
        # What the owner decided, and what is still outstanding.
        # services/owner_review.py owns both readings, so the badge, these two
        # filters and the counts beside their options are one answer rather
        # than several. Both filters are applied here rather than one of them
        # here and the other elsewhere: a surface that keeps one parameter and
        # drops the other is the regression these two are tested against
        # together.
        verdict_clause = owner_review.decision_filter_clause(Property, verdict_filter)
        if verdict_clause is not None:
            query = query.filter(verdict_clause)
        action_clause = owner_review.action_filter_clause(
            Property, action_filter, review_today
        )
        if action_clause is not None:
            query = query.filter(action_clause)
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)
        if investment_metrics_filter:
            query = filter_by_investment_rating(
                query, Property, investment_metrics_filter
            )
        if sea_view_filter:
            query = filter_by_sea_view(query, Property, sea_view_filter)
        if sea_distance_filter:
            query = filter_by_sea_distance(query, Property, sea_distance_filter)
        if build_filter:
            query = filter_by_land_classification(query, Property, build_filter)
        # Same helper and same position as /properties, so one URL cannot
        # describe two sets across the two surfaces (#445).
        if measured_filter:
            query = filter_by_measured(query, Property, measured_filter)
        # Same reading and same position as /properties (#445's rule: a
        # filter one surface keeps and another drops is the regression).
        # Built only under a cut: the map draws no chip and has no sort.
        if similar_filter:
            query = favorite_similarity.apply_filter(
                query,
                Property,
                favorite_similarity.build_context(
                    profile_ids=_similarity_scope(profile_selection)
                ),
                similar_filter,
            )

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        query = query.filter(Property.listing_status.notin_(DELISTED_LISTING_STATUSES))

        # The same criteria reading as the list (#445's rule: a filter one
        # surface keeps and another drops is the regression, one filter over)
        # -- pressing Map on the default view must not widen it with the
        # hidden fails. Applied to BOTH branches: the focus notice compares
        # against `query_without_profile`, and a criteria-hidden focus row in
        # another subscription must not read as "merely in another
        # subscription" when picking that subscription would not plot it.
        #
        # The count is asked of the profile-filtered branch, after every
        # other clause, so it says what was withheld from THIS map — hidden
        # rows without coordinates are not markers this page could have
        # drawn, which is why the number here is 8 where the list says 62
        # (production 2026-09-01, profile 24: 149 markers against 157 under
        # criteria=all, with templates/map.html not containing the word
        # "criteria" at all). The map applied the default hide in silence,
        # and on the one surface where an empty area IS the answer being
        # read, a silent hide is #98's shape: nothing here reads as nothing
        # there.
        criteria_ctx = _criteria_context()
        criteria_param = filters.get("criteria")
        query_without_profile, _ = subscription_criteria.apply_filter(
            query, criteria_ctx, criteria_param
        )
        # The subscription filter goes on last so the query without it stays
        # in hand. That is what separates a focused listing that is merely in
        # another subscription -- the one case the page can offer a way out of
        # -- from one no filter here would have let through (#287). Identical
        # SQL either way: SQLAlchemy ANDs the clauses whatever their order.
        query = apply_profile_filter(
            query, Property.search_profile_id, profile_selection
        )
        query, criteria_hidden_count = subscription_criteria.apply_filter(
            query, criteria_ctx, criteria_param, count_hidden=True
        )
        props = query.all()

        # The lift, in the only control this page has — a link. Same URL,
        # criteria=all: `rebuilt_from` keeps the profile selection, the
        # filters and the focus, and the mode is STATED rather than dropped
        # because `criteria` is the one filter whose absence still filters
        # (utils/listing_filters.CLEARED_NOT_ABSENT).
        criteria_reveal_url = None
        if criteria_hidden_count:
            reveal_args = rebuilt_from(request.args, drop=("criteria",))
            reveal_args["criteria"] = "all"
            criteria_reveal_url = url_for("main.map_view", **reveal_args)

        focus_notice = _map_focus_notice(
            focus_id, focus_property, props, query_without_profile
        )

        # Profile-driven travel targets for popup display.
        travel_display_targets = []
        if selected_profile_id is not None:
            try:
                selected_profile = db.session.get(SearchProfile, selected_profile_id)
            except Exception:
                selected_profile = None

            travel_config = SearchProfileService.get_travel_targets_config(
                selected_profile
            )
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = (
                travel_config.get("presets") if isinstance(travel_config, dict) else {}
            )

            icon_map = {
                "airport": "fa-plane",
                "train_station": "fa-train",
                "hospital": "fa-hospital",
                "police": "fa-shield-halved",
                "supermarket": "fa-cart-shopping",
                "school": "fa-school",
            }

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = (
                    bool((presets_cfg.get(key) or {}).get("enabled", True))
                    if isinstance(presets_cfg, dict)
                    else True
                )
                if not enabled:
                    continue
                travel_display_targets.append(
                    {
                        "key": key,
                        "label": preset.get("label") or key,
                        "icon": icon_map.get(key) or "fa-route",
                        "kind": "preset",
                    }
                )

            for item in (
                (travel_config.get("custom") or [])
                if isinstance(travel_config, dict)
                else []
            ):
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {
                        "key": f"custom:{target_id}",
                        "label": name,
                        "icon": "fa-location-dot",
                        "kind": "custom",
                    }
                )

        # "List View" has to land on the set this map is drawing. It carried
        # `profile_id` alone until #444, so a map of the 52 listings sold by
        # their owners had a button that opened a list of 144.
        #
        # It is no longer a list of keys. #444 wrote one, and it was stale
        # within the hour -- `verdict` and `action` (#430) had reached
        # /properties that same morning -- which is why the filters are now
        # read through `FilterArgs` above and handed straight back here. What
        # this page reads is what this link carries, and there is nothing to
        # keep in step (utils/listing_filters.py).
        #
        # `hide_removed` is the one key that is *not* the record of a read, and
        # it is stated rather than copied: the map excludes delisted listings
        # unconditionally (the `notin_` above) whatever the caller asked, so
        # 'on' is what this map is really showing, and reading the parameter
        # would send the reader to a list holding rows the map refused to plot.
        # Stating it also follows `utils/listing_status_scope.py` (#439) -- a
        # link should say what it means rather than rely on the reading at the
        # far end. Note what it is no longer: before #439 the value was load
        # bearing, because an absent `hide_removed` beside any other filter
        # read as an unticked box and widened the list. That mechanism is gone,
        # and measured today the far end agrees either way. Keep the statement;
        # do not restore the old reasoning for it.
        #
        # `measured` used to be excluded here, deliberately, because this page
        # did not apply it -- and a link carrying a filter its origin ignored
        # opens a list narrower than the map it came from. #445 removed the
        # premise by applying it above, so it now rides the link like any other
        # filter. If a future filter is again read by /properties and not by
        # this page, the answer is the same as it was: do not carry it.
        list_view_args = {
            "profile_id": list(profile_selection.link_values),
            **filters.link_args(),
            "hide_removed": "on",
        }

        markers = []
        for prop in props:
            markers.append(
                {
                    "id": prop.id,
                    "lat": float(prop.location_lat),
                    "lon": float(prop.location_lon),
                    "title": prop.title or f"Property #{prop.id}",
                    "price": float(prop.price) if prop.price else None,
                    "area": float(prop.area) if prop.area else None,
                    "score": float(prop.score_total) if prop.score_total else None,
                    "category": prop.property_category,
                    "subtype": prop.property_subtype,
                    "url": prop.url,
                    "municipality": prop.municipality,
                    "travel": prop.travel if isinstance(prop.travel, dict) else None,
                    # The popup draws the same travel badges the list does, so
                    # it needs the same warning: a marker on a locality
                    # centroid is not where the property is, and its durations
                    # are routes from there.
                    "origin_approximate": (
                        effective_travel_state(prop) == TRAVEL_STATE_APPROXIMATE_ORIGIN
                    ),
                    "is_favorite": bool(prop.is_favorite),
                }
            )

        return render_template(
            "map.html",
            markers=markers,
            profiles=profiles,
            selected_profile_id=selected_profile_id,
            profile_selection=profile_selection,
            list_view_args=list_view_args,
            travel_display_targets=travel_display_targets,
            focus_notice=focus_notice,
            # The map drops a hidden subscription's markers exactly as the
            # list drops its rows, so it owes the same disclosure -- a map
            # that silently stopped plotting several subscriptions reads as
            # one showing everything there is.
            hidden_subscription_note=_hidden_subscription_note(profile_selection),
            # And the same disclosure for the criteria hide, with the same
            # one-home line the list draws beside its result count. None
            # under every explicit mode and when nothing was hidden.
            criteria_hidden_count=criteria_hidden_count,
            criteria_reveal_url=criteria_reveal_url,
        )

    except Exception:
        logger.error("Failed to load map view", exc_info=True)
        flash("An error occurred while loading map. Check server logs.", "error")
        return render_template(
            "map.html",
            markers=[],
            profiles=[],
            selected_profile_id=None,
            profile_selection=empty_profile_selection(),
            list_view_args={},
            travel_display_targets=[],
            focus_notice=None,
            # It failed before it could ask; "0 hidden" would be a claim.
            hidden_subscription_note=None,
            criteria_hidden_count=None,
            criteria_reveal_url=None,
        )


@main_bp.route("/criteria")
def criteria():
    """Scoring criteria management page with dual scoring profiles"""
    try:
        from config import Config
        from services.scoring_service import ScoringService
        from services.settings_service import SettingsService
        from utils.city_registry import all_city_names
        from models import MarketSettings

        # Load profile weights using ScoringService for consistency
        scoring_service = ScoringService()

        # Get investment profile weights (DB first, Config fallback)
        investment_weights = scoring_service._load_profile_weights("investment")
        if not investment_weights and hasattr(Config, "SCORING_PROFILES"):
            investment_weights = Config.SCORING_PROFILES.get("investment", {})

        # Get lifestyle profile weights (DB first, Config fallback)
        lifestyle_weights = scoring_service._load_profile_weights("lifestyle")
        if not lifestyle_weights and hasattr(Config, "SCORING_PROFILES"):
            lifestyle_weights = Config.SCORING_PROFILES.get("lifestyle", {})

        # Get combined mix ratio (DB first, Config fallback)
        combined_mix = scoring_service._load_combined_mix()

        # Get criteria descriptions for display
        criteria_descriptions = {
            "investment_yield": "Rental yield, cap rate, investment metrics and return potential",
            "location_quality": "Proximity to urban centers, municipality prestige, neighborhood quality",
            "transport": "Public transport access, highways, airports, train stations",
            "infrastructure_basic": "Water, electricity, sewerage, internet connectivity",
            "infrastructure_extended": "Gas, telecommunications, public services infrastructure",
            "environment": "Environmental quality, views, natural features, orientation",
            "physical_characteristics": "Land size, shape, topography, price per square meter",
            "services_quality": "Schools, hospitals, shopping, restaurants quality ratings",
            "legal_status": "Zoning status, building permissions, land classification",
            "development_potential": "Future development possibilities, urbanization plans",
        }

        reference_cities = SettingsService.get_reference_cities()
        city_registry_names = all_city_names()

        # Get market settings
        market_settings = MarketSettings.get_settings()

        # What these weights actually score. The page used to promise "all
        # properties"; they are the legacy Land vocabulary and only the legacy
        # scorer reads them (#239).
        from models import Land

        legacy_land_count = Land.query.count()

        return render_template(
            "criteria.html",
            investment_weights=investment_weights,
            lifestyle_weights=lifestyle_weights,
            combined_mix=combined_mix,
            criteria_descriptions=criteria_descriptions,
            reference_cities=reference_cities,
            city_registry_names=city_registry_names,
            market_settings=market_settings,
            legacy_land_count=legacy_land_count,
        )

    except Exception:
        logger.error("Failed to load criteria page", exc_info=True)
        flash("An error occurred while loading criteria. Check server logs.", "error")
        return render_template(
            "criteria.html",
            investment_weights={},
            lifestyle_weights={},
            combined_mix={"investment": 0.32, "lifestyle": 0.68},
            criteria_descriptions={},
            market_settings=None,
            legacy_land_count=0,
        )


@main_bp.route("/settings/properties", methods=["GET", "POST"])
def property_settings():
    """Global settings for universal properties (ingestion + classification rules + AI context)."""
    from services.settings_service import (
        SettingsService,
        DEFAULT_PROPERTY_CLASSIFICATION_RULES,
        DEFAULT_AI_MARKET_CONTEXT,
    )

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_reference_cities":
            try:
                city_names = [
                    n
                    for n in request.form.getlist("reference_city_name")
                    if (n or "").strip()
                ]
                SettingsService.set_reference_city_names(city_names)
                flash(
                    "Reference cities updated. Re-enrich properties to update travel metrics.",
                    "success",
                )
            except Exception:
                logger.error("Failed to update reference cities", exc_info=True)
                flash(
                    "An error occurred while updating reference cities. Check server logs.",
                    "error",
                )
            return redirect(url_for("main.property_settings"))

        if action == "save_ai_context":
            raw = (request.form.get("ai_market_context") or "").strip()
            SettingsService.set_ai_market_context(raw or DEFAULT_AI_MARKET_CONTEXT)
            flash("Global AI market context saved", "success")
            return redirect(url_for("main.property_settings"))

        if action == "save_classification_rules":
            raw = (request.form.get("classification_rules_json") or "").strip()
            if not raw:
                SettingsService.set_property_classification_rules(
                    [dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES]
                )
                flash("Classification rules reset to defaults", "success")
                return redirect(url_for("main.property_settings"))

            import json

            try:
                parsed = json.loads(raw)
            except Exception:
                flash(
                    "Invalid JSON for classification rules. Check syntax and try again.",
                    "error",
                )
                return redirect(url_for("main.property_settings"))

            if not isinstance(parsed, list):
                flash("Classification rules must be a JSON array", "error")
                return redirect(url_for("main.property_settings"))

            from services.settings_service import _validate_classification_rule

            validated = []
            for item in parsed:
                if isinstance(item, dict):
                    valid = _validate_classification_rule(item)
                    if valid:
                        validated.append(valid)

            if not validated:
                flash(
                    "No valid rules found. Provide at least one rule with category/subtype/pattern/priority.",
                    "error",
                )
                return redirect(url_for("main.property_settings"))

            validated.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
            SettingsService.set_property_classification_rules(validated)
            flash("Classification rules saved", "success")
            return redirect(url_for("main.property_settings"))

        if action == "save_ingestion_settings":
            sale_only = request.form.get("sale_only") == "on"
            raw_excluded = (request.form.get("excluded_categories") or "").strip()
            excluded = (
                [part.strip() for part in raw_excluded.split(",") if part.strip()]
                if raw_excluded
                else []
            )

            SettingsService.set_sale_only(sale_only)
            SettingsService.set_excluded_property_categories(excluded)

            flash("Ingestion settings saved", "success")
            return redirect(url_for("main.property_settings"))

        flash("Unknown action", "error")
        return redirect(url_for("main.property_settings"))

    # GET
    try:
        import json

        classification_rules_json = json.dumps(
            SettingsService.get_property_classification_rules(),
            indent=2,
            ensure_ascii=False,
        )
    except Exception:
        classification_rules_json = ""

    # Reference cities registry (for datalist/autocomplete).
    reference_cities = []
    city_registry_names = []
    try:
        from utils.city_registry import all_city_names

        reference_cities = SettingsService.get_reference_cities()
        city_registry_names = all_city_names()
    except Exception:
        reference_cities = []
        city_registry_names = []

    # Diagnostic/ops info for troubleshooting ingestion cursors.
    last_seen_uid = None
    try:
        import os

        from config import Config

        uid_path = getattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", None) or getattr(
            Config, "LAST_SEEN_UID_PATH", None
        )
        if uid_path and os.path.exists(uid_path):
            with open(uid_path, "r") as f:
                last_seen_uid = int((f.read() or "").strip() or "0")
    except Exception:
        last_seen_uid = None

    try:
        from models import Property

        property_count = Property.query.count()
    except Exception:
        property_count = None

    return render_template(
        "property_settings.html",
        ai_market_context=SettingsService.get_ai_market_context(),
        classification_rules_json=classification_rules_json,
        default_ai_market_context=DEFAULT_AI_MARKET_CONTEXT,
        sale_only=SettingsService.get_sale_only(),
        excluded_categories_csv=", ".join(
            SettingsService.get_excluded_property_categories()
        ),
        last_seen_uid=last_seen_uid,
        property_count=property_count,
        reference_cities=reference_cities,
        city_registry_names=city_registry_names,
    )


@main_bp.route("/criteria/update", methods=["POST"])
def update_criteria():
    """Update scoring criteria weights"""
    try:
        from services.scoring_service import known_criteria_names

        # Get form data. Unknown names are rejected before any write: these
        # weights are stored under profile='combined', the same namespace the
        # investment/lifestyle mix lives in, so a field named weight_investment
        # would silently repoint the combined score (#48).
        valid_criteria = known_criteria_names()
        weights = {}
        for key, value in request.form.items():
            if key.startswith("weight_"):
                criteria_name = key.replace("weight_", "")
                if criteria_name not in valid_criteria:
                    flash(f"Unknown scoring criterion: {criteria_name}", "error")
                    return redirect(url_for("main.criteria"))
                try:
                    weights[criteria_name] = float(value)
                except ValueError:
                    flash(f"Invalid weight value for {criteria_name}", "error")
                    return redirect(url_for("main.criteria"))

        # Update weights using scoring service
        from services.scoring_service import ScoringService

        scoring_service = ScoringService()

        if scoring_service.update_weights(weights, profile="combined"):
            flash(
                "Scoring criteria updated successfully. All lands have been rescored.",
                "success",
            )
        else:
            flash("Failed to update scoring criteria", "error")

        return redirect(url_for("main.criteria"))

    except Exception:
        logger.error("Failed to update criteria", exc_info=True)
        flash("An error occurred while updating criteria. Check server logs.", "error")
        return redirect(url_for("main.criteria"))


@main_bp.route("/criteria/update_profile/<profile>", methods=["POST"])
def update_criteria_profile(profile):
    """Update scoring criteria weights for a specific profile (investment/lifestyle)"""
    try:
        # Validate profile
        if profile not in ["investment", "lifestyle"]:
            flash(f"Invalid profile: {profile}", "error")
            return redirect(url_for("main.criteria"))

        # Parse form data to get new weights
        weights = {}
        for key, value in request.form.items():
            if key.startswith("weight_"):
                criteria_name = key.replace("weight_", "")
                try:
                    weights[criteria_name] = float(value)
                except ValueError:
                    flash(f"Invalid weight value for {criteria_name}", "error")
                    return redirect(url_for("main.criteria"))

        logger.info(f"Updating {profile} profile weights: {weights}")

        # Use ScoringService to update weights for specific profile
        from services.scoring_service import ScoringService

        scoring_service = ScoringService()

        if scoring_service.update_weights(weights, profile=profile):
            # It rescored lands. Saying "all properties" sent the owner back to
            # /properties to look for a change that was never going to be there
            # (#239).
            from models import Land

            flash(
                f"{profile.title()} weights updated; "
                f"{Land.query.count()} legacy land listings rescored. "
                "Listings on /properties score by their subscription's own config.",
                "success",
            )
        else:
            flash(f"Failed to update {profile} profile weights.", "error")

    except Exception:
        logger.error("Failed to update %s profile criteria", profile, exc_info=True)
        flash(
            "An error occurred while updating profile criteria. Check server logs.",
            "error",
        )

    return redirect(url_for("main.criteria"))


@main_bp.route("/criteria/update_combined_mix", methods=["POST"])
def update_combined_mix():
    """Update the Investment vs Lifestyle balance for combined scoring"""
    from decimal import Decimal as D

    from models import ScoringCriteria
    from services.scoring_service import ScoringService

    # Parse form data
    try:
        investment_weight = float(request.form.get("investment_weight", 0.32))
        lifestyle_weight = float(request.form.get("lifestyle_weight", 0.68))
    except (TypeError, ValueError):
        flash("Combined mix weights must be numbers.", "error")
        return redirect(url_for("main.criteria"))

    # Validate weights sum to 1.0
    total_weight = investment_weight + lifestyle_weight
    if abs(total_weight - 1.0) > 0.01:
        flash(f"Combined mix weights must sum to 1.0, got {total_weight:.3f}", "error")
        return redirect(url_for("main.criteria"))

    logger.info(
        f"Updating combined mix: Investment={investment_weight:.3f}, Lifestyle={lifestyle_weight:.3f}"
    )

    try:
        # Persist combined mix to database
        for key, val in [
            ("investment", investment_weight),
            ("lifestyle", lifestyle_weight),
        ]:
            row = ScoringCriteria.query.filter_by(
                criteria_name=key, profile="combined"
            ).first()
            if row:
                row.weight = D(str(val))
                row.active = True
            else:
                db.session.add(
                    ScoringCriteria(
                        criteria_name=key,
                        profile="combined",
                        weight=D(str(val)),
                        active=True,
                    )
                )
        db.session.commit()

        # Rescore all lands with new mix in batches
        scoring_service = ScoringService()
        batch_size = 100
        offset = 0
        total_rescored = 0

        while True:
            lands = Land.query.limit(batch_size).offset(offset).all()
            if not lands:
                break

            for land in lands:
                scoring_service.calculate_score(land)

            db.session.commit()
            total_rescored += len(lands)
            offset += batch_size

    except SQLAlchemyError:
        # Only database failures are expected here: calculate_score() swallows
        # its own errors. Anything else is a bug and must surface as a 500
        # instead of a flash message that hides it.
        db.session.rollback()
        logger.error("Failed to update combined mix", exc_info=True)
        flash(
            "An error occurred while updating combined mix. Check server logs.", "error"
        )
        return redirect(url_for("main.criteria"))

    flash(
        f"Combined mix updated to {investment_weight * 100:.0f}% Investment + {lifestyle_weight * 100:.0f}% Lifestyle. {total_rescored} properties rescored!",
        "success",
    )
    return redirect(url_for("main.criteria"))


@main_bp.route("/criteria/update_reference_cities", methods=["POST"])
def update_reference_cities():
    """Update reference cities used for travel-time scoring/display."""
    try:
        from services.settings_service import SettingsService

        city_names = [
            n for n in request.form.getlist("reference_city_name") if (n or "").strip()
        ]
        SettingsService.set_reference_city_names(city_names)
        flash(
            "Reference cities updated. Re-enrich properties to update travel times.",
            "success",
        )
    except Exception:
        logger.error("Failed to update reference cities", exc_info=True)
        flash(
            "An error occurred while updating reference cities. Check server logs.",
            "error",
        )

    return redirect(url_for("main.criteria"))


@main_bp.route("/criteria/update_market_settings", methods=["POST"])
def update_market_settings():
    """Update market analysis settings used for AI property enrichment."""
    try:
        from models import MarketSettings

        settings = MarketSettings.get_settings()

        # Construction costs
        settings.construction_basic_min = int(
            request.form.get("construction_basic_min", 1100)
        )
        settings.construction_basic_avg = int(
            request.form.get("construction_basic_avg", 1300)
        )
        settings.construction_basic_max = int(
            request.form.get("construction_basic_max", 1500)
        )
        settings.construction_premium_min = int(
            request.form.get("construction_premium_min", 1500)
        )
        settings.construction_premium_avg = int(
            request.form.get("construction_premium_avg", 1800)
        )
        settings.construction_premium_max = int(
            request.form.get("construction_premium_max", 2200)
        )

        # Purchase costs (convert from percentage to ratio)
        settings.purchase_costs_ratio = (
            float(request.form.get("purchase_costs_ratio", 11)) / 100
        )

        # Urban adjustments (convert from percentage to ratio)
        settings.urban_vacancy_rate = (
            float(request.form.get("urban_vacancy_rate", 5)) / 100
        )
        settings.urban_operating_expenses = (
            float(request.form.get("urban_operating_expenses", 15)) / 100
        )
        settings.urban_management_fee = (
            float(request.form.get("urban_management_fee", 0)) / 100
        )

        # Suburban adjustments
        settings.suburban_vacancy_rate = (
            float(request.form.get("suburban_vacancy_rate", 8)) / 100
        )
        settings.suburban_operating_expenses = (
            float(request.form.get("suburban_operating_expenses", 15)) / 100
        )
        settings.suburban_management_fee = (
            float(request.form.get("suburban_management_fee", 0)) / 100
        )

        # Rural adjustments
        settings.rural_vacancy_rate = (
            float(request.form.get("rural_vacancy_rate", 20)) / 100
        )
        settings.rural_operating_expenses = (
            float(request.form.get("rural_operating_expenses", 18)) / 100
        )
        settings.rural_management_fee = (
            float(request.form.get("rural_management_fee", 10)) / 100
        )

        # Rental prices
        settings.urban_rental_min = int(request.form.get("urban_rental_min", 8))
        settings.urban_rental_avg = int(request.form.get("urban_rental_avg", 10))
        settings.urban_rental_max = int(request.form.get("urban_rental_max", 13))
        settings.suburban_rental_min = int(request.form.get("suburban_rental_min", 6))
        settings.suburban_rental_avg = int(request.form.get("suburban_rental_avg", 8))
        settings.suburban_rental_max = int(request.form.get("suburban_rental_max", 10))
        settings.rural_rental_min = int(request.form.get("rural_rental_min", 5))
        settings.rural_rental_avg = int(request.form.get("rural_rental_avg", 7))
        settings.rural_rental_max = int(request.form.get("rural_rental_max", 9))

        db.session.commit()
        flash(
            "Market settings updated successfully. New AI analyses will use these values.",
            "success",
        )

    except Exception:
        logger.error("Failed to update market settings", exc_info=True)
        db.session.rollback()
        flash(
            "An error occurred while updating market settings. Check server logs.",
            "error",
        )

    return redirect(url_for("main.criteria"))


@main_bp.route("/criteria/refresh_market_settings", methods=["POST"])
def refresh_market_settings():
    """Refresh market settings with current-year figures via the AI bridge.

    One user click is one Claude call on the subscription bridge. The service
    refuses a partial or out-of-bounds answer, so a failed refresh leaves the
    stored settings exactly as they were.
    """
    from services.market_settings_refresh_service import (
        MarketSettingsRefreshError,
        refresh_market_settings as run_market_refresh,
    )
    from services.subscription_transport import (
        SubscriptionTransportError,
        describe_failure,
    )

    try:
        changes, sources_note = run_market_refresh()
        if changes:
            summary = f"{len(changes)} value(s) updated"
        else:
            summary = "current values confirmed, nothing changed"
        note = f" {sources_note}" if sources_note else ""
        flash(f"Market settings refreshed via AI: {summary}.{note}", "success")
    except SubscriptionTransportError as exc:
        logger.error("Market settings AI refresh failed at the bridge: %s", exc)
        _kind, message = describe_failure(exc)
        flash(f"Market settings refresh failed: {message}", "error")
    except MarketSettingsRefreshError as exc:
        db.session.rollback()
        logger.error("Market settings AI refresh rejected: %s", exc)
        flash(
            f"Market settings refresh rejected: {exc}. Existing values were kept.",
            "error",
        )
    except Exception:
        db.session.rollback()
        logger.error("Market settings AI refresh failed", exc_info=True)
        flash(
            "An error occurred while refreshing market settings. Check server logs.",
            "error",
        )

    return redirect(url_for("main.criteria") + "#market-settings")


@main_bp.route("/land/<int:land_id>/edit-environment", methods=["GET", "POST"])
def edit_environment(land_id):
    """Edit environment data for a land"""
    try:
        land = db.get_or_404(Land, land_id)

        if request.method == "POST":
            # Update environment data
            environment = {
                "sea_view": request.form.get("sea_view") == "on",
                "mountain_view": request.form.get("mountain_view") == "on",
                "forest": request.form.get("forest") == "on",
                "orientation": request.form.get("orientation", ""),
                "buildable_floors": request.form.get("buildable_floors", ""),
                "access_type": request.form.get("access_type", ""),
                "certified_for": request.form.get("certified_for", ""),
            }

            land.environment = environment

            # Update property details if provided
            property_details = request.form.get("property_details", "").strip()
            if property_details:
                land.property_details = property_details

            db.session.commit()
            flash("Environment data updated successfully", "success")
            return redirect(url_for("main.land_detail", land_id=land_id))

        return render_template("edit_environment.html", land=land)

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to edit environment for land %s", land_id, exc_info=True)
        flash(
            "An error occurred while editing environment. Check server logs.", "error"
        )
        return redirect(url_for("main.land_detail", land_id=land_id))


@main_bp.route("/land/<int:land_id>/update-score", methods=["POST"])
def update_score(land_id):
    """Update manual score for a land"""
    try:
        land = db.get_or_404(Land, land_id)

        # Get the new score from form
        new_score = request.form.get("score")
        if new_score:
            try:
                # Guard against NaN and infinity injection - validate before conversion
                new_score_lower = new_score.lower().strip()
                if new_score_lower in ("nan", "inf", "infinity", "-inf", "-infinity"):
                    raise ValueError("NaN and infinity values not allowed")

                score_value = float(new_score)
                # Validate score is between 0 and 100
                if 0 <= score_value <= 100:
                    # Convert to Decimal for proper database storage
                    land.score_total = Decimal(str(score_value))
                    db.session.commit()
                    flash(f"Score updated to {score_value:.1f}", "success")
                else:
                    flash("Score must be between 0 and 100", "error")
            except ValueError:
                flash("Invalid score value", "error")
        else:
            flash("Score is required", "error")

        return redirect(url_for("main.land_detail", land_id=land_id))

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to update score for land %s", land_id, exc_info=True)
        flash("An error occurred while updating score. Check server logs.", "error")
        return redirect(url_for("main.land_detail", land_id=land_id))


@main_bp.route("/export.csv")
def export_csv():
    """Export current land selection to CSV"""
    try:
        from flask import make_response

        try:
            from defusedcsv import csv
        except ImportError:  # pragma: no cover
            import csv
        import io

        # Get same filters as lands page
        mode = request.args.get("mode", "combined")

        # Smart sorting defaults based on mode (same as main lands route)
        mode_sort_defaults = {
            "combined": "score_total",
            "investment": "score_investment",
            "lifestyle": "score_lifestyle",
        }
        default_sort = mode_sort_defaults.get(mode, "score_total")

        sort_by = request.args.get("sort", default_sort)
        sort_order = request.args.get("order", "desc")
        land_type_filter = request.args.get("land_type", "")
        municipality_filter = request.args.get("municipality", "")
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        sea_view_filter = request.args.get("sea_view", "") == "on"
        favorites_filter = request.args.get("favorites", "") == "on"

        # Build query with same filters - defer heavy columns for export performance
        query = Land.query.options(
            defer(Land.infrastructure_basic),
            defer(Land.infrastructure_extended),
            defer(Land.transport),
            defer(Land.environment),
            defer(Land.neighborhood),
            defer(Land.services_quality),
            defer(Land.ai_analysis),
            defer(Land.enhanced_description),
        )

        if land_type_filter:
            query = query.filter(Land.land_type == land_type_filter)

        if municipality_filter:
            query = query.filter(Land.municipality.ilike(f"%{municipality_filter}%"))

        if search_query:
            # Split search query into words for flexible matching
            # Filter out common words and short terms
            stop_words = {
                "for",
                "in",
                "the",
                "a",
                "an",
                "of",
                "to",
                "and",
                "or",
                "sale",
                "plot",
            }
            words = [w.strip(",.;:!?()[]{}") for w in search_query.split()]
            words = [
                w for w in words if w and len(w) > 1 and w.lower() not in stop_words
            ]

            if words:
                # Each word must match at least one field (title, description, or municipality)
                for word in words:
                    word_pattern = f"%{word}%"
                    query = query.filter(
                        or_(
                            Land.title.ilike(word_pattern),
                            Land.description.ilike(word_pattern),
                            Land.municipality.ilike(word_pattern),
                        )
                    )

        if investment_metrics_filter:
            query = filter_by_investment_rating(query, Land, investment_metrics_filter)

        if sea_view_filter:
            # SQLAlchemy 2.x: .astext removed; use JSON accessors
            query = query.filter(Land.environment["sea_view"].as_boolean().is_(True))

        if favorites_filter:
            query = query.filter(Land.is_favorite)

        # Apply sorting with same logic as main lands route
        if sort_by == "investment_metrics":
            rank = investment_rating_rank(Land)
            rank_order = rank.asc() if sort_order == "asc" else rank.desc()
            lands = query.order_by(
                rank_order.nullslast(),
                Land.score_total.desc().nullslast(),
                Land.id.asc(),
            ).all()
        elif hasattr(Land, sort_by):
            sort_column = getattr(Land, sort_by)
            if sort_order == "asc":
                # For ascending, NULLs go last
                lands = query.order_by(
                    sort_column.asc().nullslast(), Land.id.asc()
                ).all()
            else:
                # For descending (default for scores), NULLs go last
                lands = query.order_by(
                    sort_column.desc().nullslast(), Land.id.asc()
                ).all()
        else:
            # Fallback to mode default if invalid sort field
            fallback_column = getattr(Land, default_sort)
            if sort_order == "asc":
                lands = query.order_by(
                    fallback_column.asc().nullslast(), Land.id.asc()
                ).all()
            else:
                lands = query.order_by(
                    fallback_column.desc().nullslast(), Land.id.asc()
                ).all()

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header - include dual scores
        writer.writerow(
            [
                "ID",
                "Title",
                "URL",
                "Price (€)",
                "Area (m²)",
                "Municipality",
                "Land Type",
                "Legal Status",
                "Score Total",
                "Score Investment",
                "Score Lifestyle",
                "Latitude",
                "Longitude",
                "Created At",
            ]
        )

        # Data rows - include dual scores
        for land in lands:
            writer.writerow(
                [
                    land.id,
                    land.title,
                    land.url,
                    land.price,
                    land.area,
                    land.municipality,
                    land.land_type,
                    land.legal_status,
                    land.score_total,
                    land.score_investment,
                    land.score_lifestyle,
                    land.location_lat,
                    land.location_lon,
                    land.created_at.isoformat() if land.created_at else "",
                ]
            )

        # Create response
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv"
        response.headers["Content-Disposition"] = (
            "attachment; filename=idealista_lands.csv"
        )

        return response

    except Exception:
        logger.error("Failed to export CSV", exc_info=True)
        flash("An error occurred while exporting CSV. Check server logs.", "error")
        return redirect(url_for("main.lands"))


@main_bp.route("/properties/export.csv")
def export_properties_csv():
    """Export current property selection to CSV (profile-aware)."""
    try:
        from flask import make_response

        try:
            from defusedcsv import csv
        except ImportError:  # pragma: no cover
            import csv
        import io
        import re

        from services.search_profile_service import SearchProfileService

        # Unassigned, for its side effect and like /properties does it: the
        # catch-all is ensured to exist, and nothing here needs to hold it now
        # that the fallback is `all` rather than a profile picked for the
        # reader.
        SearchProfileService.get_default_profile(create=True)
        # Visible, like /properties -- an export of "all subscriptions" that
        # carried the hidden ones would disagree with the page it was taken
        # from (2026-08-17).
        profiles = SearchProfileService.list_visible_profiles(active_only=True)

        # Same profile_id contract as /properties (auto | all | selected(ids))
        # and the same fallback, which is `all` -- the owner's decision of
        # 2026-08-09 that a bare listing surface shows every live subscription
        # at once, rather than one picked for the reader.
        #
        # This comment claimed exactly that while the code did the opposite: it
        # resolved a bare export to a single auto-selected profile. Measured on
        # production 2026-08-31, `/properties` showed 386 listings and
        # `/properties/export.csv` handed over **2** -- the catch-all's whole
        # contents -- with nothing on either surface saying they disagreed. The
        # page's own Export button carries `profile_id=all`, so this only ever
        # bit the bare URL, which is the one a person types or bookmarks.
        selection = parse_profile_selection(request.args)
        if selection.is_auto:
            selection = ProfileSelection(ProfileSelectionState.ALL)
        profile_selection = resolve_profile_selection(
            selection, [profile.id for profile in profiles]
        )
        selected_profile_id = profile_selection.single_id

        category_filter = request.args.get("category", "")
        subtype_filter = request.args.get("subtype", "")
        municipality_filter = request.args.get("municipality", "")
        source_filter = request.args.get("source", "")
        advertiser_filter = request.args.get("advertiser", "")
        verdict_filter = request.args.get("verdict", "")
        action_filter = request.args.get("action", "")
        # One date for the whole request. `overdue` is a due date compared
        # against today, and the badge, the filter, the count beside its option
        # and both serializers have to compare against the *same* today or they
        # disagree for the few minutes a day nobody is watching
        # (services/owner_review.py).
        review_today = owner_review.today()
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        favorites_filter = request.args.get("favorites", "") == "on"
        sea_view_filter = request.args.get("sea_view", "")
        sea_distance_filter = request.args.get("sea_dist", "")
        build_filter = request.args.get("build", "")
        # The export did not read this one at all, so the Export CSV button on
        # a `measured=full` page exported the whole subscription: measured
        # against production 2026-08-20, the page found 72 listings and its own
        # export returned 471 rows.
        measured_filter = request.args.get("measured", "")
        criteria_filter = request.args.get("criteria", "")
        # Likeness to the subscription's favorites: the filter, the sort and
        # the columns below all read the export's one context
        # (services/favorite_similarity.py), built here because the columns
        # need a reading per row whether or not the cut is on.
        similar_filter = request.args.get("similar", "")
        similarity_ctx = favorite_similarity.build_context(
            profile_ids=_similarity_scope(profile_selection)
        )

        # The same reading as /properties, from the same module, so an export
        # and the page it was taken from cannot disagree about which listings
        # are in scope. The Export CSV link states `hide_removed` outright in
        # both directions, so for a page-drawn export the default below is
        # never consulted.
        hide_removed_filter = resolve_hide_removed(request.args)

        sort_by = request.args.get("sort", "created_at")
        sort_order = request.args.get("order", "desc")

        query = apply_profile_filter(
            Property.query.options(
                defer(Property.description),
                defer(Property.enhanced_description),
                defer(Property.ai_analysis),
            ),
            Property.search_profile_id,
            profile_selection,
        )

        if category_filter:
            if category_filter == UNCLASSIFIED_FILTER:
                query = query.filter(_unclassified_clause(Property.property_category))
            else:
                query = query.filter(Property.property_category == category_filter)
        if subtype_filter:
            if subtype_filter == UNCLASSIFIED_FILTER:
                query = query.filter(_unclassified_clause(Property.property_subtype))
            else:
                query = query.filter(Property.property_subtype == subtype_filter)
        if municipality_filter:
            query = query.filter(municipality_filter_clause(municipality_filter))
        # Which site the listing is on. utils/listing_source.py owns the
        # reading, so the four surfaces cannot drift apart on it.
        source_clause = source_filter_clause(Property, source_filter)
        if source_clause is not None:
            query = query.filter(source_clause)
        # Who is selling. services/advertiser.py owns the reading, so the
        # badge, this filter and the count printed beside its option are one
        # answer rather than three.
        advertiser_clause = advertiser.filter_clause(Property, advertiser_filter)
        if advertiser_clause is not None:
            query = query.filter(advertiser_clause)
        # What the owner decided, and what is still outstanding.
        # services/owner_review.py owns both readings, so the badge, these two
        # filters and the counts beside their options are one answer rather
        # than several. Both filters are applied here rather than one of them
        # here and the other elsewhere: a surface that keeps one parameter and
        # drops the other is the regression these two are tested against
        # together.
        verdict_clause = owner_review.decision_filter_clause(Property, verdict_filter)
        if verdict_clause is not None:
            query = query.filter(verdict_clause)
        action_clause = owner_review.action_filter_clause(
            Property, action_filter, review_today
        )
        if action_clause is not None:
            query = query.filter(action_clause)
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)

        if investment_metrics_filter:
            query = filter_by_investment_rating(
                query, Property, investment_metrics_filter
            )

        if sea_view_filter:
            query = filter_by_sea_view(query, Property, sea_view_filter)

        if sea_distance_filter:
            query = filter_by_sea_distance(query, Property, sea_distance_filter)

        if build_filter:
            query = filter_by_land_classification(query, Property, build_filter)

        if measured_filter:
            query = filter_by_measured(query, Property, measured_filter)

        if similar_filter:
            query = favorite_similarity.apply_filter(
                query, Property, similarity_ctx, similar_filter
            )

        # The same criteria reading as the page and the map: an export of the
        # visible list must not smuggle the hidden fails back in, and
        # criteria=all must widen it the same way. The context is kept, not
        # discarded, because the rows below have to SAY which verdict they
        # carry -- the filter alone leaves a file that dropped 59 of 443 rows
        # (production, 2026-08-31) and cannot be used to work out which.
        criteria_ctx = _criteria_context()
        query, _ = subscription_criteria.apply_filter(
            query, criteria_ctx, criteria_filter
        )

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )

        # Same ordering as /properties, tiebreaker included: the export link
        # forwards whatever the page is sorted by, so an allow-list that does
        # not know a value would hand back the same rows in another order.
        # The same version the page sorts against, read once for the export.
        taste_version = taste_service.current_profile_version()
        sort_columns = {
            "title": Property.title,
            "created_at": Property.created_at,
            "price": Property.price,
            "area": Property.area,
            "score_total": Property.score_total,
            "score_investment": Property.score_investment,
            "score_lifestyle": Property.score_lifestyle,
            "travel_time_nearest_beach": _nearest_beach_minutes(Property),
            # Same expression as the page (#498), so the export link carrying
            # sort=taste_score hands back the page's order and not a silent
            # fall-back to created_at.
            "taste_score": taste_service.sortable_score_expression(
                Property, taste_version
            ),
            # Same reading as the page, so an export link carrying
            # sort=similarity hands back the page's order.
            "similarity": favorite_similarity.sort_expression(Property, similarity_ctx),
        }
        if sort_by == "investment_metrics":
            rank = investment_rating_rank(Property)
            rank_order = rank.asc() if sort_order == "asc" else rank.desc()
            props = query.order_by(
                rank_order.nullslast(),
                Property.score_total.desc().nullslast(),
                Property.id.asc(),
            ).all()
        else:
            sort_column = sort_columns.get(sort_by, Property.created_at)
            if sort_order == "asc":
                props = query.order_by(
                    sort_column.asc().nullslast(), Property.id.asc()
                ).all()
            else:
                props = query.order_by(
                    sort_column.desc().nullslast(), Property.id.asc()
                ).all()

        travel_display_targets = []
        if selected_profile_id is not None:
            try:
                selected_profile = db.session.get(SearchProfile, selected_profile_id)
            except Exception:
                selected_profile = None

            travel_config = SearchProfileService.get_travel_targets_config(
                selected_profile
            )
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = (
                travel_config.get("presets") if isinstance(travel_config, dict) else {}
            )

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = (
                    bool((presets_cfg.get(key) or {}).get("enabled", True))
                    if isinstance(presets_cfg, dict)
                    else True
                )
                if not enabled:
                    continue
                travel_display_targets.append(
                    {"key": key, "label": preset.get("label") or key}
                )

            for item in (
                (travel_config.get("custom") or [])
                if isinstance(travel_config, dict)
                else []
            ):
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {"key": f"custom:{target_id}", "label": name}
                )

        def _csv_key(value: str) -> str:
            return (
                re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "")).strip("_") or "target"
            )

        output = io.StringIO()
        writer = csv.writer(output)

        base_header = [
            "ID",
            "Profile ID",
            "Title",
            "URL",
            "Price (EUR)",
            "Area (m²)",
            "Area Type",
            # The parcel, where a portal stated one (migration 025). It is the
            # figure the criteria verdict beside it rests on and it was in no
            # column at all, so the export could neither state the verdict nor
            # let a reader recompute it. Blank is "nobody stated a plot",
            # never "no plot": on production 2026-08-31 that is 436 of the 443
            # rows of the one subscription that carries criteria, which is why
            # `unknown` is the common verdict and not a disappointing `fail`.
            #
            # Blank is NOT, however, "the criteria could not judge the plot".
            # On a bare-land row the module makes `area` the parcel when
            # `plot_area` is absent, so this cell can be empty beside a
            # measured `fail` -- property 1458 on production is exactly that
            # (`area_type=plot, area=150, plot_area NULL -> fail`). The
            # recompute this column exists to support therefore keys on
            # `Criteria`, and reading a blank here as "unjudged" is the
            # mistake the sentence above invites.
            "Plot Area (m²)",
            # This export APPLIES the criteria filter, so by default it omits
            # the measured fails the owner has not judged -- and until this
            # column it said nothing about having done so. Four states, from
            # the one reading the page and the map use
            # (services/subscription_criteria.py): `fail` is a MEASURED
            # shortfall, `pass` needs every bound measured and met, `unknown`
            # is a figure nobody stated, and `no_criteria` is a subscription
            # that sets no bounds. `unknown` is never folded into `fail` -- a
            # plot nobody has stated is not a plot that is too small.
            #
            # With this column an export taken at `criteria=all` says which
            # rows the default drops: `fail`, minus the ones Favorite, Owner
            # Verdict and Next Action State exempt -- all three already
            # columns here. That arithmetic is a disclosure and not a
            # guarantee, in one known direction: the hide reads
            # `owner_verdict IS NULL` while the column states the *verdict*,
            # which is `undecided` for a NULL and for a string no writer of
            # this application produces alike. A row carrying such a string is
            # KEPT and the recompute would name it as dropped -- over-naming,
            # which is the safe direction for a reader checking what is
            # missing.
            "Criteria",
            "Price per m²",
            "Bedrooms",
            "Bathrooms",
            "Municipality",
            "Category",
            "Subtype",
            # The verdict, not the raw column: an ingested row exports
            # `unchecked`, because exporting its default as `active` is what
            # let a report recommend a listing that had been withdrawn months
            # earlier. The two columns after it carry the provenance, the way
            # Sea View carries its own below.
            "Status",
            "Status Source",
            "Status Checked At",
            # Who is selling, and what established it. `unchecked` is exported
            # as itself: a report that read a blank as "agency" would be the
            # same defect as the status column's, one column over.
            "Advertiser",
            "Advertiser Source",
            "Favorite",
            "Sea View",
            "Sea View Source",
            # What the verdict was measured to, not just how far away it was.
            # A buyer comparing "sea view" rows needs to be able to tell a
            # clifftop above open water from a plot looking up an estuary
            # channel, and the state alone cannot say which (#334). Empty on
            # rows whose verdict predates the target being recorded.
            "Sea View Distance (m)",
            "Sea View Target Lat",
            "Sea View Target Lon",
            "Latitude",
            "Longitude",
            # Which rows the travel columns below actually describe. An
            # `approximate` coordinate is a locality centroid, so those
            # durations are routes from the village, not from the parcel --
            # and a spreadsheet sorting on them cannot tell without this.
            "Location Accuracy",
            # The hazard scan (#437). `Hazards` is the *verdict*, not a count:
            # `none_within_radius` is a measurement and `not_scanned` is not,
            # and a spreadsheet that folded the two into an empty cell would
            # rebuild the defect the block exists to remove. The distance is
            # blank on an approximate row and the min/max pair carries the
            # band instead, for the same reason the page never prints a point
            # distance from a locality centroid.
            "Hazards",
            # An `ok` from a scan that hit Overpass's element cap is a short
            # list, not a complete one. The card says so; a spreadsheet
            # sorting on the columns below could not tell without this.
            "Hazard Scan Complete",
            "Hazard Facilities",
            "Nearest Hazard",
            "Nearest Hazard Kind",
            "Nearest Hazard Severity",
            "Nearest Hazard Distance (m)",
            "Nearest Hazard Distance Min (m)",
            "Nearest Hazard Distance Max (m)",
            "Nearest Hazard Bearing",
            # What the owner decided and what is still outstanding. The
            # decision column says `undecided` where nobody has judged the
            # listing -- never blank and never `rejected`, because a report
            # built off a blank cell reads "nobody looked" as "looked and said
            # no" (services/owner_review.py).
            "Owner Verdict",
            "Owner Verdict Reason",
            "Next Action",
            "Next Action Due",
            "Next Action State",
            # The taste ranking (#498). State says whether the score is about
            # the CURRENT profile (`ok`), an earlier one (`stale`) or absent
            # (`none`) -- a spreadsheet sorting the score column could not
            # tell a v1 number from a v3 one without it.
            "Taste Score",
            "Taste State",
            "Taste Profile Version",
            "Taste Scored At",
            # Likeness to the subscription's favorites
            # (services/favorite_similarity.py). The state says whether the
            # number ranks (`ok`), is a favorite itself (`reference`), or
            # rests on too little to rank (`thin`) -- a spreadsheet sorting
            # the score column could not tell without it -- and the last
            # three columns say what it was measured against and on.
            "Similarity",
            "Similarity State",
            "Similarity Nearest Favorite",
            "Similarity Compared On",
            "Similarity Location Basis",
            "Created At",
        ]

        travel_headers = []
        for tt in travel_display_targets:
            key = tt.get("key") or ""
            sk = _csv_key(key)
            travel_headers.extend(
                [
                    f"travel_{sk}_duration_min",
                    f"travel_{sk}_distance_km",
                    f"travel_{sk}_mode",
                    f"travel_{sk}_place",
                ]
            )

        writer.writerow(base_header + travel_headers)

        for prop in props:
            price = float(prop.price) if prop.price else None
            area = float(prop.area) if prop.area else None
            price_per_m2 = (price / area) if (price is not None and area) else None

            attrs = prop.attributes if isinstance(prop.attributes, dict) else {}
            bedrooms = attrs.get("bedrooms") if isinstance(attrs, dict) else None
            bathrooms = attrs.get("bathrooms") if isinstance(attrs, dict) else None
            sea_view_verdict = sea_view_service.read_verdict(prop)
            sea_view_target = sea_view_verdict["target"]
            listing_verdict_row = listing_verdict(prop)
            advertiser_verdict = advertiser.read_verdict(prop)
            # Restated against the row's *current* accuracy, exactly as the
            # page does it -- an export read off the stored block would print
            # a point distance for a locality centroid the page refuses to.
            hazards = hazard_verdict(prop)
            hazard_nearest = hazards["nearest"]
            # The request's one date, so a row exported at 23:59 Madrid is
            # described against the same day the filter selected it on.
            review_action = owner_review.read_action(prop, review_today)
            # The export's one profile version, threaded per row the same way.
            taste_row = taste_service.read_taste(prop, taste_version)
            # The row's verdict against ITS OWN subscription's bounds, from
            # the context the filter above was built from -- so the column and
            # the row set it explains cannot be answers to two questions.
            criteria_row = subscription_criteria.row_verdict(prop, criteria_ctx)
            # The export's one similarity reading, per row.
            similarity_row = (
                similarity_ctx.read(prop.id)
                if similarity_ctx is not None
                else {"state": favorite_similarity.STATE_NO_REFERENCE, "score": None}
            )

            row = [
                prop.id,
                prop.search_profile_id,
                prop.title,
                prop.url,
                price,
                area,
                prop.area_type,
                # `is not None`, not truthiness: fotocasa writes 0 where it
                # has no figure, and the criteria reader treats that 0 as a
                # blank -- but the export states what is STORED and lets the
                # verdict column say how it was read. Blanking it here would
                # make "the portal said nothing" and "the portal said zero"
                # the same cell.
                float(prop.plot_area) if prop.plot_area is not None else None,
                criteria_row["state"],
                price_per_m2,
                bedrooms,
                bathrooms,
                prop.municipality,
                prop.property_category,
                prop.property_subtype,
                listing_verdict_row["state"],
                listing_verdict_row["source"] or "",
                listing_verdict_row["checked_at"].isoformat()
                if listing_verdict_row["checked_at"]
                else "",
                advertiser_verdict["state"],
                advertiser_verdict["source"] or "",
                bool(prop.is_favorite),
                sea_view_verdict["state"],
                sea_view_verdict["source"],
                sea_view_verdict["distance_m"],
                sea_view_target["lat"] if sea_view_target else None,
                sea_view_target["lon"] if sea_view_target else None,
                float(prop.location_lat) if prop.location_lat else None,
                float(prop.location_lon) if prop.location_lon else None,
                prop.location_accuracy or "unknown",
                hazards["status"],
                # `complete`, the same fact the coverage line counts, and
                # deliberately not gated on `measured`: a block taken before
                # the listing moved is still a complete scan, and blanking the
                # cell there made the export disagree with the count above it
                # (codex review, 2026-08-20).
                hazards["complete"],
                hazards["item_count"] if hazards["measured"] else None,
                hazard_nearest.get("name") if hazard_nearest else None,
                hazard_nearest.get("kind") if hazard_nearest else None,
                hazard_nearest.get("severity") if hazard_nearest else None,
                hazard_nearest.get("distance_m") if hazard_nearest else None,
                hazard_nearest.get("min_distance_m") if hazard_nearest else None,
                hazard_nearest.get("max_distance_m") if hazard_nearest else None,
                hazard_nearest.get("cardinal") if hazard_nearest else None,
                owner_review.read_decision(prop)["state"],
                prop.owner_verdict_reason or "",
                prop.next_action or "",
                prop.next_action_due_on.isoformat() if prop.next_action_due_on else "",
                review_action["state"],
                taste_row["score"] if taste_row["state"] != "none" else None,
                taste_row["state"],
                taste_row.get("profile_version"),
                taste_row.get("scored_at") or "",
                similarity_row.get("score")
                if similarity_row["state"]
                in (
                    favorite_similarity.STATE_OK,
                    favorite_similarity.STATE_REFERENCE,
                    favorite_similarity.STATE_THIN,
                )
                else None,
                similarity_row["state"],
                similarity_row.get("reference_id"),
                " ".join(similarity_row.get("compared") or []),
                similarity_row.get("geography_basis") or "",
                prop.created_at.isoformat() if prop.created_at else "",
            ]

            travel = prop.travel if isinstance(prop.travel, dict) else {}
            targets = travel.get("targets") if isinstance(travel, dict) else None
            targets = targets if isinstance(targets, dict) else {}

            for tt in travel_display_targets:
                key = tt.get("key")
                tdata = targets.get(key) if key else None
                if not isinstance(tdata, dict):
                    row.extend([None, None, None, None])
                    continue
                duration_min = tdata.get("duration_min")
                distance_km = tdata.get("distance_km")
                mode = tdata.get("mode")
                place = None
                place_data = tdata.get("place")
                if isinstance(place_data, dict):
                    place = place_data.get("name")
                row.extend([duration_min, distance_km, mode, place])

            writer.writerow(row)

        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv"
        response.headers["Content-Disposition"] = (
            "attachment; filename=idealista_properties.csv"
        )
        return response

    except Exception:
        logger.error("Failed to export properties CSV", exc_info=True)
        flash("An error occurred while exporting CSV. Check server logs.", "error")
        return redirect(url_for("main.properties"))


@main_bp.route("/healthz")
def health_check():
    """Health check endpoint"""
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Importing listings by link (fotocasa)
# ---------------------------------------------------------------------------
#
# Two routes, because this application cannot delete a property: a tree-wide
# search finds no delete route and no `db.session.delete` on `Property`, so a
# row created from a misread page stays in the table, in the `/municipalities`
# medians and in the comparable pool of its subscription. The preview is the
# only undo there is, and it is therefore not optional.
#
# The read runs as a background job and the write runs in the request, which
# is the opposite of where the work looks like it belongs. It is the right way
# round: ninety links at the courtesy pace is four and a half minutes against
# one gunicorn worker with four threads and a thirty-second default timeout,
# while the write touches nothing but the database.


def _import_profiles():
    """Subscriptions offered as the destination, live ones first.

    Required rather than defaulted to nothing: `search_profile_id` NULL means
    "unassigned", which a bare /properties does not show at all -- the owner
    would import a listing and not find it. It is also what decides whose
    comparable pool the row joins (`services/property_comparables.py` scopes
    on it), so it is a real choice, not a formality.

    Hidden subscriptions are not offered, for exactly that reason: importing
    into one would file the listing where the page does not show it, which is
    the same "imported it and cannot find it" the paragraph above is about.
    The archived ones stay -- they are offered everywhere else too.
    """
    from services.search_profile_service import SearchProfileService

    return (
        SearchProfile.query.filter(SearchProfileService.visible_clause())
        .order_by(SearchProfile.is_active.desc(), SearchProfile.name.asc())
        .all()
    )


@main_bp.route("/properties/import", methods=["GET"])
def import_listings():
    """Paste links, then look at what the pages said before anything is written."""
    from services.background_jobs import get_job

    job_id = (request.args.get("job") or "").strip() or None
    job = get_job(job_id) if job_id else None

    rows = []
    job_state = None
    if job:
        job_state = job.get("status")
        result = job.get("result")
        if isinstance(result, dict):
            rows = result.get("rows") or []

    return render_template(
        "property_import.html",
        profiles=_import_profiles(),
        job_id=job_id,
        job_state=job_state,
        rows=rows,
        selected_profile_id=request.args.get("profile_id", type=int),
    )


@main_bp.route("/properties/import", methods=["POST"])
def import_listings_read():
    """Fetch and read every pasted link. Writes nothing at all."""
    from services.fotocasa_import import read_urls
    from services.fotocasa_source import split_urls

    urls = split_urls(request.form.get("urls"))

    # Reading needs no destination: `read_urls` does not take one, and asking
    # for it here would put the same control on the page twice. It is asked
    # once, at the moment it is used -- next to the button that writes.
    if not urls:
        flash("Paste at least one fotocasa listing link.", "error")
        return redirect(url_for("main.import_listings"))
    if len(urls) > MAX_IMPORT_URLS:
        flash(
            f"{len(urls)} links pasted; this imports at most {MAX_IMPORT_URLS} "
            "at a time.",
            "error",
        )
        return redirect(url_for("main.import_listings"))

    app_obj = current_app._get_current_object()

    def _run():
        with app_obj.app_context():
            return {"rows": read_urls(urls)}

    from services.background_jobs import enqueue_job

    # Keyed on the links themselves, so a double press -- or a reload of the
    # POST -- joins the run already fetching instead of asking fotocasa for
    # the same ninety pages a second time. `enqueue_job` returns the live
    # job's id for a key already claimed, so the redirect below lands on the
    # preview either way.
    dedupe_key = (
        "fotocasa_import:"
        + hashlib.sha256("\n".join(sorted(urls)).encode("utf-8")).hexdigest()
    )

    job_id = enqueue_job(
        _run,
        job_type="fotocasa_import_read",
        meta={"count": len(urls)},
        app=app_obj,
        dedupe_key=dedupe_key,
    )
    return redirect(url_for("main.import_listings", job=job_id))


@main_bp.route("/properties/import/confirm", methods=["POST"])
def import_listings_confirm():
    """Create the rows the owner just looked at."""
    from services.background_jobs import get_job
    from services.fotocasa_import import insert_rows

    job_id = (request.form.get("job_id") or "").strip()
    profile_id = request.form.get("profile_id", type=int)
    job = get_job(job_id) if job_id else None

    if not job or job.get("status") != "success":
        flash(
            "That import preview is no longer available. Paste the links again.",
            "error",
        )
        return redirect(url_for("main.import_listings"))
    if not profile_id:
        flash("Choose which subscription the listings go into.", "error")
        return redirect(url_for("main.import_listings", job=job_id))

    result = job.get("result") or {}
    rows = result.get("rows") or []

    try:
        outcome = insert_rows(rows, profile_id=profile_id)
    except Exception:
        db.session.rollback()
        logger.error("Importing fotocasa listings failed", exc_info=True)
        flash("Import failed. Check server logs.", "error")
        return redirect(url_for("main.import_listings", job=job_id))

    created = outcome["created"]
    if not created:
        flash("Nothing new to add — every link was already here.", "success")
        return redirect(url_for("main.import_listings"))

    flash(
        f"Added {len(created)} listing(s). Press Enrich on each to measure "
        "travel, sea and amenities.",
        "success",
    )
    return redirect(
        url_for("main.properties", profile_id=profile_id, source="fotocasa")
    )
