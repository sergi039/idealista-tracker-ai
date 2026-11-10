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

## Not done here

Folding duplicates on the `/properties` list is the next step; until then no
screen hides a row because of this rule.
