"""Whether a request wants the withdrawn listings hidden.

`/properties` and `/properties/export.csv` both have to answer it, and until
2026-08-20 they answered it from two hand-written lists of parameter names --
"is any filter present? then this came from the filter form, so an absent
`hide_removed` means the box is unticked". The lists had drifted apart and
away from the filters the routes really apply: the page's was missing `source`
and `advertiser`, the export's was missing those *and* `measured`, and the
export route does not implement `measured` at all.

Naming the missing parameters is not the fix, and on the page it would be a
regression. Measured 2026-08-20 by following every link the page draws, with
and without the `base_args` repair: not one of them diverges, because every
in-page link carries `sort` and `order` and both were already listed. What the
list changes is the *hand-typed* URL, and there it changes it the wrong way --
`?advertiser=owner` reads as a submitted form with the box unticked, so
filtering by seller would quietly start showing the withdrawn listings.

The list was a guess at provenance, and provenance is answerable exactly. The
filter form emits `mode` and `view_type` as hidden inputs on every submission,
and `base_args` in templates/properties.html puts both on every link the page
draws -- pagination, the sort headers, the subscription chips, the two
switches, the mode and view buttons. Nothing else does: a bare `/properties`,
the cross-page links from `/profiles`, `/map` and `/profiles/<id>/edit`, and a
hand-typed query string all carry neither. So `submitted_by_the_filter_form`
needs no list, cannot go stale when a filter is added, and answers the three
cross-page links correctly for the first time -- they used to read as a
submitted form and show the withdrawn listings the bare page hides (#417's
`drilldown_args` docstring describes this hazard and works around it for
`/municipalities` alone, by passing `hide_removed` explicitly).

An explicit `hide_removed` always wins, in both directions, and the marker only
decides the default when the parameter is absent. That is what lets a link say
what it means instead of relying on the reading at the far end: `off` is the
spelling `drilldown_args` already uses, and the Export CSV link now sends it
too, so the page and its own export cannot disagree about a set they describe
together. An *empty* value is not a statement and is read as absent, which is
how `request.args.get(...)` truthiness is read everywhere else in these routes.
"""

from __future__ import annotations

# Emitted by the filter form as hidden inputs, and by `base_args` on every
# in-page link. Deliberately not filters: what is being detected is where the
# request came from, not what it narrows -- which is exactly why this must not
# be folded into a shared list of filter parameter names.
FORM_MARKERS = ("mode", "view_type")


def submitted_by_the_filter_form(args) -> bool:
    """Did this request come from `/properties`' own form or one of its links?"""
    return any(args.get(marker) for marker in FORM_MARKERS)


def resolve_hide_removed(args) -> bool:
    """True when the withdrawn and sold listings should be left out.

    `args` is a `request.args`-like mapping. An explicit `hide_removed` is
    obeyed either way; without one the default is ON, except for a request the
    filter form made, where an absent parameter is an unticked checkbox.
    """
    stated = args.get("hide_removed")
    if stated:
        return stated == "on"
    return not submitted_by_the_filter_form(args)
