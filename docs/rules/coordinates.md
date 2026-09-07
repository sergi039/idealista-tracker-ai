# Coordinate quality

Moved verbatim from `CLAUDE.md` (lines 785–847 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**A coordinate that is not the parcel measures nothing about it, and every
consumer asks before it measures** (#358). `location_accuracy` is Google's own
word for what it matched: `precise` is an address, anything else is a locality
centroid. At 11:08 UTC on 2026-08-16 that was **532 of the 725 located rows**,
280 of them sharing a point with another listing across 78 points (21 listings
on the worst one, 39 of the sharers labelled `precise`). Two hours earlier the
same query said 466 of 652: the set grows with every ingest, so re-measure
rather than quoting these. `sea_view_service` has refused such
a point since #196; `sea_distance_service` and `property_travel_service` did
not, so a listing scored the centroid's sea distance and the centroid's drive
times, and nothing on the page said so. Properties 460, 461, 574 and 641 are
four different streets of Santa María del Mar resolved to one point 23.8 m from
the coastline — including `San Miguel de Quiloño s/n`, which is inland by the
airport. It is the *fourth* way a geocode goes wrong and the first three guards
all pass it: not a coarse type (#331), the right province (#348), an accepted
accuracy label (#321). Right place, wrong precision.

The policy has one home, `services/coordinate_quality.py`, and it is
`sea_view_service`'s idea rather than a second one: an approximate coordinate
may still decide a question **when the answer cannot change anywhere inside the
5 km slack**. So a measured distance is scored only if its lower and upper
bounds score the same — which for a precise row is one number twice, so nothing
about that path is special-cased — and a travel duration only if both ends of
the slack, converted at the mode's own assumed speed by
`estimate_duration_seconds`, land in the same flat region. "No coastline within
17 km of the centroid" survives it, which is why `searched_m` on an approximate
row is 12 km: the radius the answer is guaranteed for *around the parcel*.

Three consequences worth knowing before changing it. **Travel used to refuse
before the Places and Distance Matrix calls, and since 2026-08-17 it does not**
(owner decision). #358 built three things and only the purchase refusal is
gone: the scorer still applies the slack target by target, and every surface
still derives `approximate_origin` from the row's accuracy and captions it, so
what comes back is a measurement and not the claim that it is the parcel's —
the state 115 rows in `Plots 0–50 km` have carried since before #358 landed.
The reason it can go is that the rows the owner wants numbers for cannot become
precise: their adverts give a hamlet or hide the address, so a re-geocode
returns the same centroid, and the choice was a measured duration that says
what it is against no answer at any price. What it costs is that a recalc over
such rows no longer walks past them, and whether that bills depends on who
routes: free with `OSRM_URL` set (#416; the mini since 2026-08-20), a billed
Distance Matrix request per listing without it — so read the billing rule
above before pointing one at a wide scope on a machine where Google still
routes. Nothing
unattended reaches that code (`AUTO_TRAVEL_ENRICHMENT` is false), which is what
keeps the lift a spending decision rather than a spending default. The run
records `api_status.origin_accuracy`, and the only refusal left is a row with
no coordinate at all. The scorer and the templates read the row's *current*
accuracy through `parcel_measurement` and `effective_travel_state`, not the
stored block, because 264 sea blocks and 532 travel blocks say `ok` from runs
that predate the rule and no amount of re-reading them reveals it. The
exemption is asked of the travel *average*, never target by target: 690 of
those durations sit under `best`, where the slack cannot move them, against 9
past `worst`, so keeping the individually-safe ones would keep the near ones
and report ~100 for a listing whose real mix is nothing of the sort. And a
shared
coordinate is surfaced as evidence, never as a gate: two flats in one building
share a point legitimately, the coordinate alone cannot tell that from four
plots on a centroid, so it is shown next to the coordinate and counted by
`utils/report_coordinate_quality.py` (free, read-only) — repairing the rows is
`utils/refresh_property_accuracy.py`, which is billed and therefore the owner's
call.

## `precise` earns its zero only by answering the address asked (#535, 2026-09-07)

**`precise` is earned only by an answer to the address that was asked** (#535).
`coordinate_slack_m()` gives a `precise` row 0 m of slack, and until #535
nobody had asked whether the label earned it: #493 measured what `approximate`
is worth and stopped there. Measured on production 2026-09-02, 152 of the 166
`precise` rows were the geocoder's — every one a Google ROOFTOP by
construction — and **not one carries a second coordinate** to check against;
the one ROOFTOP a person checked, row 360 ("Barrio de Prendonés, 1"), sat
2868 m from the parcel, on a travesía named after the village. The stored
record could not even say what the label rested on: 0 of 1727 records kept
`location_type`. What it *can* say for free is whether the ROOFTOP answers the
address the query named, and for 23 of the 152 it did not — 17 answered a
house number to a query that named none (1379: "calle Fiobre, Bergondo" →
"Rua Fiobre, 100"; the 100 is Google's), 6 answered a different street or
number (355: 83 asked, 10 answered).

The rule has one home, `services/address_agreement.py`, and it compares the
one thing the record supports: the **house number**. A ROOFTOP is Google's
claim to have matched one building; a query that named no building cannot have
earned it, a query that named a different one refutes it, and the label is
then `approximate` — what any other answer to that query is worth — with the
slack that label already carries. `_geocode_outcome` decides it *before* the
even-trade comparison, so a withdrawn `precise` cannot displace a portal pin
either, and the record keeps `location_type`, `address_check` and, when the
label was withdrawn, `answered_accuracy: precise` beside the one stored. What
the fix deliberately does not do is what the issue names: it does not widen
`precise` globally (a cannot-tell — a caserío matched by name — keeps the
label), and it does not relabel from a displacement.

The street name is not compared, and that is measured: Google answers in the
local language and its own abbreviations, and the hand review discarded 13 of
36 token mismatches as spelling. So two blind spots are pinned as passes in
`tests/test_issue_535_precise_earns_its_slack.py`: a same-number answer
somewhere else (360) and a different street with the same number (25:
"calle Tarancon, 6" → "Av. de Salamanca, 6"). Only a person's pin catches
those.

The rows geocoded before the rule existed are read by
`utils/audit_precise_accuracy.py` from their stored `query` and
`formatted_address` — re-geocoding is billed and deterministic, so it would
answer the same thing. Re-run over the 168 `precise` rows on 2026-09-07 it
refutes 21: 20 of the hand review's 23 plus row 1759, ingested after the
review, and none of the 129 the review passed. The 15 a naive form flagged
beyond that were the same number twice — a letter suffix Google added or
dropped ("24" against "24b"), a number the query wrote without a comma
("Barrio Otero 15") — refinements, not contradictions. A person-established
row is skipped whatever shape the finding took (a hand-set block, an ad-hoc
provenance block, a coordinate standing on the cadastre reference point), the
tool is dry-run by default, `--apply` is refused without `--snapshot`, and
`--restore` is `utils/refresh_property_accuracy.py`'s own, which refuses to
overwrite a location a person set after the snapshot.

