# /municipalities, /agencies, municipality grouping and search

Moved verbatim from `CLAUDE.md` (lines 1054–1149 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**`/municipalities` keeps municipality facts and listing medians apart, and says
which is which on the page** (proposal D22, #281). Facts are the municipality's
own values — INE renta and población, SEPE registered unemployment — with no
listing involved. Medians are taken over *that municipality's own listings*
(sea, beach, pool, hospital, supermarket, airport, train, price, score): the
owner's decision of 2026-08-14, over a capital-centroid basis, because what he
is choosing between is the listings and not the town halls. The median, never
the minimum — a minimum crowns a municipality because one listing happens to sit
next to a pool. Every metric carries its own coverage count, since a median over
2 of 30 listings is a different claim from one over 30 of 30; sorts put
unmeasured rows last in **both** directions, like the listing table; and a name
the INE join cannot resolve says "not matched" rather than showing a guessed
code (#98's shape, applied to a join). SEPE publishes a *count*, so the page
renders it as a labeled proxy against población and never as the official
unemployment rate. Listings whose municipality is empty or email-truncated
(#298) are counted aside, not compared.

**`/agencies` is a dated measurement, not a live feed** (owner request
2026-08-22). The page ranks the agencies holding the most detached houses up to
300 000 EUR in Asturias and Cantabria, and reads `data/top_agencies.json` --
the fourth committed reference file and the only **hand-curated** one, since
`utils/` holds no importer for it. So `measured_at` is the whole contract: it
says when a person last counted, the page prints it beside the title, and every
count links to the filtered idealista microsite or fotocasa agency page it was
read from -- a number whose query nobody can re-run is not reproducible. A
missing or unreadable file refuses the page with **503** rather than drawing an
empty table, because an empty table reads as "no agencies", which is #98 inside
a reference file. The ranking key is chalets independientes + casas rusticas
together, with the independientes-only figure beside it: those are two different
questions (MN Tu Punto is third on the first and nowhere on the second), and the
table answers both rather than choosing for the reader.
`services/agency_directory.py` owns the read and the ranking -- unmeasured rows
last, the way `/municipalities` and the listing table already sort them -- and
`tests/test_agencies_page.py` parses the **committed file itself**, so the
deployment cannot ship a page that is a 503 by accident.

**`properties.municipality` is free text, so anything that groups by it goes
through one key.** The same place arrives under several spellings — measured
2026-08-16, "Gijón" (57 rows) beside "Gijon" (16), "Castrillon" (28) beside
"Castrillón" (18), 247 rows across 8 municipalities — and both surfaces that
grouped by the raw string reported a partial result as a complete one: the
`/properties` dropdown offered each spelling separately, so picking "Gijón"
showed 57 of 73 listings and said nothing about the rest, and `/municipalities`
keyed on `name.lower()`, which lowercases without stripping accents, and drew
one municipality as two rows with two medians and two coverage counts. The key
is `utils/municipality_codes.normalize()` — the function the INE join already
folds *both* sides of its lookup with — wrapped by `utils/municipality_grouping.
py`, which owns the grouping, the shared filter clause the four listing surfaces
use, and the rule for which stored spelling a human is shown (accents beat
frequency, `MUROS DE NALON` never wins, and the label is always a string that is
really in the table). Do not add a second normalizer, and do not canonicalise on
write: the stored string is what the email said and the input the #298 repair
reads, a derived column would have to be maintained by every writer, and the one
that forgot would hide rows from their own filter — the defect being removed,
relocated. A truncated artifact ("Ovi...") has **no** key: `normalize("Ovi...")`
is `"ovi"`, and folding it into `oviedo` by prefix is exactly the wrong-pick
hazard `resolve_truncated_municipality` refuses. It stays out of the dropdown,
out of `/municipalities` and out of every group's rows.

**The search box also takes a listing URL, and one clause says what it
accepts** (`utils/listing_search.py`). It read `title`, `description` and
`municipality` only, so the most natural way to look one listing up — paste the
link from the alert email — answered "0 properties found" for a row the table
was holding: measured 2026-08-17, `idealista.com/en/inmueble/91523456/` found
nothing while property 351 carried that very id. Two ways in, because two
kinds of URL are stored. The **listing id** (`/inmueble/<id>/`, or typed on its
own) matches `idealista_property_id`, which survives both the `?utm_…` tail the
stored link carries and a language segment that differs from the pasted one.
The **URL itself** matches `url` for the 57 rows of 730 that are fotocasa or an
agency's own site and have no Idealista id at all — after the tracking
parameters are dropped, and only for a query really shaped like a link, since
matching every search against `url` would quietly widen ordinary ones ("terreno"
would pull in every fotocasa link by its path). The four listing surfaces share
the clause the way they already share `municipality_filter_clause`; do not give
one of them its own. Two details are measured rather than assumed: a pasted URL
is matched with `ESCAPE`, because `_` is a LIKE wildcard and slugs are full of
them, and a query of 25 digits names no listing — against this deployment's
PostgreSQL the untyped literal psycopg2 sends returns no rows while the same
value bound to a `bigint` parameter fails with `ERROR: bigint out of range`, so
the guard is what makes the two agree.

**And an empty result says what it looked for.** "0 properties found" was one
sentence for two different facts: no such listing here, and *the query was read
differently from how you typed it* — a pasted link is read as the listing it
names, not as text to match. So when the count is really zero and the query
named a listing, the line beside it says which id, or which link, was searched
for. It renders from `interpret_search()`, the same reading
`listing_search_clause()` is built from, because a page describing a search
that did not happen would be the defect it exists to remove, relocated. Rows on
screen say it already, so the line appears only at zero. **Testing that line
means asserting the page rendered**, not only that the line is absent:
`routes/main_routes.py` turns a template error into a flash and a second render
with no rows, which also shows "0 properties found" and no line — the first
version of `tests/test_listing_search_by_url.py` stayed green through a
mutation that broke exactly that path.
