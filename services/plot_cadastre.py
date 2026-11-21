"""The plot of one house, from the cadastral parcel an Enrich press just placed (#634).

`services/plot_text.py` reads a plot the listing states. For the rows that state
none -- 1,663 of the 1,831 plot-`unknown` rows of subscription 24 that already
pass price and house size are yaencontre cards with no text at all -- the only
source left is Catastro, and only for a row whose parcel is *established*.

**This is not a new parcel lookup.** `NeighboursService` already resolves the
parcel under the strictest rules the app has -- a saved reference, else one
zero-distance parcel under a `precise` coordinate, else an exact numbered
address, never a privacy circle or a nearby guess -- and records it, with the
subject it was resolved for, in `enrichment["neighbours"]`. This step runs right
after it inside the same Enrich press and reads that block. **It starts no
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
  already compared with Catastro's declared `areaValue`; it must be credible
  (`MAX_CREDIBLE_M2`) and fit the `NUMERIC(10,2)` column.

**What it does not know** and says so: a parcel is not always the plot a
listing sells. A property made of two parcels reads smaller than it is, and a
house sold with part of a larger finca reads larger. The provenance block
records the reference, so a person can check it against the Catastro viewer.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
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


def needs_plot(prop: Any) -> bool:
    """A house whose plot nobody has stated (the one gate, asked twice)."""
    if listing_kind(
        getattr(prop, "property_category", None),
        getattr(prop, "property_subtype", None),
    ) != (KIND_HOUSE):
        return False
    if effective_figures(prop)["plot_m2"] is not None:
        return False
    return getattr(prop, "plot_area", None) is None or _blank(prop.plot_area)


def _established_parcel(prop: Any) -> Optional[Dict[str, str]]:
    """The reference and subject the neighbour step settled on, or None."""
    block = (getattr(prop, "enrichment", None) or {}).get("neighbours")
    if not isinstance(block, dict) or block.get("status") != "ok":
        return None
    if block.get("subject") != neighbours.subject(prop):
        return None
    reference = cadastre.normalize_reference(block.get("reference"))
    if not reference:
        return None
    return {
        "reference": cadastre.parcel_reference(reference),
        "subject": block["subject"],
    }


def parcel_area_m2(reference: str) -> float:
    """The parcel's polygon area in m2, checked against Catastro's declaration.

    Cache only: the budget is spent before the call, so `_metric_parcel` can
    answer from what the neighbour step cached and can start no request.
    """
    with lookup_budget(0):
        parcel, _ = neighbours._metric_parcel(reference)
    area = round(float(parcel.area), 1)
    if not (
        math.isfinite(area)
        and 0 < area < MAX_CREDIBLE_M2
        and area < PLOT_AREA_STORAGE_MAX
    ):
        raise cadastre.CadastreError(cadastre.MALFORMED, "parcel area not credible")
    return area


def enrich(prop: Any, *, commit: bool = True) -> Optional[Dict[str, Any]]:
    """Write the parcel's area as the plot of this row; None when nothing was.

    Call it after `NeighboursService.enrich` in the same press. Every refusal is
    a return of None, so the row stays `unknown` and nothing is recorded.
    """
    if not needs_plot(prop):
        return None
    parcel = _established_parcel(prop)
    if parcel is None:
        return None

    locked = check_writable(prop, commit)
    try:
        area = parcel_area_m2(parcel["reference"])
    except Exception as exc:
        logger.info("plot from cadastre skipped for %s: %s", prop.id, exc)
        return None

    with locked_write(prop, locked=locked, commit=commit):
        # Re-asked under the lock: a plot stated, hand-set or read from the text
        # while Catastro answered wins, and so does a row that has since moved.
        if not needs_plot(prop):
            return None
        if _established_parcel(prop) != parcel:
            return None
        block = {
            "value": area,
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
