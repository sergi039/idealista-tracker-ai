import logging
import math
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    session,
    url_for,
    flash,
    jsonify,
)

# get_or_404 raises HTTPException, and the blanket `except Exception` handlers
# below would answer 500 for it: every one of them re-raises it first so an
# unknown id stays a 404 (issue #136).
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_, case, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer
from models import Land, Property, SearchProfile
from app import db
from services import sea_view_service
from services.coordinate_quality import shared_coordinate_peers
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
from utils.listing_search import listing_search_clause
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
}
PROPERTY_VIEW_TYPES = ("cards", "list")
# The table is what a bare /properties opens on (owner decision, 2026-08-09):
# it puts price, area, travel and date side by side, which the cards cannot.
DEFAULT_PROPERTY_VIEW_TYPE = "list"
# A bare /properties must open on the freshest listings, so the mode default
# only applies once the user actually picks a mode.
DEFAULT_PROPERTY_SORT = "created_at"

# Investment ratings the AI analysis emits, ordered worst to best. The rank is
# what "sort by Inv. Metr." orders on; the keys are what the filter accepts.
INVESTMENT_RATING_ORDER = ("BELOW", "MODERATE", "GOOD", "EXCELLENT")

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


def _investment_rating_expr(model):
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


def _nearest_beach_minutes(model):
    """Drive minutes to the nearest measured beach; NULL without one.

    Items are nearest-first (services/property_travel_service.py sorts and
    dedupes them), so element 0 IS the nearest beach. Shared by the list sort
    and the CSV export so the two allow-lists cannot drift apart. Live again
    per issue #271 — the #98 placeholder said "not one row holds a travel
    time", and the Phase-2 backfill made that false.
    """
    return model.travel["beaches"]["items"][0]["duration_min"].as_float()


def _filter_by_investment_rating(query, model, raw_value):
    """Keep only rows whose investment rating starts with `raw_value`."""
    wanted = (raw_value or "").strip().upper()
    if wanted not in INVESTMENT_RATING_ORDER:
        return query
    return query.filter(_investment_rating_expr(model).like(f"{wanted}%"))


def _investment_rating_rank(model):
    """Sortable rank for the investment rating; NULL when there is none."""
    rating = _investment_rating_expr(model)
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


def _sea_view_state_expr(model):
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


def _score_coverage_share_expr(model):
    """`scoring.coverage.share` as a float; NULL where it was never recorded.

    #379. Recorded by `PropertyScoringService` from this change on; a row
    scored before it has no value here and therefore does not pass a
    "fully measured" filter until it is rescored -- the honest reading, since
    the SQL cannot re-derive the share the way `score_coverage()` does in
    Python, and "unknown coverage" must not pass as "full".
    """
    return model.scoring["coverage"]["share"].as_float()


def _filter_by_measured(query, model, raw_value):
    """`measured=full` keeps rows whose every enabled criterion answered."""
    if (raw_value or "").strip().lower() != "full":
        return query
    return query.filter(_score_coverage_share_expr(model) >= 0.999)


def _filter_by_sea_view(query, model, raw_value):
    """Keep only rows whose sea-view verdict is in the requested bucket."""
    wanted = SEA_VIEW_FILTER_VALUES.get((raw_value or "").strip().lower())
    if not wanted:
        return query
    return query.filter(_sea_view_state_expr(model).in_(wanted))


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

    Deliberately different from `/properties`, which takes the richest active
    profile: a map is useless without coordinates, so the profile with the
    most mappable rows wins. When nothing has coordinates yet it falls back to
    the most recently active profile, then the default, then the first one.

    Because the two pages resolve differently, whatever this returns has to
    travel in the link back to the list (`ResolvedProfileSelection.
    link_values`) or the user lands on a different subscription than the map
    just showed -- and the focused listing may not even be loaded there.
    """
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
    dropped = {"profile_id"}
    if not keep_filters:
        dropped |= {
            "category",
            "subtype",
            "municipality",
            "search",
            "inv_metr",
            "sea_view",
            "favorites",
        }
    args = {
        key: (values[0] if len(values) == 1 else values)
        for key, values in request.args.lists()
        # `url_for` reads `endpoint` and every `_`-prefixed name as its own
        # argument, so a query string carrying one would raise here instead of
        # travelling. They are not filters; dropping them costs nothing.
        if key not in dropped and key != "endpoint" and not key.startswith("_")
    }
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


def _profile_dropdown_options(profiles, resolved):
    """Rows for the subscription filter: the live subscriptions first, the
    retired ones after them, each with how many listings it holds.

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
    counts = {
        profile_id: count
        for profile_id, count in db.session.query(
            Property.search_profile_id, func.count(Property.id)
        ).group_by(Property.search_profile_id)
    }

    active_ids = {profile.id for profile in profiles}
    selected = set(resolved.checked_ids)

    # One query for the archive too -- inactive profiles are not in `profiles`,
    # which `SearchProfileService.list_profiles(active_only=True)` filtered.
    known = {profile.id: profile for profile in profiles}
    for profile in SearchProfile.query.filter(
        SearchProfile.is_active.isnot(True)
    ).all():
        known.setdefault(profile.id, profile)
    for profile in SearchProfile.query.filter(
        SearchProfile.id.in_(selected - set(known))
    ).all():
        known.setdefault(profile.id, profile)

    options = []
    for profile_id, profile in known.items():
        count = counts.get(profile_id, 0)
        if not count and profile_id not in selected:
            continue
        options.append(
            {
                "id": profile_id,
                "name": profile.name,
                "is_active": profile_id in active_ids,
                "count": count,
            }
        )

    # An id the selection names that no longer exists at all: it still needs a
    # checkbox, or the selection silently widens on the next Apply.
    for profile_id in selected - set(known):
        options.append(
            {
                "id": profile_id,
                "name": f"Unknown profile #{profile_id}",
                "is_active": False,
                "count": counts.get(profile_id, 0),
            }
        )

    # Live subscriptions first, then the archive; alphabetical inside each
    # group so the order does not shuffle when a listing arrives.
    options.sort(key=lambda option: (not option["is_active"], option["name"].lower()))
    return options


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


def _municipality_choices(profile_selection, applied=""):
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

    Counts follow the same selection as the options themselves -- the
    subscriptions on screen, not the other filters -- so the number beside a
    name answers "how many listings does this municipality have here", the
    same question the subscription chips answer.
    """
    rows = apply_profile_filter(
        db.session.query(Property.municipality, func.count(Property.id)).group_by(
            Property.municipality
        ),
        Property.search_profile_id,
        profile_selection,
    ).all()

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


def _property_filter_options(
    profile_selection,
    category_filter="",
    subtype_filter="",
    municipality_filter="",
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
    """
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
        "municipalities": _municipality_choices(profile_selection, municipality_filter),
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
        profiles = SearchProfileService.list_profiles(active_only=True)

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
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        favorites_filter = request.args.get("favorites", "") == "on"
        sea_view_filter = request.args.get("sea_view", "")
        measured_filter = request.args.get("measured", "")

        # Hide removed: ON by default (similar to /lands)
        hide_removed_param = request.args.get("hide_removed", None)
        form_submitted = any(
            request.args.get(p)
            for p in [
                "profile_id",
                "category",
                "subtype",
                "municipality",
                "search",
                "inv_metr",
                "sea_view",
                "measured",
                "sort",
                "order",
                "favorites",
                "hide_removed",
            ]
        )
        if form_submitted:
            hide_removed_filter = hide_removed_param == "on"
        else:
            hide_removed_filter = True

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
        if request.args.get("mode"):
            default_sort = PROPERTY_MODE_SORT_DEFAULTS[mode]
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
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)

        if investment_metrics_filter:
            query = _filter_by_investment_rating(
                query, Property, investment_metrics_filter
            )

        if sea_view_filter:
            query = _filter_by_sea_view(query, Property, sea_view_filter)
        if measured_filter:
            query = _filter_by_measured(query, Property, measured_filter)

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )

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
        query = apply_profile_filter(
            query, Property.search_profile_id, profile_selection
        )

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
        }
        if sort_by not in sort_columns and sort_by != "investment_metrics":
            sort_by = default_sort

        if sort_by == "investment_metrics":
            rank = _investment_rating_rank(Property)
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
        else:
            active_mode = mode

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # A page past the end renders the last one instead of an empty table
        # under a correct "of N results" count. Reachable from the page-size
        # control (which used to carry `page` across) and from any stale link.
        if pagination.pages and page > pagination.pages:
            page = pagination.pages
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)

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
            category_filter=category_filter,
            subtype_filter=subtype_filter,
            municipality_filter=municipality_filter,
        )

        return render_template(
            "properties.html",
            properties=pagination.items,
            pagination=pagination,
            profiles=profiles,
            profile_options=_profile_dropdown_options(profiles, profile_selection),
            profile_names=profile_names,
            unassigned_count=unassigned_count,
            max_selected_profiles=MAX_SELECTED_PROFILE_IDS,
            selected_profile_id=selected_profile_id,
            profile_selection=profile_selection,
            travel_display_targets=travel_display_targets,
            listing_verified_count=listing_verified_count,
            **filter_options,
            current_filters={
                # A list, so `url_for` repeats the parameter instead of
                # stringifying it -- every in-page link is rebuilt from here.
                "profile_id": list(profile_selection.link_values),
                "category": category_filter,
                "subtype": subtype_filter,
                "municipality": municipality_filter,
                "search": search_query,
                "inv_metr": investment_metrics_filter,
                "sea_view": sea_view_filter,
                "measured": measured_filter,
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
            profile_names={},
            # The page failed before it could count anything; claiming a number
            # here would be worse than the missing disclosure.
            unassigned_count=0,
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

        return render_template(
            "property_detail.html",
            property=prop,
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
            sea_view_verdict=sea_view_service.read_verdict(prop),
            # One row, one query, and only on this page: the list would run it
            # per row. It is evidence about the coordinate, next to the
            # coordinate, and nothing scores on it.
            shared_coordinate_ids=shared_coordinate_peers(prop),
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
    """List search profiles (MVP; editing comes later)."""
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
            from services.property_scoring_service import PropertyScoringService

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

            # Turning the pool criterion ON is the one save that re-scores
            # every listing in the subscription by design (proposal D17, the
            # agreed weight-0 shipping rule): it must not happen from a save
            # that merely *looked* like the others. The transition to a
            # positive pool weight therefore shows a dry-run preview first
            # and commits only on the explicit confirm below.
            def _pool_weight_enabled(cats: dict) -> bool:
                # Every level is isinstance-guarded: `categories` can hold a
                # hand-written category whose branch is a scalar (#239 keeps
                # unmanaged keys), and a crash here would take the whole save
                # down (diff review, 2026-08-14).
                if not isinstance(cats, dict):
                    return False
                for cat_cfg in cats.values():
                    if not isinstance(cat_cfg, dict):
                        continue
                    for branch in ("investment", "lifestyle"):
                        branch_cfg = cat_cfg.get(branch)
                        if not isinstance(branch_cfg, dict):
                            continue
                        weight = branch_cfg.get("pool_score")
                        if (
                            isinstance(weight, (int, float))
                            and not isinstance(weight, bool)
                            and weight > 0
                        ):
                            return True
                return False

            stored_before = (
                profile.scoring_config
                if isinstance(profile.scoring_config, dict)
                else {}
            )
            pool_turning_on = _pool_weight_enabled(categories) and not (
                _pool_weight_enabled(stored_before.get("categories") or {})
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
                    row_changed = False
                    for old_value, new_value in zip(old, new):
                        if (old_value is None) != (new_value is None):
                            row_changed = True
                        elif old_value is not None and new_value is not None:
                            if abs(float(new_value) - float(old_value)) >= 0.05:
                                row_changed = True
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
                    "Pool criterion preview: "
                    f"{changed} of {len(before)} listings would change score "
                    f"(mean total shift {mean_delta:+.1f}). Nothing is saved "
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
                f"Pool criterion enabled; {rescored} listings rescored.",
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
            TRAVEL_STATE_APPROXIMATE_ORIGIN,
            TRAVEL_STATE_UNAVAILABLE,
            PropertyTravelService,
            travel_api_state,
        )

        service = PropertyTravelService()
        updated = 0
        api_refused = 0
        approximate_origin = 0
        for prop in properties:
            try:
                if service.calculate_for_property(prop, commit=True):
                    updated += 1
                elif travel_api_state(prop) == TRAVEL_STATE_APPROXIMATE_ORIGIN:
                    # Nothing was asked of Google and nothing failed: the row
                    # has no coordinate worth routing from. Counting it as a
                    # refusal would send the operator hunting an outage.
                    approximate_origin += 1
                elif travel_api_state(prop) == TRAVEL_STATE_UNAVAILABLE:
                    api_refused += 1
            except Exception as inner:
                logger.warning(
                    "Travel recalculation failed for property %s: %s", prop.id, inner
                )
                db.session.rollback()
                continue

        # A run where Google refused everything used to flash the same green
        # count as a real one (#98); the refusals get their own number now.
        summary = f"Recalculated travel for {updated} / {len(properties)} properties"
        if approximate_origin:
            summary += (
                f"; {approximate_origin} not measured (approximate location — "
                "re-geocode first)"
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
        flash(summary, "warning" if (api_refused or approximate_origin) else "success")
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
    """
    from services.municipality_comparison_service import (
        DEFAULT_SORT,
        SORT_KEYS,
        MunicipalityComparisonService,
    )

    try:
        include_archived = request.args.get("archived") == "on"
        favorites_only = request.args.get("favorites") == "on"
        sort_by = request.args.get("sort") or DEFAULT_SORT
        if sort_by not in SORT_KEYS:
            sort_by = DEFAULT_SORT
        order = request.args.get("order") or (
            "asc" if sort_by == "municipality" else "desc"
        )

        query = Property.query
        if not include_archived:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )
        if favorites_only:
            query = query.filter(Property.is_favorite.is_(True))
        properties = query.all()

        service = MunicipalityComparisonService()
        rows = service.build_rows(properties)
        rows = service.sort_rows(rows, sort_by, descending=(order == "desc"))

        # Truncated email artifacts ("Ovi...", issue #298) count with the
        # unnamed listings: build_rows skips both, for the same reason -- a
        # value that names no municipality cannot be compared by one. Both
        # sides ask `group_key`, so the footnote cannot drift from the table
        # it is explaining.
        unnamed = sum(1 for p in properties if group_key(p.municipality) is None)
        return render_template(
            "municipalities.html",
            rows=rows,
            sort_by=sort_by,
            order=order,
            include_archived=include_archived,
            favorites_only=favorites_only,
            listing_total=len(properties),
            unnamed_listings=unnamed,
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
        profiles = SearchProfileService.list_profiles(active_only=True)

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

        category_filter = request.args.get("category", "")
        subtype_filter = request.args.get("subtype", "")
        municipality_filter = request.args.get("municipality", "")
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        favorites_filter = request.args.get("favorites", "") == "on"
        sea_view_filter = request.args.get("sea_view", "")

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
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)
        if investment_metrics_filter:
            query = _filter_by_investment_rating(
                query, Property, investment_metrics_filter
            )
        if sea_view_filter:
            query = _filter_by_sea_view(query, Property, sea_view_filter)

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        query = query.filter(Property.listing_status.notin_(DELISTED_LISTING_STATUSES))

        # The subscription filter goes on last so the query without it stays
        # in hand. That is what separates a focused listing that is merely in
        # another subscription -- the one case the page can offer a way out of
        # -- from one no filter here would have let through (#287). Identical
        # SQL either way: SQLAlchemy ANDs the clauses whatever their order.
        query_without_profile = query
        query = apply_profile_filter(
            query, Property.search_profile_id, profile_selection
        )
        props = query.all()

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
            list_view_profile_id=list(profile_selection.link_values),
            travel_display_targets=travel_display_targets,
            focus_notice=focus_notice,
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
            list_view_profile_id=None,
            travel_display_targets=[],
            focus_notice=None,
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
            query = _filter_by_investment_rating(query, Land, investment_metrics_filter)

        if sea_view_filter:
            # SQLAlchemy 2.x: .astext removed; use JSON accessors
            query = query.filter(Land.environment["sea_view"].as_boolean().is_(True))

        if favorites_filter:
            query = query.filter(Land.is_favorite)

        # Apply sorting with same logic as main lands route
        if sort_by == "investment_metrics":
            rank = _investment_rating_rank(Land)
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

        default_profile = SearchProfileService.get_default_profile(create=True)
        profiles = SearchProfileService.list_profiles(active_only=True)

        # Same profile_id contract as /properties (auto | all | selected(ids))
        # and the same auto-select fallback, so an export matches what that
        # page is showing instead of landing on a possibly-empty default.
        selection = parse_profile_selection(request.args)
        profile_selection = resolve_profile_selection(
            selection,
            [profile.id for profile in profiles],
            auto_profile_id=(
                SearchProfileService.resolve_richest_active_profile_id(
                    default_profile, profiles
                )
                if selection.is_auto
                else None
            ),
        )
        selected_profile_id = profile_selection.single_id

        category_filter = request.args.get("category", "")
        subtype_filter = request.args.get("subtype", "")
        municipality_filter = request.args.get("municipality", "")
        search_query = request.args.get("search", "")
        investment_metrics_filter = request.args.get("inv_metr", "")
        favorites_filter = request.args.get("favorites", "") == "on"
        sea_view_filter = request.args.get("sea_view", "")

        hide_removed_param = request.args.get("hide_removed", None)
        form_submitted = any(
            request.args.get(p)
            for p in [
                "profile_id",
                "category",
                "subtype",
                "municipality",
                "search",
                "inv_metr",
                "sea_view",
                "sort",
                "order",
                "favorites",
                "hide_removed",
            ]
        )
        if form_submitted:
            hide_removed_filter = hide_removed_param == "on"
        else:
            hide_removed_filter = True

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
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)

        if investment_metrics_filter:
            query = _filter_by_investment_rating(
                query, Property, investment_metrics_filter
            )

        if sea_view_filter:
            query = _filter_by_sea_view(query, Property, sea_view_filter)

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(
                Property.listing_status.notin_(DELISTED_LISTING_STATUSES)
            )

        # Same ordering as /properties, tiebreaker included: the export link
        # forwards whatever the page is sorted by, so an allow-list that does
        # not know a value would hand back the same rows in another order.
        sort_columns = {
            "title": Property.title,
            "created_at": Property.created_at,
            "price": Property.price,
            "area": Property.area,
            "score_total": Property.score_total,
            "score_investment": Property.score_investment,
            "score_lifestyle": Property.score_lifestyle,
            "travel_time_nearest_beach": _nearest_beach_minutes(Property),
        }
        if sort_by == "investment_metrics":
            rank = _investment_rating_rank(Property)
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

            row = [
                prop.id,
                prop.search_profile_id,
                prop.title,
                prop.url,
                price,
                area,
                prop.area_type,
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
                bool(prop.is_favorite),
                sea_view_verdict["state"],
                sea_view_verdict["source"],
                sea_view_verdict["distance_m"],
                sea_view_target["lat"] if sea_view_target else None,
                sea_view_target["lon"] if sea_view_target else None,
                float(prop.location_lat) if prop.location_lat else None,
                float(prop.location_lon) if prop.location_lon else None,
                prop.location_accuracy or "unknown",
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
