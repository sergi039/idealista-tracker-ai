"""Publications of the same home folded into one row on every listing surface (#633).

`services/property_duplicates.py` says which rows are publications of the same
home; this module is the ONE place that turns that reading into "show one row".
The list, the CSV export, `/api/properties` and the map all call `fold_query`,
so they cannot disagree about which rows a page holds.

What "selection" means here: the rows a surface is about to draw, after EVERY
other filter and hide it applies (subscription, filter bar, criteria hide,
rejected hide, Favorites, Hide removed, the Recommendations collection) and
before pagination and sort. A row a filter removed takes no part: it neither
folds another row nor is counted in a group. Search for one publication and you
get that publication; filter to one portal and each portal's row stays.
Groups are judged over the whole selection, across subscriptions: the same
home reached through two saved searches is still one home, and the Seaia
advert in the recommendations is grouped the same way. Narrowing to one
subscription is a filter like any other, so each of its rows then stands alone.

Cost, bounded: one statement for the rule's columns (never ORM objects, never
the JSON blobs -- only the two small coordinate sub-documents the rule reads),
one for the owner's links, one for the timeline entries of grouped rows; three
statements whatever the size of the selection. A selection larger than
`FOLD_MAX_ROWS` is not folded and the surface says so.

Nothing is merged, stored or deleted. A decision is written to the clicked row
only; a protected member (favourite, decision, outstanding action, timeline
entry) is never folded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from sqlalchemy import false, select

from services import property_duplicates as dup
from utils.listing_source import safe_external_url, source_label, source_of

# Above this many rows in one selection the fold is skipped, not slowed down.
FOLD_MAX_ROWS = 20_000

# Query parameters. Both are filter-only (never a sort or view state).
PARAM_SHOW = "duplicates"
SHOW_VALUE = "show"
PARAM_OF = "duplicate_of"

# The statuses the rule treats as withdrawn (`property_duplicates.representative`).
WITHDRAWN_STATUSES = frozenset({"removed", "sold"})


@dataclass(frozen=True)
class Params:
    """What the request said about folding."""

    show: bool = False
    of: Optional[int] = None
    of_invalid: bool = False
    raw_of: str = ""

    @property
    def folding(self) -> bool:
        """True when the surface drops folded members."""
        return not self.show and self.of is None and not self.of_invalid

    @property
    def link_args(self) -> Dict[str, Optional[str]]:
        return {
            PARAM_SHOW: SHOW_VALUE if self.show else None,
            PARAM_OF: self.raw_of or None,
        }


def for_collection(params: Params, recommendations: bool) -> Params:
    """The parameters a surface applies.

    The Recommendations collection keeps its own grouping
    (`services/recommendation_duplicates.py`: the five seed pairs, each needing
    independent corroboration) and is not folded by this rule on top of it;
    `duplicate_of` still works there. Everywhere else the request is applied
    as read.
    """
    if not recommendations or params.of is not None or params.of_invalid:
        return params
    return replace(params, show=True)


def read_params(args: Any) -> Params:
    """`duplicates=show` and `duplicate_of=<integer>`; anything else is absent
    for `duplicates`, and an unusable `duplicate_of` names no group (an empty
    result, never a widened one)."""
    show = (args.get(PARAM_SHOW) or "").strip().lower() == SHOW_VALUE
    raw = (args.get(PARAM_OF) or "").strip()
    if not raw:
        return Params(show=show)
    if raw.isascii() and raw.isdigit() and len(raw) <= 9 and int(raw) > 0:
        return Params(show=show, of=int(raw), raw_of=raw)
    return Params(show=show, of_invalid=True, raw_of=raw[:20])


@dataclass(frozen=True)
class Publication:
    """One member of a group, as much as a row needs to name it."""

    id: int
    portal: str
    portal_label: str
    href: Optional[str]  # already through `safe_external_url`, else None
    price: Optional[float]
    withdrawn: bool


@dataclass(frozen=True)
class GroupView:
    representative_id: int
    tier: str
    members: Tuple[Publication, ...]  # ordered by id
    folded_ids: FrozenSet[int]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def member_ids(self) -> Tuple[int, ...]:
        return tuple(m.id for m in self.members)

    @property
    def withdrawn(self) -> int:
        return sum(1 for m in self.members if m.withdrawn)

    @property
    def price_range(self) -> Optional[Tuple[float, float]]:
        """(low, high) when the members' asking prices differ by a whole euro."""
        prices = [m.price for m in self.members if m.price]
        if not prices or round(min(prices)) == round(max(prices)):
            return None
        return min(prices), max(prices)

    def others(self, row_id: int) -> List[Publication]:
        return [m for m in self.members if m.id != row_id]


@dataclass
class Fold:
    """The fold of one selection."""

    groups: Dict[int, GroupView] = field(default_factory=dict)  # member id -> group
    folded_ids: FrozenSet[int] = frozenset()
    skipped: bool = False
    selection_size: int = 0

    def group_of(self, row_id: int) -> Optional[GroupView]:
        return self.groups.get(row_id)

    def annotation(self, row_id: int) -> Dict[str, Any]:
        """`duplicate_group_size`, `duplicate_member_ids`, `duplicate_tier` for
        a CSV or API row: size 1 and nothing else for an ungrouped row."""
        group = self.groups.get(row_id)
        if group is None:
            return {
                "duplicate_group_size": 1,
                "duplicate_member_ids": [],
                "duplicate_tier": "",
            }
        return {
            "duplicate_group_size": group.size,
            "duplicate_member_ids": list(group.member_ids),
            "duplicate_tier": group.tier,
        }


# The columns the rule reads, and nothing else.
def _columns():
    from models import Property as P

    return (
        P.id,
        P.municipality,
        P.price,
        P.previous_price,
        P.area,
        P.title,
        P.url,
        P.source_email_id,
        P.idealista_property_id,
        P.cadastral_reference,
        P.location_lat,
        P.location_lon,
        P.location_accuracy,
        P.listing_status,
        P.is_favorite,
        P.owner_verdict,
        P.next_action,
        P.next_action_due_on,
        # The two small sub-documents `coordinate_quality.coordinate_tier`
        # reads, not the whole `enrichment` blob.
        P.enrichment["import"]["coordinate"].label("pin"),
        P.enrichment["location"].label("hand_set"),
    )


def _light(row: Any) -> SimpleNamespace:
    data = dict(row._mapping)
    pin, hand = data.pop("pin"), data.pop("hand_set")
    enrichment: Dict[str, Any] = {}
    if isinstance(pin, dict):
        enrichment["import"] = {"coordinate": pin}
    if isinstance(hand, dict):
        enrichment["location"] = hand
    return SimpleNamespace(**data, enrichment=enrichment)


def _publication(row: Any) -> Publication:
    portal = source_of(row)
    price = dup._number(row.price)
    return Publication(
        id=row.id,
        portal=portal,
        portal_label=source_label(portal),
        href=safe_external_url(row.url),
        price=price,
        withdrawn=row.listing_status in WITHDRAWN_STATUSES,
    )


def _links(ids: List[int]) -> "dup.Links":
    """The owner's decisions between rows of `ids`: ONE statement."""
    from app import db
    from models import PropertyDuplicateLink

    members = set(ids)
    same, different = set(), set()
    if not ids:
        return dup.Links()
    for link in db.session.execute(
        select(PropertyDuplicateLink).where(
            PropertyDuplicateLink.property_low_id.in_(ids)
        )
    ).scalars():
        if link.property_high_id not in members:
            continue
        pair = (link.property_low_id, link.property_high_id)
        (same if link.verdict == dup.VERDICT_SAME else different).add(pair)
    return dup.Links(frozenset(same), frozenset(different))


def _activity_ids(ids: Iterable[int]) -> FrozenSet[int]:
    """Rows with a live timeline entry: ONE statement over grouped rows."""
    from app import db
    from models import PropertyActivity

    wanted = list(ids)
    if not wanted:
        return frozenset()
    found = db.session.execute(
        select(PropertyActivity.property_id)
        .where(
            PropertyActivity.property_id.in_(wanted),
            PropertyActivity.deleted_at.is_(None),
        )
        .distinct()
    ).scalars()
    return frozenset(found)


def compute(query: Any, *, keep_ids: Iterable[int] = ()) -> Fold:
    """The fold of `query`'s rows. `keep_ids` are never folded (a focused id)."""
    rows = (
        query.order_by(None).with_entities(*_columns()).limit(FOLD_MAX_ROWS + 1).all()
    )
    if len(rows) > FOLD_MAX_ROWS:
        return Fold(skipped=True, selection_size=len(rows))
    light = [_light(row) for row in rows]
    links = _links([row.id for row in light])

    found = dup.groups_with_links(light, links)
    if not found:
        return Fold(selection_size=len(light))

    members = {row.id for group in found for row in group.members}
    folded = dup.folded_ids(found, activity_ids=_activity_ids(members))
    folded -= set(keep_ids)

    views: Dict[int, GroupView] = {}
    for group in found:
        keep = dup.representative(group)
        group_folded = frozenset(i for i in group.ids if i in folded)
        view = GroupView(
            representative_id=keep.id,
            tier=group.tier,
            members=tuple(_publication(row) for row in group.members),
            folded_ids=group_folded,
        )
        for row_id in group.ids:
            views[row_id] = view
    return Fold(groups=views, folded_ids=frozenset(folded), selection_size=len(light))


def apply(query: Any, fold: Fold, params: Params) -> Any:
    """Narrow `query` by `fold` as the request asked."""
    from models import Property

    if params.of_invalid:
        return query.filter(false())
    if params.of is not None:
        group = fold.groups.get(params.of)
        ids = group.member_ids if group is not None else (params.of,)
        return query.filter(Property.id.in_(ids))
    if params.show or fold.skipped or not fold.folded_ids:
        return query
    return query.filter(Property.id.notin_(fold.folded_ids))


def fold_query(
    query: Any, params: Params, *, keep_ids: Iterable[int] = ()
) -> Tuple[Any, Fold]:
    """`(narrowed query, the fold)` -- the one call every surface makes."""
    fold = compute(query, keep_ids=keep_ids)
    return apply(query, fold, params), fold


def folded_count(fold: Fold, params: Params) -> int:
    """How many rows this request dropped; 0 when it dropped none."""
    return len(fold.folded_ids) if params.folding and not fold.skipped else 0
