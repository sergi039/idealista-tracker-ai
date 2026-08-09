"""One tiebreaker for every ordered listing (issue #113).

Each sort surface -- `/properties`, `/lands`, both CSV exports and the two
JSON list endpoints -- orders by a single nullable column and stops there.
Rows tied on that column, and the whole NULL run `nullslast()` parks at the
end, then come back in whatever order the database happened to produce for
that one execution: the pages run a query per page, so a row can be handed
out twice while another is never shown; the exports run one query over the
whole set, so file and screen disagree; the JSON endpoints window by
offset/limit and lose rows the same way.

Appending the primary key makes the ordering total, which is what removes the
ambiguity.
"""

from functools import lru_cache

import sqlalchemy


@lru_cache(maxsize=None)
def sortable_columns(model):
    """The mapped column names of `model`, resolved once per model.

    `/lands`, `/export.csv` and `/api/lands` accept any sort name and used to
    gate it with `hasattr(Land, sort_by)`. That asks the wrong question: a
    mapped class also carries `to_dict`, `query` and `metadata`, none of which
    can produce an ORDER BY term, so those names passed the gate and then died
    on `.desc()` -- the page swallowed the error and rendered nothing, the
    export redirected instead of exporting, the API answered 500. Asking the
    mapper which names really are columns sends everything else to the
    fallback branch that already existed for an unknown sort.
    """
    return frozenset(sqlalchemy.inspect(model).columns.keys())


def stable_order(model, *terms):
    """`terms` followed by `model.id`, to be splatted into `order_by()`.

    The tiebreaker is always descending. Determinism itself does not care
    which way it points; what the page, its export and the API window need is
    that all of them pick the *same* way, and branching on the requested sort
    order would only add a way for them to drift apart.
    """
    return (*terms, model.id.desc())
