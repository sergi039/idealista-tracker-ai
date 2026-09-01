"""Writing one block of `Property.enrichment` without losing the others.

`enrichment` is a single JSON column, so every writer of a block inside it
performs a read-modify-write over the whole column, and every one of them
spends seconds on external calls before the read. A guard that compares the
new value against `prop.enrichment` is therefore comparing against the copy
*its own session* loaded, which is correct within one process and blind to
anything another one commits in between.

Measured on the mini, 2026-08-16 (#339): two `utils.backfill_pool` runs
overlapped and each wrote the other's rows away, replacing two measured pools
with refusals. #344 closed it for the pool writer; #352 recorded that the same
shape was still live in `sea_distance_service` and `quality_of_life_service`,
and that a lock is mutual exclusion only among writers that take it -- one
locked writer among three unlocked ones can make matters worse, because the
unlocked `UPDATE` blocks on the lock and then writes the copy it read before
it.

The rule this module owns for a writer that owns its commit, settled for this
column by `sea_view_service.apply_to_property` in #196:

* the caller is validated **before** the measurement -- it is a cheap raise
  instead of a billed round of lookups for a write that could not persist;
* the row is locked **after** the measurement -- holding it across those
  seconds is the cost #196 refused;
* the block is re-read from the locked row, never from the copy in memory;
* the transaction is ended on every exit, including the failure path, so no
  row lock survives into the next row of a backfill.

`commit=False` takes no lock inside an individual writer: the caller owns a
transaction whose end that writer cannot see, and holding a lock across later
network calls is worse than the race it would close. The decisive pass in
`PropertyEnrichmentService.enrich_property` is the exception at the
orchestration boundary (#473): after its network calls it uses
`merge_json_object_changes_under_lock` to lock, re-read and merge only the
top-level keys it changed, then immediately commits its sea, Google and travel
results together. Its advisory pass passes `commit=True` and each step owns
its locked write, so nothing score-neutral can hold a paid measurement in an
uncommitted session (#434).

**Nothing here is about `enrichment`** except the name and the incidents that
prompted it. The helpers accept a mapped row or attribute rather than naming a
column: `check_writable` inspects session state, `locked_write` refreshes a row,
and the merge helper selects the mapped JSON attribute it is given. #400 made
that literal -- `services/property_location_service.py` writes the scalar
`location_lat`/`location_lon`/`location_accuracy` under the same rule, after
the same kind of incident one column over. If a third kind of writer arrives,
it belongs here too rather than in a second module; what would not belong is a
caller that takes the lock itself, for the reason
`services/sea_view_service.py:1294` gives — a lock at a call site protects that
call site, and every future caller reopens the hole.
"""

import logging
from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager
from typing import Any

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


class EnrichmentWriteContractError(RuntimeError):
    """The caller cannot be honoured: a programming error, not a refusal.

    Distinct from every measurement failure on purpose. Both writers that take
    `commit=True` sit inside a blanket `except Exception` that logs a warning
    and rolls back -- `services/property_imap_service.py` at ingestion and
    `utils/backfill_quality_of_life.py` in its loop. Raising a bare
    `RuntimeError` there would be swallowed and logged as *"Sea distance
    measurement failed"*, indistinguishable from Overpass refusing: the row
    would carry no measurement, `--only-missing` would re-attempt it forever,
    and nobody would learn that the caller was at fault rather than the
    network. That is #98's shape one level up -- a caller error wearing a
    refusal's clothes.

    It subclasses `RuntimeError` so existing handlers still catch it, and the
    guard logs at `error` with its own wording before raising, so even a
    caller that swallows it leaves a line that cannot be read as a transport
    problem.
    """


def check_writable(prop: Any, commit: bool) -> bool:
    """Whether the write will be locked; raises when `commit` cannot be honoured.

    Call this *before* the measurement. A mapped instance the session does not
    hold, or a session with other work pending, cannot be committed here and
    saying so early costs nothing.
    """
    from app import db

    try:
        state = sa_inspect(prop)
    except NoInspectionAvailable:
        state = None  # not a mapped instance; nothing to lock

    if not (commit and state is not None):
        return False

    # `db.session` is a scoped-session proxy, so comparing it against
    # `state.session` is always unequal; ask the proxy whether it holds the
    # object. This covers a detached one too, whose `state.session` is None.
    if prop not in db.session:
        raise _contract_error(
            "an enrichment write was asked to commit a property this session "
            "does not hold; the write would not be persisted"
        )
    # The locked refresh autoflushes, which would write out anything else
    # pending -- including a stale `enrichment` assigned before this call,
    # erasing the very block the locked read is about to inspect. And since
    # every exit ends the transaction, a caller's uncommitted work would be
    # committed or discarded wholesale.
    if db.session.new or db.session.dirty or db.session.deleted:
        raise _contract_error(
            "an enrichment write with commit=True needs a session with nothing "
            "pending: it ends the transaction on every exit, which would commit "
            "or discard whatever else is in flight"
        )
    return True


def _contract_error(message: str) -> EnrichmentWriteContractError:
    """Log it as a programming error, then hand it back to be raised.

    Logged here rather than left to the caller because both `commit=True`
    callers swallow exceptions into a warning about the *measurement*. This
    line is the one that survives that.
    """
    logger.error("enrichment write contract violated: %s", message)
    return EnrichmentWriteContractError(message)


def merge_json_object_changes_under_lock(
    prop: Any,
    attribute: str,
    *,
    base: Mapping[str, Any],
    updated: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge this transaction's top-level JSON changes into the stored value.

    ``base`` is the object read before the caller's slow work and ``updated``
    is the result that work produced. Only keys that differ between the two
    belong to this transaction. The stored object is therefore read again
    with the row held and those keys alone are applied to it, preserving a
    different block another transaction committed while the slow work ran.

    The caller owns the surrounding transaction. This helper deliberately
    neither commits nor rolls back: the row remains locked until the caller
    makes its other decisive writes durable in the same commit. The caller
    must also roll back if this helper or that commit raises, so the lock ends
    on every exit.
    """
    from app import db

    try:
        state = sa_inspect(prop)
    except NoInspectionAvailable:
        state = None

    if state is None or not state.persistent or prop not in db.session:
        raise _contract_error(
            "a locked JSON merge needs an attached persistent mapped row"
        )
    if state.identity is None:
        raise _contract_error("a locked JSON merge needs a persistent row identity")
    if not isinstance(base, Mapping) or not isinstance(updated, Mapping):
        raise _contract_error("a locked JSON merge needs JSON objects to compare")

    try:
        mapped_attribute = state.mapper.column_attrs[attribute]
    except KeyError:
        raise _contract_error(
            f"a locked JSON merge was given unmapped attribute {attribute!r}"
        ) from None
    if len(mapped_attribute.columns) != 1:
        raise _contract_error(
            f"a locked JSON merge needs one column for attribute {attribute!r}"
        )

    identity_filter = [
        column == value
        for column, value in zip(state.mapper.primary_key, state.identity, strict=True)
    ]
    statement = (
        select(mapped_attribute.columns[0]).where(*identity_filter).with_for_update()
    )
    # Selecting the scalar column, rather than refreshing the mapped object,
    # leaves this transaction's pending JSON and sibling-column writes intact.
    # Disabling autoflush is essential: flushing first would emit the stale
    # whole-column value before the locked read had a chance to merge it.
    with db.session.no_autoflush:
        stored = db.session.execute(statement).scalar_one()

    if stored is None:
        stored_object: Mapping[str, Any] = {}
    elif isinstance(stored, Mapping):
        stored_object = stored
    else:
        raise _contract_error(
            f"a locked JSON merge found a non-object in attribute {attribute!r}"
        )

    merged = deepcopy(dict(stored_object))
    missing = object()
    for key in base.keys() | updated.keys():
        before = base.get(key, missing)
        after = updated.get(key, missing)
        if before == after:
            continue
        if after is missing:
            merged.pop(key, None)
        else:
            merged[key] = deepcopy(after)

    setattr(prop, attribute, merged)
    flag_modified(prop, attribute)
    return merged


@contextmanager
def locked_write(prop: Any, *, locked: bool, commit: bool):
    """Hold the row while its `enrichment` block is read, decided and written.

    Read the stored block *inside* this block -- reading it before is the
    defect this exists to prevent.
    """
    from app import db

    try:
        if locked:
            db.session.refresh(prop, with_for_update=True)
        yield
        if commit:
            db.session.commit()
    except Exception:
        if locked:
            # The FOR UPDATE is still held and this owns the transaction; end
            # it rather than leave the row locked for the rest of a backfill.
            db.session.rollback()
        raise
