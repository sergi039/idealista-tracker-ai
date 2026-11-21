"""The plot of one house, from the cadastral parcel an Enrich press just placed (#634).

`services/plot_text.py` reads a plot the listing states. For the rows that state
none -- 1,663 of the 1,831 plot-`unknown` rows of subscription 24 that already
pass price and house size are yaencontre cards with no text at all -- the only
source left is Catastro, and only for a row whose parcel is *established*.

**This is not a new parcel lookup.** `NeighboursService` already resolves the
parcel under the strictest rules the app has -- a saved reference, else one
zero-distance parcel under a `precise` coordinate, else an exact numbered
address, never a privacy circle or a nearby guess -- and records it, with the
subject it was resolved for. This step runs right after it inside the same
Enrich press and is handed what that call returned **in this press**: a lookup
that raised, was refused or ran out of clock establishes nothing, and the
neighbour step's own "kept the earlier block" answer (`kept_previous`) is not a
parcel either. **It starts no
Catastro request at all**: the metric outline is read from the cache the
neighbour step just filled, under a spent lookup budget (`lookup_budget(0)`,
which refuses every request before it starts), so a cache that cannot be reached
or has already expired is a miss and the plot stays `unknown` until the next
press. A row the neighbour step could not place stays `unknown`, never `fail`,
and nothing is written.

**Rules, each one the contract of a neighbour:**

* one row per press, and no command line, no sweep: Catastro bans IPs, and the
  route that reaches this is limited to five presses a minute;
* a plot somebody stated is never overwritten, and the check is repeated under
  the row lock, because the lookup takes seconds (`plot_text.apply_to_property`
  has the same rule at ingestion);
* the figure goes to `plot_area` -- the column the Python and SQL twins of
  `subscription_criteria` read -- and its source goes beside it in
  `enrichment["plot_from_cadastre"]` in the same transaction (no migration);
* only a house: a flat's parcel is the whole building's lot, not its plot, and a
  row of no known kind says nothing;
* the figure is the parcel's own polygon area, which `cadastre_service` has
  already compared with Catastro's declared `areaValue`, **floored** from the
  exact double (no `round()` or `str()` first) to the 0.01 the `NUMERIC(10,2)` column stores (never up: 699.96 must not read 700),
  and credible (`MAX_CREDIBLE_M2`);
* **a value lives only as long as its subject.** The block records the subject
  (`neighbours.subject`: the cadastral reference of the column and of the block,
  the coordinate, its accuracy, the asked address, the municipality) the area
  was measured for. A plot that still equals the block's value while the row's
  subject has changed -- the reference was corrected or cleared, the coordinate
  moved, the row stopped being a house -- is retired: `plot_area` back to NULL
  and the block removed, under the row lock, whether or not Catastro answers
  now. A press that also establishes the new parcel writes its area in the same
  transaction. A plot that is not the block's value (stated, hand-set, read from
  the text, or typed over this one) is never touched.

**What it does not know**, in both directions: one parcel is not always the
plot a listing sells. Where several units stand on the parcel its area is
bigger than any one unit's plot; where the property lies on several parcels
(usual in rural Galicia) it is smaller than the plot, and such a row can go from
`unknown` to `fail` on the plot criterion. The provenance block records the
reference, so a person can check it against the Catastro viewer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Dict, Optional

from services import cadastre_service as cadastre, neighbours_service as neighbours
from services.enrichment_write import check_writable, locked_write
from services.plot_text import PLOT_AREA_STORAGE_MAX, _blank
from services.property_classification_service import KIND_HOUSE, listing_kind
from services.subscription_criteria import MAX_CREDIBLE_M2, effective_figures
from utils.http import lookup_budget

logger = logging.getLogger(__name__)

ENRICHMENT_KEY = "plot_from_cadastre"
SOURCE = "catastro_inspire_parcel"
EXTRACTOR_VERSION = "plot_cadastre/1"


PLOT_STEP = Decimal("0.01")  # what NUMERIC(10, 2) stores


def _is_house(prop: Any) -> bool:
    return listing_kind(
        getattr(prop, "property_category", None),
        getattr(prop, "property_subtype", None),
    ) == (KIND_HOUSE)


def needs_plot(prop: Any) -> bool:
    """A house whose plot nobody has stated (the one gate, asked twice)."""
    if not _is_house(prop):
        return False
    if effective_figures(prop)["plot_m2"] is not None:
        return False
    return getattr(prop, "plot_area", None) is None or _blank(prop.plot_area)


def _stored(value: Any) -> Optional[Decimal]:
    """A plot as the column holds it (already two decimals), or None.

    Read through its text: a stored 699.97 that came back as the float
    699.969999... is still 699.97.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value)).quantize(PLOT_STEP, rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _floored(area: float) -> Optional[Decimal]:
    """A measured area floored to the column's 0.01, from its exact value.

    `Decimal(float)` is the number the double really is -- no `str()`, no
    `round()` in between, either of which can lift a figure just under a
    threshold onto it (699.9999996 -> 700.0). Floored, never rounded: the stored
    figure is never above the measurement.
    """
    try:
        number = Decimal(area).quantize(PLOT_STEP, rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _written_block(prop: Any) -> Optional[Dict[str, Any]]:
    """Our block, when the column still holds exactly the value it recorded."""
    block = (getattr(prop, "enrichment", None) or {}).get(ENRICHMENT_KEY)
    if not isinstance(block, dict):
        return None
    recorded = _stored(block.get("value"))
    if recorded is None or recorded != _stored(getattr(prop, "plot_area", None)):
        return None
    return block


def is_stale(prop: Any) -> bool:
    """A plot this module wrote whose subject has since changed."""
    block = _written_block(prop)
    if block is None:
        return False
    return block.get("subject") != neighbours.subject(prop) or not _is_house(prop)


def retire_stale(prop: Any) -> bool:
    """Take back a stale plot of ours: `plot_area` NULL, the block removed.

    A change to the session only: the caller holds the row lock and commits. It
    asks no question of Catastro and never touches a plot that is not ours.
    """
    if not is_stale(prop):
        return False
    enrichment = dict(prop.enrichment or {})
    enrichment.pop(ENRICHMENT_KEY, None)
    prop.enrichment = enrichment
    prop.plot_area = None
    return True


def _established_parcel(
    prop: Any, result: Optional[Dict[str, Any]]
) -> Optional[Dict[str, str]]:
    """The reference and subject THIS press's neighbour step settled on, or None.

    `result` is what `NeighboursService.enrich` returned in this press -- not the
    block it stored. A stored `ok` from an earlier press says nothing about this
    one: when this press's lookup raised, was refused or ran out of clock, the
    neighbour step keeps its old block and says so (`kept_previous`), and a
    parcel nobody established now is not a ground to write a plot.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    if result.get(neighbours.KEPT_PREVIOUS):
        return None
    if result.get("subject") != neighbours.subject(prop):
        return None
    reference = cadastre.normalize_reference(result.get("reference"))
    if not reference:
        return None
    return {
        "reference": cadastre.parcel_reference(reference),
        "subject": result["subject"],
    }


def parcel_area_m2(reference: str) -> Decimal:
    """The parcel's polygon area in m2, checked against Catastro's declaration.

    Cache only: the budget is spent before the call, so `_metric_parcel` can
    answer from what the neighbour step cached and can start no request. Rounded
    DOWN to the column's 0.01: the value never exceeds the polygon's, so no
    threshold of two decimals or fewer is crossed by rounding.
    """
    with lookup_budget(0):
        parcel, _ = neighbours._metric_parcel(reference)
    area = _floored(parcel.area)
    if area is None or not (
        0 < area < Decimal(str(MAX_CREDIBLE_M2))
        and area < Decimal(str(PLOT_AREA_STORAGE_MAX))
    ):
        raise cadastre.CadastreError(cadastre.MALFORMED, "parcel area not credible")
    return area


def enrich(
    prop: Any, established: Optional[Dict[str, Any]], *, commit: bool = True
) -> Optional[Dict[str, Any]]:
    """Keep the plot of this house in step with its parcel; the block when written.

    `established` is what `NeighboursService.enrich` returned in this same press
    (None when it raised). A plot of ours whose subject changed is retired first,
    under the lock and with no question to Catastro; then, when this press
    established the parcel and its outline is cached, the new area is written in
    the same transaction. Every refusal leaves the row `unknown`, never the old
    figure and never a `fail`.
    """
    if not (is_stale(prop) or needs_plot(prop)):
        return None
    parcel = _established_parcel(prop, established) if _is_house(prop) else None

    locked = check_writable(prop, commit)
    area = None
    if parcel is not None:
        try:
            area = parcel_area_m2(parcel["reference"])
        except Exception as exc:
            logger.info("plot from cadastre skipped for %s: %s", prop.id, exc)

    with locked_write(prop, locked=locked, commit=commit):
        # Everything is asked again on the row as the lock found it: a plot
        # stated or hand-set while Catastro answered wins, a row that moved or
        # changed kind is not written, and a stale figure of ours goes whatever
        # the answer was.
        retire_stale(prop)
        if area is None or not needs_plot(prop):
            return None
        if _established_parcel(prop, established) != parcel:
            return None
        block = {
            "value": str(area),
            "reference": parcel["reference"],
            "source": SOURCE,
            "extractor_version": EXTRACTOR_VERSION,
            "subject": parcel["subject"],
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }
        enrichment = dict(prop.enrichment or {})
        enrichment[ENRICHMENT_KEY] = block
        prop.enrichment = enrichment
        prop.plot_area = area
    return block
