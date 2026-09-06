# Hazards

Moved verbatim from `CLAUDE.md` (lines 912–1053 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**What is 1.1 km away is a datum, and an empty page was answering "nothing"**
(#437). Property 793 is a plot advertised as a "quiet environment surrounded by
nature" with a cement works 1.12 km off, a coal yard at 1.57, Repsol's LPG
spheres at 1.79 and a coal-fired power station at 2.13; the page showed net
income, supermarkets, hospitals, beaches and drive times, and not one word about
any of it. `services/hazard_service.py` writes `enrichment["hazards"]` -- free,
OpenStreetMap, one query per listing on the *free* pass through
`EnrichmentService._overpass_elements` -- with the four states this file keeps
insisting on: `ok`, `none_within_radius` (a measurement), `unavailable` (a
refusal, never cached, never allowed to overwrite an earlier measurement) and
`no_coordinates`. The card renders on **every** property page, including the
rows nobody has scanned, because the question it answers is "is anything bad
near this plot" and silence reads as no.

**The tag is not the severity, and that was measured before it was written.**
One live Overpass answer at 793's own coordinate, 6 km, ten candidate tags,
committed verbatim as `tests/data/osm_hazards_xivares_793.json`: **144
elements**, of which 82 are storage tanks and 42 are `landuse=industrial`. On
the identical tags sit *Alskin Cosmetics* (`industrial=laboratory`), *Neoalgae*,
*Fábrica de Hielo* -- an ice factory -- *Talleres Prendes*, eight *polígonos
industriales* and a lorry park. So `services/hazard_rules.py` is the sibling of
`services/place_rules.py` and the rule is that **a hazard has to say what it
is**: `plant:source=coal`, `content=gas`, `industrial=steelmaking`,
`landuse=landfill`, or the word *cementos* in its name. `landuse=industrial`
and `man_made=works` never qualify alone. The name has to be read at all
because OSM is sometimes coarser in its tags than in its labels -- El Musel's
coal yard is mapped `landuse=quarry` and the cement works carries no `product`
tag -- but it is read as **words** and the **more severe** of the two verdicts
wins. Both halves of that are review findings with a reproduction behind them:
substring matching read *Bioquímica* as a chemical works and *La Cantera* (the
ordinary Spanish word for a club's youth academy) as a quarry, and letting the
name win outright reported an LPG tank on *Polígono La Cantera* as a moderate
quarry over its own `content=gas` -- understating a real hazard, which is
strictly worse than reporting a spurious one. In the same family, a lifecycle
*prefix* is not a closure: `was:name=Ensidesa` on
*Acería de Veriña - ArcelorMittal* records that plant's renaming history, and
reading it as "gone" erased the one hazard the feature exists to catch, so only
bare `disused`/`abandoned`/`ruins`/`demolished`/`razed` refuse, and `historic`
refuses by value (`monument`, never `archaeological_site`, which describes what
is *under* a working landfill). Do not copy the table into a second caller;
import it.

**Sibling elements collapse into their facility**, which is #325's
hospital-indexed-room-by-room in a new place: `facility_key` takes the operator
before the name, and `merge_keys` folds a name that *contains* an operator into
it, so *Turbina A*, *Turbina B*, two stacks, *Vertedero ArcelorMittal* and
*Acería de Veriña - ArcelorMittal* are one entry and not six. Elements with
neither operator nor name cluster by position at 500 m. But **a key says who
runs it and never where it is**: keyed members are split again by
`FACILITY_SPAN_M` (2 km), because `operator=Enagás` names a national gas
transporter and two of its installations 5.6 km apart collapsed into one item
wearing the near one's distance and bearing. 2 km is measured, not chosen --
the widest real facility in the fixture, ArcelorMittal's tip, acería, turbines
and stacks, spans 1346 m. A cluster is a disc around its anchor and never a
chain, or a line of tanks 400 m apart walks a "facility" across kilometres.
The direction of the guards matters: two rows for one plant over-reports, one
row for two plants hides one.

Four more things are load bearing. **An approximate coordinate cannot support
"1.1 km"** -- `read_verdict` restates the stored block against the row's
*current* accuracy exactly as `sea_distance_service.parcel_measurement` does,
so a centroid gets a band (0.0-6.1 km) and never a point, and the block reports
`guaranteed_m` -- the 6 km scan around the point guarantees only 1 km around the
parcel. A coordinate that has *moved* since the scan is worse than an
imprecise one and gets no restatement at all: `read_verdict` compares the
stored `origin` through `services/enrichment_origin.py` and answers
`stale_origin`, because re-applying today's slack to yesterday's point printed
a centroid's 1.1 km as an exact measurement. And a row that *loses* its
coordinate has its measurement **replaced** by `no_coordinates` -- see "Keep
only what a silence cannot contradict" above; this module kept it until
2026-08-26, alone in the family, and the two things that keep was written to
protect were both measured and neither was real. And the scorer reads `truncated`: a scan
that hit Overpass's element cap and found nothing qualifying is not a clean
neighbourhood, it is a short list, and scoring it 100 was #98 one layer under
a card that disclosed it correctly. **The bearing is recorded and never interpreted**: whether 1.1 km
matters depends on the wind, there is no free per-listing wind rose here, and
writing "downwind" into a measurement field would be the STATUS-002 mistake.
**The criterion ships weightless**, `hazard_score: 0.0` in all six scorers, and
raising it goes through the same dry-run preview the pool weight does --
`WEIGHTLESS_SCORE_KEYS` in `services/property_scoring_service.py` is now what
that gate reads, so a third such criterion cannot be added to a scorer and
forgotten by the gate. And **the scan is deliberately not folded into the
preset Overpass query** the issue suggested folding it into: those presets run
only on the paid path, this runs on every ingest, so sharing would drag a 100 km
aerodrome query into every ingested row to save one round trip on an Enrich
press -- and would invalidate every cached preset cell at a moment when Overpass
was refusing the mini outright (#434).

What OSM cannot answer is named on the card rather than left to be assumed
away: emissions (PRTR-España publishes those, and it is worth its own issue),
measured air quality (Asturias runs a station named *Xivares* inside this very
urbanisation), and a plant approved but not yet built. `utils/backfill_hazards.py`
fills the Phase-2 scope, free and resumable per row; announce it before running
it on the mini, and read `tools/backfill_status.sh` first.

**And what OSM says is not what a `product` tag says.** Three review rounds
against live objects settled the shape of that table, and the settled rule is
narrower than the one it started with. `product=X` names what comes *out*, not
the process that made it: `way/1068457365` is *Balumco*, `man_made=works` +
`product=aluminum`, and the Catalan environmental register describes extrusion
and anodising. Nothing structural separates it from `relation/11519713`,
*Asturiana de Zinc*, which really is a smelter — so no bare metal is evidence,
and AZSA classifies as **nothing** until somebody tags it with a process. That
is the price, and it is written into the table rather than left to be
discovered. In the same family: a lifecycle prefix refuses only the *name*
(`was:name=Ensidesa` on the acería is renaming history, `disused:power=plant`
on a live chemical works is one dead plant on its site), `end_date` refuses on
every documented range form, an operator may absorb a name and is never
absorbed itself, and a *central térmica solar* burns nothing while a nuclear
station is not harmless for burning nothing either.

**Two facts, not one: `complete` and `measured`.** "Carries a complete scan" is
answerable in SQL; "is about this coordinate" is not, and conflating them is
what put a cast over stored JSON in the coverage predicate — where on
PostgreSQL a hand-edited value raises and takes the whole `/properties` count,
and the page, down with it. Nothing in that predicate casts now: the truncation
flag is read as text against one list both languages share, which is also what
makes them agree across a JSON boolean PostgreSQL renders `false` and SQLite
renders `0`. `read_verdict` is the other side and it is **total and
fail-closed** — a readable matching origin, `items` a list, `item_count` an
integer that agrees with it, every measurement finite — because six malformed
shapes each scored a clean 100 or raised into the redirect
`routes/main_routes.py` turns a template error into. A block nobody can read
reads as a block nobody has read.

The scan runs in `enrich_property`'s **advisory pass** (#434/#443), where every
score-neutral step owns its own locked write and nothing decisive waits behind
it. That position is load bearing in both directions: the decisive pass ends by
assigning the whole `enrichment` column from a copy loaded before its network
calls, so a locked write placed *ahead* of it is restored to the older value by
its commit — reproduced with two sessions — and scoring runs *after* the
advisory pass, so it reads the block this scan just wrote.

The same rule governs the scan itself: an element cap reached, a hazard OSM
could not place, an element nobody can parse — each makes the scan *incomplete*
rather than empty, the list badges *Scan incomplete* even with nothing to name,
the CSV carries `Hazard Scan Complete`, the scorer abstains, and
`needs_hazards` puts the row back in the backfill's scope. And the badge on an
approximate row says *near the locality*: 532 of 725 rows share a centroid, so
one scan claiming "Industry nearby" would claim it for every listing in the
village.
