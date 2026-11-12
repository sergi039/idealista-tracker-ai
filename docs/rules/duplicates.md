# Publications of the same home (#633)

The long form of the "Publications of the same home" rules in `CLAUDE.md`.
One home is often advertised on several portals, and more than once on one.
`services/property_duplicates.py` reads the table and says which rows are
publications of the same home; the owner settles the doubtful pairs. No row is
ever merged or deleted by any of it.

## The rule is a reading, not a column

Computed on read over rows already loaded; no SQL twin, nothing stored (the
pattern of `services/favorite_similarity.py`). A missing fact abstains: it
never corroborates and never vetoes (#98).

Three tiers, each carrying the evidence that earned it (`explain(a, b)`):

* **confirmed** -- the same portal listing id across the idealista <->
  yaencontre pair, an entry in the five hand-checked seed aliases
  (`recommendation_duplicates.CONFIRMED_ALIASES`, source "seed"), or the same
  unit-level cadastral reference (18 or 20 characters; a 14-character parcel
  reference names a whole building and abstains).
* **probable** -- the three fields (municipality key, price, built area) and
  one corroboration: the same place named in the title, or coordinates close
  enough (200 m on one portal, 1 km across portals; a locality centroid never
  counts).
* **possible** -- the three fields alone. Reported, never grouped.

## What was measured on 2026-11-10

* Three-field groups: **223** (2549 rows read; 76 groups cross-portal, 147
  same-portal). Three fields alone collide by chance in about 2-5% of pairs,
  which is why they are only `possible` without a corroboration.
* **211 confirmed pairs** by a shared idealista <-> yaencontre listing id
  (yaencontre re-publishes idealista adverts under the same number): 47 of 47
  inspected pairs were the same home, although price differed in 4 and area
  matched in only 3.
* **Idealista `area` is not evidence**: 108 of 118 stated values are exactly
  150.00, a suspected placeholder (#645). It neither supports nor vetoes a
  match (`AREA_DISQUALIFIED_PORTALS`). Bedrooms, bathrooms, photos and
  descriptions are not evidence either: they disagree inside true duplicates.

## The veto

Two different unit-level cadastral references are two different homes. The
veto outranks every other signal, a shared listing id included, and a vetoed
pair never shares a group, directly or through an intermediate row
(`_Union`: each component tracks its references; a join that would put two
into one is refused and reported in `refused`).

## The owner's word outranks the rule

Table `property_duplicate_link` (migration 029): one row per unordered pair,
`property_low_id < property_high_id`, `verdict` `same` | `different`, the
rule's `explain` output as `evidence`, an optional note, UTC `decided_at`.

* `property_duplicates.set_link` is the ONE writer. It refuses a self-link, an
  unknown id, an unknown verdict and a note over 500 characters
  (`LinkError`, fixed safe messages); takes both property rows `FOR UPDATE`
  one statement each in id order (the 026 discipline, so opposite-order callers
  queue instead of deadlocking); upserts; commits inside. There is no
  `commit=False`. `verdict=None` deletes the link and so restores the computed
  reading.
* `groups_with_links(rows, links)`: owner `same` pairs are confirmed edges with
  source `owner_link`, applied BEFORE every rule edge and allowed through a
  cadastral veto; owner `different` pairs are `forbidden_pairs`, so they can
  never share a group, even through an intermediate row. An owner `same`
  blocked by another owner `different` is refused and reported, never silently
  dropped. A `same` over a veto is allowed, and the stored evidence records
  `vetoed_by`, so the override is never silent. A component the owner joined
  across two references is frozen against further rule edges: the weaker side
  loses.
* The five seed aliases stay as they are in `recommendation_duplicates`.

## The detail page

`publications_for(prop)` feeds the "Other publications of this home" panel and
is bounded: at most three indexed lookups inside the row's own subscription
(the three-field block by price and built area, the shared or seeded listing
id, the same unit-level cadastral reference), each `LIMIT`ed to
`CANDIDATE_LIMIT`, plus the rows this one is linked to. The rule, not those
queries, decides; they only narrow. The form route
`POST /properties/<id>/duplicate-link` takes `other_id` (the panel's buttons)
or `other_ref` (an id or link typed into the "link another listing" box,
resolved with `utils/listing_search.listing_reference_clause` inside the
subscription; an ambiguous or unknown reference is refused). Pairs are judged
within one subscription, so linking across subscriptions is refused; undoing
an existing link is allowed from anywhere. The panel is absent when there is
nothing to show and no link exists; the link box is always offered, because a
pair the rule missed is exactly when the panel is empty.

`python -m utils.report_duplicates --profile-id N` applies the same links and
prints how many were applied; it writes nothing.

## The fold on the list (`services/duplicate_fold.py`)

`fold_query(query, params, keep_ids=())` is the one call. The list, the CSV
export, `/api/properties` and the map all make it, so they cannot disagree.

* **Where.** In `properties()`: after the rejected hide (the last narrowing of
  the selection) and before every count beside the result count and before
  pagination, so page size, page count, sort and each "K of N" line are about
  the rows shown. In the CSV, API and map it sits after their last filter
  (the Recommendations selection included) and before ordering, `total` and
  the subscription mix.
* **What "selection" means.** The rows the surface is about to draw. A row an
  earlier filter removed (portal, search, Hide removed, criteria, the rejected
  hide, Favorites, subscription) takes no part: it neither folds another row
  nor is counted. Search for one publication and you get that publication;
  filter to one portal and each portal's row stays. Groups are judged across
  subscriptions inside the selection.
* **Tiers folded**: `confirmed` and `probable`, owner links applied
  (`groups_with_links`). `possible` never folds. The representative is the
  rule's (`representative`).
* **Protected, never folded**: a favourite, an owner decision, an outstanding
  action, a live timeline entry (`property_duplicates.protection`).
* **Cost.** Three statements whatever the size: the rule's columns in one query
  (ids, municipality, prices, area, title, url, source_email_id, listing id,
  cadastral reference, coordinates and accuracy, listing status, the
  favourite/decision/action columns, and the two small coordinate
  sub-documents `enrichment->import->coordinate` and `enrichment->location`;
  never the ORM object or its JSON blobs), the owner's links, and the timeline
  ids of grouped rows (`tests/test_duplicates_fold_on_the_list.py` bounds it).
  A selection larger than `FOLD_MAX_ROWS` (20,000) is not folded; the page says
  so (`#duplicates-fold-skipped`, a `scope` note in the API). On the list, a
  narrowed page costs three more statements for the "N of M" baseline and an
  empty page three for its "if cleared" count, because the page those links
  land on folds.
* **Parameters** (filter-only, neither a sort nor a view state):
  `duplicates=show` lifts the fold; `duplicate_of=<id>` shows exactly the group
  holding that id, unfolded, and an unknown or unusable id is the ordinary
  empty result (a typo never widens the page). Neither is a filter-bar
  dimension; both are in `base_args` so sort and pagination keep them, both
  travel in the form as hidden inputs, both are chips that remove through the
  existing mechanism, and the clear-filters link drops them.
* **Filter memory.** `duplicates=show` IS remembered, like `verdict=all`: it is
  a standing choice of how to read the page, and the page's own links carry the
  `mode`/`view_type` markers that make a request remembered. `duplicate_of` is
  NOT (`NOT_REMEMBERED`): it names one row's group, like the `remember=off`
  reveal link a listing page draws.
* **The row.** The representative (and any visible member of a group that has a
  folded member) carries one badge "N publications" (plus "M withdrawn" from
  the loaded status), the other portals as links through `safe_external_url`,
  and "show all". When the members' prices differ the range sits under the
  price; sorting uses the row's own price.
* **Counts.** Every count on the page is of the rows shown (after the fold):
  the result count, pagination, the verified/hazard/score/taste coverage lines,
  the similarity numbers and the "N of M" baseline. The exceptions are counts
  inside controls -- the subscription chips, the filter-option labels and the
  New/Reviewed switch -- which are of the unfolded selection, because a count
  per option would need one fold per option. The disclosure line makes it add
  up: rows shown + folded = the unfolded total.
* **CSV and API rows** carry `duplicate_group_size`, `duplicate_member_ids`
  (sorted; space separated in the CSV) and `duplicate_tier`; an ungrouped row
  has size 1 and the other two empty. The API `scope` carries
  `duplicates_folded` (null when lifted or skipped).
* **The map**: one pin per group; a `focus` id is never folded away.
* **Recommendations** keeps its own grouping
  (`services/recommendation_duplicates.py`: the five seed pairs, each needing
  independent corroboration) and is not folded by this rule on top of it: a
  shared listing id alone is `confirmed` here, but the collection deliberately
  asks for corroboration. Groups are still computed for the CSV/API columns.

## A decision stays on its row

A decision is written to the clicked row only (`set_review`). Nothing is fanned
out to the group and no verdict makes another row disappear. Because a decided
row is protected, deciding the representative leaves it on the list; the rest
of the group folds under it on the plain list (the badge says so) and stays in
the New queue, which excludes the decided row from the selection. The badge's
tooltip says it. A later change may let "rejected" hide the whole confirmed
group; this one does not.
