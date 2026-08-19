"""What a derived answer was computed over, said out loud (UNIVERSE-001, #265).

Five surfaces in this repository answer questions derived from `properties`,
and each answers about a different set of rows:

* `GET /api/properties` — a **selected set**: one subscription, a page of it;
* `/municipalities` — the stored **inventory**, every subscription at once;
* `services/property_comparables.py` — a **pool relative to one subject**,
  inside that subject's own subscription and around its own size;
* `shared_coordinate_peers` — a **global exact-equality class** over the whole
  table;
* `utils/enrich_scope.py` and the backfills — an **operational work queue**.

The decision recorded in #410 is that those five populations are legitimately
different and must **not** be unified: forcing one "all" on them would make
four of them answer a question nobody asked. What was missing is that none of
them *named* the population it used, so two of them disagreeing read as a fact
about the listings rather than as a difference of scope — which is exactly how
38 of 87 municipality drill-downs came to contradict the rows above them
(#417).

So this module is a **vocabulary, not a filter**. It computes nothing about
which rows belong to a population and offers no way to narrow one. It gives
the four things a reader needs in order to know what an answer is about, and
one shape for saying them:

1. **population** — what set this is, in a phrase;
2. **inclusions and exclusions** — what is in it that a reader would not
   expect, chiefly which subscriptions (`SubscriptionMix`, the one piece of
   arithmetic here that is genuinely the same for four of the five);
3. **pagination and truncation** — how much was held back, and by what;
4. **adjustment basis** — whether a derived number was normalised, banded or
   left raw.

`utils/report_coordinate_quality.py` already wrote the thesis for the third of
those, and this module is its generalisation: *"a truncated view that does not
name its own truncation reads as the whole picture."*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

# What `basis` says when a surface hands back rows as they are stored. Named
# rather than left blank: "no adjustment" is a fact about the answer, and a
# missing field reads as an unanswered question.
BASIS_RAW_ROWS = "rows as stored, no adjustment"

# What a work queue says instead. It derives no number at all, and "no basis"
# and "nobody said" have to stay distinguishable here for the same reason they
# do everywhere else in this repository.
BASIS_NOT_DERIVED = "no derived number - rows are measured and written in place"


@dataclass(frozen=True)
class SubscriptionMix:
    """Which subscriptions a population is made of, and how many rows each kind
    contributed.

    Four of the five surfaces span more than one subscription, and the kinds
    are not interchangeable: a retired subscription (`is_active = false`) holds
    listings that stopped arriving — 311 of 772 on production, median
    `created_at` 2026-02-18 against 2026-08-16 for the live ones — and a hidden
    one (`is_hidden`, #403) is one the owner took off the screens. An answer
    that mixes them without saying so is describing a corpus the reader thinks
    they are not looking at.

    `unknown` counts ids that name no `search_profiles` row at all: a
    subscription deleted since the rows were written. It is kept separate from
    the three real kinds rather than folded into any of them, because "nobody
    knows what this was" is not a fourth flavour of subscription.
    """

    active: int = 0
    retired: int = 0
    hidden: int = 0
    unknown: int = 0
    listings_active: int = 0
    listings_retired: int = 0
    listings_hidden: int = 0
    listings_unknown: int = 0
    listings_unassigned: int = 0

    @property
    def subscriptions(self) -> int:
        return self.active + self.retired + self.hidden + self.unknown

    @property
    def listings(self) -> int:
        return (
            self.listings_active
            + self.listings_retired
            + self.listings_hidden
            + self.listings_unknown
            + self.listings_unassigned
        )

    @property
    def is_mixed(self) -> bool:
        """Whether anything but live subscriptions contributed.

        The question a disclosure line exists to answer: a population that is
        only live subscriptions is the one the reader already assumes.
        """
        return bool(
            self.retired or self.hidden or self.unknown or self.listings_unassigned
        )

    def as_dict(self) -> Dict[str, int]:
        return {
            "subscriptions": self.subscriptions,
            "active": self.active,
            "retired": self.retired,
            "hidden": self.hidden,
            "unknown": self.unknown,
            "listings": self.listings,
            "listings_active": self.listings_active,
            "listings_retired": self.listings_retired,
            "listings_hidden": self.listings_hidden,
            "listings_unknown": self.listings_unknown,
            "listings_unassigned": self.listings_unassigned,
        }


def subscription_mix(
    listings_by_profile: Mapping[Optional[int], int],
) -> SubscriptionMix:
    """Classify the subscriptions behind `{profile id: listing count}`.

    `None` as a key is the unassigned rows (`search_profile_id IS NULL`), which
    are listings without a subscription rather than a subscription of their
    own — so they are counted apart and never make `active` bigger.

    One query, whatever the caller holds: the alternative is every caller
    writing its own `is_active` / `is_hidden` join, which is how "the same
    fact under two names" starts. The four state flags are read from
    `SearchProfile` directly rather than through
    `SearchProfileService.list_profiles`, because that helper's job is to
    decide what to *offer*, and offering is exactly the judgement a disclosure
    must not make -- it has to count what is there.
    """
    from models import SearchProfile
    from services.search_profile_service import SearchProfileService

    ids = sorted({int(key) for key in listings_by_profile if key is not None})
    rows = (
        {
            profile.id: profile
            for profile in SearchProfile.query.filter(SearchProfile.id.in_(ids)).all()
        }
        if ids
        else {}
    )
    # "Hidden" is asked of `SearchProfileService.hidden_clause()` rather than
    # read off the column here, because that clause and `visible_clause()` are
    # each other's complement by construction -- a NULL `is_hidden` is visible
    # there and must not be counted as hidden here. A second reading written
    # out by hand is what those two clauses exist to prevent.
    hidden_ids = (
        {
            profile_id
            for (profile_id,) in SearchProfile.query.with_entities(SearchProfile.id)
            .filter(SearchProfile.id.in_(ids), SearchProfileService.hidden_clause())
            .all()
        }
        if ids
        else set()
    )

    active = retired = hidden = unknown = 0
    listings_active = listings_retired = listings_hidden = listings_unknown = 0
    for profile_id, count in listings_by_profile.items():
        count = int(count or 0)
        if profile_id is None:
            continue
        profile = rows.get(int(profile_id))
        if profile is None:
            unknown += 1
            listings_unknown += count
        elif int(profile_id) in hidden_ids:
            # Hidden first: a hidden subscription is usually active too, and
            # counting it in both places would make the kinds sum to more than
            # the subscriptions there are. It is also how the subscription menu
            # already ranks the two flags -- `_profile_dropdown_options` sorts
            # by `(is_hidden, not is_active, name)`, so a subscription that is
            # both shows under *Hidden* and never under *Archive*.
            hidden += 1
            listings_hidden += count
        elif bool(profile.is_active):
            # `list_profiles(active_only=True)` filters `is_active.is_(True)`,
            # so a NULL is not active there and is retired here. The two agree;
            # there is no clause helper to import for this one.
            active += 1
            listings_active += count
        else:
            retired += 1
            listings_retired += count

    return SubscriptionMix(
        active=active,
        retired=retired,
        hidden=hidden,
        unknown=unknown,
        listings_active=listings_active,
        listings_retired=listings_retired,
        listings_hidden=listings_hidden,
        listings_unknown=listings_unknown,
        listings_unassigned=int(listings_by_profile.get(None) or 0),
    )


@dataclass(frozen=True)
class Population:
    """The set an answer is about, in the four terms a reader needs.

    Every field is optional except the label, and an absent field means "this
    surface has nothing to say here" -- never "zero". A `total` of `None` is a
    population nobody counted, which is a different statement from a population
    of no rows, and the #98 rule this repository applies to measurements
    applies to its own bookkeeping too.
    """

    #: What set this is, in a phrase the reader can check against their
    #: expectation ("every stored listing", "one subscription").
    label: str

    #: Rows the population holds, before any cap. `None` when nothing counted.
    total: Optional[int] = None

    #: Rows actually handed over.
    returned: Optional[int] = None

    #: What limited `returned`, if anything did -- a page size, a hard cap.
    cap: Optional[int] = None

    #: How a derived number was adjusted, or `BASIS_RAW_ROWS`.
    basis: Optional[str] = None

    #: Which subscriptions contributed.
    subscriptions: Optional[SubscriptionMix] = None

    #: Anything else this surface owes its reader -- a parameter that was read
    #: differently from how it was written, a filter the caller cannot see.
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def truncated(self) -> bool:
        """Whether rows were held back. Unknown counts as not truncated.

        Deliberately `False` rather than `None` when the total was never
        counted: `truncated` is a claim, and the honest way to say "we did not
        count" is to leave `total` at `None`, which a reader can see.
        """
        if self.total is None or self.returned is None:
            return False
        return self.returned < self.total

    @property
    def not_shown(self) -> int:
        if self.total is None or self.returned is None:
            return 0
        return max(0, self.total - self.returned)

    def as_dict(self) -> Dict[str, Any]:
        """The JSON shape. Keys whose value is unknown are still present.

        A consumer that has to check whether a key exists before it can read a
        disclosure will eventually stop checking; `null` says the same thing
        and cannot be missed.
        """
        return {
            "population": self.label,
            "total": self.total,
            "returned": self.returned,
            "cap": self.cap,
            "truncated": self.truncated,
            "basis": self.basis,
            "subscriptions": self.subscriptions.as_dict()
            if self.subscriptions
            else None,
            "notes": list(self.notes),
        }

    def as_lines(self) -> List[str]:
        """The stdout/log shape, for the CLIs.

        One fact per line, in the order of the contract: what the set is, what
        it is made of, what was held back, how it was adjusted.
        """
        lines = [f"Population: {self.label}"]
        if self.total is not None:
            if self.returned is not None and self.returned != self.total:
                lines.append(f"  rows: {self.returned} of {self.total}")
            else:
                lines.append(f"  rows: {self.total}")
        mix = self.subscriptions
        if mix is not None:
            parts = [f"{mix.active} live"]
            if mix.retired:
                parts.append(f"{mix.retired} retired")
            if mix.hidden:
                parts.append(f"{mix.hidden} hidden")
            if mix.unknown:
                parts.append(f"{mix.unknown} unknown")
            detail = [f"{mix.listings_active} listings from live"]
            if mix.listings_retired:
                detail.append(f"{mix.listings_retired} from retired")
            if mix.listings_hidden:
                detail.append(f"{mix.listings_hidden} from hidden")
            if mix.listings_unknown:
                detail.append(f"{mix.listings_unknown} from unknown")
            if mix.listings_unassigned:
                detail.append(f"{mix.listings_unassigned} with no subscription")
            lines.append(
                f"  subscriptions: {mix.subscriptions} ({', '.join(parts)}) — "
                + ", ".join(detail)
            )
        if self.truncated:
            lines.append(f"  not shown: {self.not_shown} (cap {self.cap})")
        if self.basis:
            lines.append(f"  basis: {self.basis}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return lines


def listings_by_profile(rows: Iterable[Any]) -> Dict[Optional[int], int]:
    """Tally `{search_profile_id: rows}` over an already-selected set.

    Takes the rows the caller *used*, never a query of its own. Every defect
    this module was written for began with a second query answering a slightly
    different question from the first (#417).
    """
    counts: Dict[Optional[int], int] = {}
    for row in rows:
        key = getattr(row, "search_profile_id", None)
        key = int(key) if key is not None else None
        counts[key] = counts.get(key, 0) + 1
    return counts
