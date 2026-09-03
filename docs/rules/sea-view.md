# Sea view

Moved verbatim from `CLAUDE.md` (lines 415–593 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**Sea view is a four-state verdict, not a flag** (`services/sea_view_service.py`):
`yes` needs the listing text and the terrain to agree, `likely` is one source
unopposed, `no` is computed and negative, and `unknown` means it could not be
computed — an approximate coordinate, or a source that refused. `unknown` is
never folded into `no`, and the filter never counts it as a match. Geometry
alone stops at `likely` on purpose: Copernicus EU-DEM is a *bare-earth* model,
so trees and buildings are invisible to it. A hand-set verdict on the property
page outranks both: a recalculation reads the row under a lock and leaves a
hand-set one untouched. Both sources are free and
keyless — OpenStreetMap coastline, EU-DEM 25 m via OpenTopoData — so Google
billing is not involved. Fill it with `python -m utils.backfill_sea_view`;
`enrichment.environment.sea_view` is where it lands, and mirrored `Land` rows
keep their old boolean at `enrichment.legacy_land.environment.sea_view`, which
reads as `likely` (it came from the same weak keyword pass) and never as `yes`.

**Keep only what a silence cannot contradict.** One rule for the whole
enrichment family — sea view, sea distance, hazards — settled on 2026-08-26
because two of them had drifted into opposite answers for one shape of fact. A
stored measurement survives a re-run on exactly one condition: **the subject is
unchanged, and the run learned nothing because a source did not answer.** A
refusal is that, and it is the whole of it. Anything else overwrites — a
coordinate that has gone, one that has changed, one that has lost the precision
the stored claim rested on, and a source that *did* answer even when its answer
is "I have no data at this point".

The case that split them is a row carrying a measurement taken at a coordinate
it no longer has. `sea_view_service` and `sea_distance_service` overwrote;
`hazard_service` kept, and its reasoning — `no_coordinates` is not retryable,
so overwriting takes the row out of the backfill's scope for good — was the
right worry aimed at the wrong mechanism. Measured: `needs_hazards` reads
`read_verdict` and not the stored status, so a row stored `no_coordinates`
stays in scope either way (the codex review that fixed *that* landed later and
nobody went back to the keep); `RETRYABLE_STATUSES`, the constant the claim
was written on, was dead code nothing imported; and `read_verdict` refuses to
assert a kept measurement anyway, so it showed on no surface. Keeping bought
one free Overpass query in the single case where the identical coordinate
returns, and cost one wrong number — `complete_expression` counted a row the
app cannot locate as carrying a complete scan.

The reproduction is what settles it rather than the symmetry. A full
`utils.backfill_sea_view` on 2026-08-26 moved six production rows from a
measured `no` to `unknown`, and the pre-run snapshot
(`data/sea_view_pre_fan_snapshot_20260826T134313Z.json` on the mini) says five
of them — 128, 132, 170, 174, 175 — were measured at **40.463667,-3.74922**,
the centre of Spain: what geocoding a #298 truncated title fragment ("Finca
Offers For Sale This Buildi") returns. Their coordinates have since been
cleared, and preserving those verdicts would have preserved a claim about
Madrid. Row 132 is **Carreño, on the coast**, and its stored
`no_coastline_in_range` was a false negative that survived only because it was
measured 400 km inland. The sixth, 149, moved between two inland centroids.
Exposure of the change itself: zero rows either side — 881 hazard blocks and
none on a coordinate-less row, 247 measured sea-view geometries and none whose
origin disagrees with its row.

Two things the rule does **not** move, and both are load bearing. A verdict
measured at a `precise` coordinate that has since decayed to `approximate`
sits on the *same point*, so an origin check waves it through — and it is
still overwritten, for #196's reason, in
`sea_distance_service._last_known_good`'s own words: *"same point, different
claim"*. The stored verdict was a claim about the parcel; the row has just
lost the right to one. And sea view's `no_elevation_at_property` stays outside
`SOURCE_REFUSAL_REASONS`: the subject did not move and the source did not go
quiet — EU-DEM answered, and what it said was that it has no ground there. A
computed answer is not a silence. It has never fired on production (0 rows),
and it is named in the module so that its absence reads as a decision.

Where the two modules still legitimately differ is *where* the rule is
enforced, and that is the thing not to harmonise. A hazard block is a list of
facilities that has to be restated on read anyway (the slack band, per item),
so its reader is the natural place to refuse — and it still answers
`no_coordinates` / `stale_origin` for a measured block that arrives on a row
that moved, because direct SQL is a supported workflow and a reader that
refuses a shape only because today's writer cannot produce it is a reader that
trusts the writer. A sea-view verdict is one word in a column that four
surfaces and a SQL filter read; there is nothing to restate, and `unknown` is
precisely the state the module invented for "not computable for this
property". **Sea view's storage is its restatement.**

**The coastline does not follow the rías inland, and the verdict now records
the point it measured to** (#334, the "Selorio report" of 2026-08-15 — it
arrived as a direct owner request and was filed retroactively). The reported
defect was that OSM's
`natural=coastline` runs up an estuary to the tidal limit, so a plot 4 km from
a ría scores a sea view against it. Measured against real OSM data for all four
candidate rías, it does not: the coastline **closes across each mouth** and the
estuary is mapped separately as a `natural=water` / `water=river` multipolygon
reaching much further in — Villaviciosa 7.13 km past the nearest coastline
node, Avilés 4.60, Navia 4.90, the Nalón 4.37 and 7.29. Not one coastline way
in any of those four boxes carries a name either (the only named ones are rocks
and islets near the Nalón, *Islote La Ñera*, *El Peñón*), so a rule keyed on a
named ría way has nothing to read. The nearest coastline to an inland plot is
therefore the mouth, which *is* open sea, and property 125's `likely` is
correct: re-run against live EU-DEM it gives `clear_line_of_sight` to the mouth
at 4173.8 m, and so does a ray on the same bearing to open water 5839 m out,
with 9 null (over-water) samples confirming the far end really is sea.

What was wrong is what the verdict *said*. Its sight line runs 2.8 km up the
ría channel before it reaches the sea, and the page announced "Sea view likely"
either way. So `geometry.target_lat` / `target_lon` now record the coastline
node — additively, with no cache bump, because the field changes no verdict and
re-querying 67 cells through a 5 s gate to gain it is not worth it — and the
property page, the list and `properties/export.csv` carry it. A distance and a
bearing are not a substitute: both are stored rounded to one decimal, so
casting the ray back out lands metres off the node, which is enough to put the
reconstruction on the far bank of a 300 m channel. And a `likely` resting on
terrain alone is now named *"Terrain allows a sea view"* rather than asserting
one — `state_label_key()` in `services/sea_view_service.py` is the single home
of that distinction, for the three templates that draw the badge. What it must
not do is soften a `likely` the listing itself claims, or a hand-set one.
`tests/test_sea_view_target_recorded.py` pins all of it against the real
coastline in `tests/data/`.

**The nearest coastline node is the hardest target on the coast, and for a long
time it was the only one asked about.** One profile ran to it, and a blocked
sight line was written down as "No sea view" — but the water's edge nearest a
house is the *first* thing rising ground occludes, while the open sea beyond it
stays in view. Measured on production 2026-08-26: of the 124 rows where a
profile actually ran, **120 answered `terrain_blocks_line_of_sight` and 4
answered `clear_line_of_sight`**, 85 of the 120 blocked before half the distance
and 66 before a quarter. A model that says no to 97% of the coastal rows it can
compute is not answering the question it was asked, and on this coast that is
the question people buy houses for.

Property 1282 (Seiruga, Malpica) is the shape of it: eye at 50.2 m, water's edge
394.7 m out on bearing 21°, a 41.0 m brow at 91.1 m. Every number right — the
sight line is at 38.6 m where the ground is at 41.0 — and the bay and the
Sisargas are in the listing's own photographs.

So a blocked shoreline ray now runs a **fan**, and the fan asks the other
question: is any *sea surface* visible, rather than is that one node. Five rays
out past the shore, each to a run of null EU-DEM samples — the model has no
value over open water, which is the same reading `null_elevation_samples` has
always recorded. Live against real OSM and EU-DEM, 1282 reproduces production's
shoreline numbers exactly and then finds water at 673 m on bearing 21.0, inside
the 600–650 m band the owner measured by hand over 21 bearings.

Seven things about it are deliberate, and most were measured rather than
chosen. The **bearings come from the coastline**, not from a sector invented
around the nearest node: a house on a headland has water at 20° and at 200° with
land between, and one extended ray or a fixed sector looks at one and calls the
other absent. Selection is farthest-point sampling on the circle seeded with the
nearest node, so the first ray *is* the profile that was already run and the
rest spread — coastline nodes crowd where the shore is nearest, so picking the
five nearest aims the whole fan down one street. It is **one extra
OpenTopoData request and only on the path that used to answer `no`**, because
that endpoint batches: 1 observer + 5 × 19 = 96 inside the 100-location cap, and
`_probe_plan` *derives* the fan from `SEA_VIEW_ELEVATION_MAX_LOCATIONS` so a
lower cap costs resolution rather than raising out of `fetch_elevations` on
every blocked row. Sampling is **quadratic, not even**: 19 even samples over
3 km start at 158 m and step clean over the 91 m brow this exists for, and a
ridge stepped over is a false *positive* — the same defect mirrored. A **run of
two nulls** is required, because `None` is also a hole in the model; and being
in the run and being visible are separate questions, since water gets easier to
see the further out it is (the line to sea level falls away as `-H/d`), so a ray
that leaves the land at a hidden shore has an invisible first water sample and
visible ones behind it — requiring the run to *start* visible reported the whole
ray blocked, and that bug was in the first implementation. A **fan the elevation
model refused is `unknown`, never `no`**: the shoreline half was never enough
for a negative on its own (#98), and `elevation_source_unavailable` is already
in `SOURCE_REFUSAL_REASONS`, so `repaired_with_stored_geometry` keeps whatever
an earlier run measured and nothing is cached. Geometry still stops at
`likely` — the far field stays coarse and bare earth still cannot see the pine
wood, so the ceiling is what bounds the cost of both. And the **cache key went
`_v1` → `_v2`**, against the #334 rule that additive fields do not earn a bump:
that rule cuts the other way when the answer moves, and a cached `_v1` `no` is
one of the false negatives.

**"The shore below is visible" and "the sea is visible" are two facts**, and the
page said only the first while meaning the second. `shoreline_visible` is
recorded on every computed verdict, the fan's evidence sits beside it in
`sea_probe`, and `state_label_key()` gives the second reading its own name —
*"Sea visible over nearer ground"* against *"Terrain allows a sea view"*. One
label for both would put the hillside house back exactly where it started.
The 120 stored rows keep their `no` until re-evaluated;
`utils/backfill_sea_view.py` is what moves them, it is free, and the announce
rule for the mini applies. `tests/test_sea_view_over_nearer_ground.py` builds
1282's own terrain — a near brow with open water past it — because an abstract
ridge blocks everything and reproduces nothing.
