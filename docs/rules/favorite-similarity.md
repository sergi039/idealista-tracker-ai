# Similar to the favorites

Moved verbatim from `CLAUDE.md` (lines 1701–1829 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**"Similar to the favorites" is a reading of the table, not a model, and it
is per subscription** (owner request 2026-09-02: from the whole Galicia
subscription, the listings most alike the two they starred, as a filter of
its own; the code is generic, the owner's use is subscription 24).
`services/favorite_similarity.py` compares every row of a subscription with
that subscription's OWN favorites — the criteria rule, one axis over — on
the facts both sides state: price and built area as log ratios (0 at twice
or half; the built area on `subscription_criteria.effective_figures`, so a
parcel is never scored as a house and never twice), location on a 60 km
linear scale, the plot through the same reading, bedrooms/bathrooms as
count differences, the sea view's bucket, and the sea distance on the
`sea_dist` filter's own two keys — but that one only where the answer is the
same at every point the coordinate's slack allows (#358, the scorer's rule):
a non-precise side is a 5 km band against a 2 km scale, so it is 0 when the
bands cannot come within the scale, the plain figure when both sides are
precise, and absent otherwise (on production: 6 rows scored, 19 at 0, 475
abstain). A fact one side lacks abstains and lowers the coverage the row
reports; it never scores 0 (#98). Two gates rather than components, both
measured before they were written: a different **kind** — category and
subtype folded into one word, `land` whatever the legacy `developed` says,
since sub 17 stars 14 plots beside 2 "developed" parcels — or, for houses, a
different **typology** on the title head (`house_typology`: the owner's own
/agencies definition, adosados and pareados against chalet independiente /
casa rural / casa de pueblo; a bare "Chalet" states nothing and gates
nothing) — 56 of subscription 24's 565 rows, 34 of them attached houses; and
a row that cannot be placed (`thin`: no coordinate, and no located row in
its municipality — the 5 Ares rows), which keeps its number for the reader,
muted, and never passes a cut or ranks. **299 of 543 Galicia rows have no
coordinate, so the municipality point exists**: the median of the
coordinates the whole table holds under the key `/properties` groups
municipalities by, derived on read, never stored, named as the basis with
the number of rows that made it (`municipality (3)`, against `approximate`
for a row's own non-precise coordinate — a pin or a locality centroid, the
label does not claim to know which — and `coordinate` for a precise one;
the favorite's own basis is recorded too and the row's page prints it when
it is the looser side, since sub 6's favorite is a centroid and its plots
are precise).
Measured: a 5 km slack is 8.3 points of the location component; leave-one-
out of the point over 228 located rows moved the total by median 0.0, p90
1.5, max 5.4, and flipped 0 rows at the 80 cut, 3 at 70, 5 at 60. The
favorites read as `reference`: kept by every cut and carrying the highest
sort key — they lead the descending order and close the ascending one, which
lists the least alike first, and a duplicate of a favorite under a lower id
cannot lead — and the line beside the count says what the rows were measured
against — "Similar: 42 at ≥ 70 to 2 favorites" on production, "Similar: 500
of 565 rankable against 2 favorites" without a cut, with a tooltip that
counts what a missing chip means (cannot be placed, a different kind, no
favorite to compare to) and how many of the kept rest on price, area and
location alone (30 of the 42), since most rows state three facts and the
chip says so ("≈ 87.6 3/8"). Under several favorite-holding subscriptions on
screen the line says "each subscription's own favorites (N in all)": the
references are per subscription, and a summed basis is one no row was
compared against.

**A listing the owner has rejected is not offered as similar** (owner
request 2026-09-03). They turned one down for reasons the table holds no
column for — "маленькие комнаты, рядом много построек" — and the cut went on
presenting it at 83.1, because the facts it CAN see (265k, 483 m², Laxe)
really are close to a favorite. `rejected` is therefore a state of its own:
the score is still computed and shown, the row's own page says how alike it
looked and that it was left out, and the line beside the count says how many
were set aside — counted off the page's own selection with the similarity
clause left off, the `criteria_hidden_count` shape, because the summary
describes the rows that SURVIVED and under a cut the rejected ones are gone
from it (measured on production 2026-09-03: it read `0 you rejected` exactly
where the number was needed) — a row that vanished in silence would read as a defect. It
is the ONE verdict that removes a row: `interested` and `waiting` are not
refusals, `undecided` is most of the table, and a rejected FAVORITE stays a
reference, because the star is what defines the selection. The verdict is
read through `owner_review.read_decision`, never by a second test of the
column.

Four things about the wiring are load bearing. **There is no SQL twin.**
The reading is Python, once per request — over the subscriptions on screen
plus whatever the URL named on an ordinary page, over EVERY favorite-holding
subscription under a cut or a similarity sort, because then the chip counts
and the hidden-subscription note run the clause with only the subscription
left open and a row nobody scored would count as 0 while naming it opens it
— and the clause and the sort key are *derived* from it (`id IN (...)`,
`CASE id WHEN ...`), so the list, the map, the CSV, the API and the row's own
page cannot disagree. The loader reads JSON leaves as text and three small
sub-documents and parses in Python, never a CAST in SQL: a hand-edited value
raises on PostgreSQL and takes the whole page with it (and this runs on
every page load, where the `sea_dist` filter's own casting expression runs
only when that filter is on); measured on production, ~80 ms for
subscription 24's 563 rows, ~124 ms for all 978. The detail page hands in
`candidate_ids` and pays for one row. **A known cut always narrows**,
favorites or none: a subscription without one has nothing to be similar to,
and a page showing every row under a control reading "≥ 70" would be a
filter that did not apply — the API says "selected nothing: subscription N
holds no favorites", the page keeps the control on screen so the cut can be
undone where it was applied, discloses the narrowing and offers the clear
link, which is built from the record of the request
(`_clear_filters_url`, the `_empty_state_scope` precedent) and never from a
list of filter names. This is deliberately not the criteria module's dormant
rule, because there the absent parameter is a hide and here it is nothing.
**Under the Favorites switch the similar rows cannot be on the page** (they
are never favorites, by definition), so the disclosure counts with the
switch lifted and says the switch is what hides them — the owner's own URL
carries `favorites=on`, and "Similar: 0 at ≥ 70" there would have read as
"nothing resembles them". And **one rounding rule**: the score is kept and
printed to one decimal, so a 79.6 never prints as 80 while the ≥ 80 cut
leaves it out. The cut's vocabulary is numbers (`similar=80|70|60`), the
`sea_dist` precedent; picking a cut moves the sort select to `similarity`
(and the order to descending) unless the owner chose a sort, a hand-typed
cut with neither a sort nor a mode named orders by likeness, and a chosen
mode keeps its own order. With no favorite anywhere there is no likeness to
order by: the Similarity option is not offered, and a `sort=similarity` typed
into the URL falls back to the mode's order or the date the way an unknown
sort does, rather than ordering by the tie-breaker under that label. What
the reading cannot see, and says so in its
docstring: a municipality whose only located rows are wrongly geocoded (the
median shrugs off one among several and cannot with one), a listing-pin
coordinate (#524, which abstains on the wide side like a centroid), and the
**Galicia's** two favorites' own plot, which on production sits under a
dossier key (`attributes.plot_area_cadastre_m2`) and not in the column, so
the plot component is dormant **there** until the column is filled, by a
hand-set writer with a source note on the owner's word and not by a bare
UPDATE. It is not dormant generally: 18 of the 22 favorites are bare land
(16 `plot`, 2 `developed`, measured 2026-09-02), and for those `area` IS
the parcel through `effective_figures`, so the component is live on the
land subscriptions and carries the only surface those rows state.
`tests/test_favorite_similarity.py` pins the reading by value (the neighbour's
84.1 is hand-computed component by component) and every surface; the sweeps
in `tests/test_map_and_list_agree_on_the_filters.py`,
`tests/test_api_properties_reads_the_pages_filters.py` and
`tests/test_the_pages_own_counts_read_the_criteria.py` walk the cut with the
rest.
