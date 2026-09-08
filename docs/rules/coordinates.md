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


## What a ROOFTOP is worth here, asked of the cadastre (#559, 2026-09-08)

#535 could only compare the two strings the record already held. It said so,
and it named what it could not reach: whether a ROOFTOP that *does* answer the
address asked earns the 0 m `coordinate_slack_m()` grants it. 0 of the kept
rows carried a second coordinate, and the one row anybody had ever checked by
hand (360) was 2868 m out — a sample of one.

**The second coordinate exists and it is free.** Catastro's
`Consulta_RCCOOR_Distancia` answers, for one point, which parcels lie at or
near it, each with its cadastral house number and its distance in metres. So
"is Google's ROOFTOP standing on the parcel whose number the query asked for?"
costs one keyless request per row. `services/cadastre_locate.py` is that
reader and `utils/audit_precise_against_cadastre.py` runs it over the cohort;
neither writes anything, because #559's own list of what must not happen has
"do not relabel from a displacement alone" on it, and a tool that could write
could do that by accident.

**Measured over all 130 rows of the cohort on 2026-09-08** (every row carrying
`precise` whose ROOFTOP agreed with the query, `enrichment["location"]` rows
excluded and named):

| what the cadastre says | rows |
|---|---|
| the coordinate stands on the parcel numbered as asked | 58 |
| displaced, and the distance measured | 26 |
| neither confirmed nor placed | 46 |

Of the 26 measured displacements, 17 are within the neighbour list's ~25 m
reach and **9 are not: 62.6, 62.6, 69.0, 69.0, 103.6, 130.4, 336.6, 448.3 and
1783.3 m.** Row 938 ("Avenida Viveiro, 25, Foz") is the 1783 m one and carries
`precise` — 0 m of slack — today. Row 25 is the 336.6 m one, and it is #535's
second disclosed blind spot arriving as a number: "calle Tarancon, 6" answered
on "Av. de Salamanca, 6", 337 m from the Tarancón 6 the cadastre holds. The
two rows #559 named are both answered: 1537 ("Lugar Costenla, 31, Carballo")
stands on its own parcel, and 1734 ("Calle Xoiña, 8, Foz") is 69.0 m off.

**So a ROOFTOP does not earn its zero on this coast, and the sample still
cannot say what it does earn.** 46 rows are unplaced — a road code ("N-632")
or a lugar Google wrote as a street has no entry in the municipality's index
to measure against — and an unplaced row is not a correct one. Any band drawn
over the 84 that resolved would be drawn over the half that could be resolved,
which is not the half where a 2868 m error hides. `_tier_slack_table` is
therefore **unchanged by this work**: the measurement is the deliverable, the
band is a decision, and it is the owner's, exactly as #559 wrote it.

**Two blind spots of the measurement itself, both disclosed rather than
fixed.** The comparison is on the house number, so a parcel carrying the asked
number on a *different* street would read as agreement — the first run
reported row 25 that way. `number_matched_other_street` is the answer to that:
the cadastral street of a matched parcel must share a content word with the
street asked, which is the weakest test that separates a spelling from a
different street (13 of #535's 36 token mismatches were spelling, and every
one of those keeps a word). And the street index is matched exactly, on the
set of content words, so word order and articles are absorbed
("RETELA,LA" is "Lugar la Retela", "CASTRELOS" is "avenida de Castrelos") and
nothing else is. A near-miss is `street_not_matched`, never a guess.

## The rows a person placed after #535 (#558, 2026-09-08)

All seven rows #535 left behind now carry `enrichment["location"]`, written
one at a time through `utils/set_property_location.py --apply` behind
`data/issue_558_pre_20260908.json` on the mini.

| row | what was established | how |
|---|---|---|
| 25 | the parcel of the address the advert states, 337 m from the stored ROOFTOP | Catastro `Consulta_DNPLOC`, RC `0139702YH0103N` |
| 1445 | the lugar the advert names, **3.3 km** from the stored coordinate | `Consulta_DNPLOC` on `LG VILACENDOI - SAN MARTIÑO` |
| 438, 765 | the coordinate is inside the named hamlet, but the advert names no house number and its plot is not that parcel — so `precise` was never earned | `Consulta_RCCOOR_Distancia`; coordinate unchanged, only the claim |
| 1379, 1680, 1759 | the portal's own map centre | yaencontre `ld+json` |

**1445 is the one that moved, and it moved because the title was mangled.**
"calle Lugar Vilacendoisan Martiño" is the lugar Vilacendoi run together with
its parish, San Martiño; Catastro indexes it as `LG VILACENDOI - SAN MARTIÑO`,
and three of its parcels span 114 m around a point 3.3 km from where the row
sat — on `TR COLON 8`, in the middle of Foz. The advert still names no house
number, so what was recorded is the hamlet and the note says so.

**A portal map centre is not a pin, and the note says so on all three rows.**
yaencontre states "El anunciante no ha indicado la dirección exacta" and draws
a ~500 m circle with no marker, so what was recorded is the centre of a
privacy circle. It is still better provenance than the stored ROOFTOP — 1379's
stood on `LG FIOBRE 100`, a real cadastral address but an arbitrary house on a
lugar that runs at least 817 m — and a hand-set block moves the row from the
5 km locality slack to the 2 km listing-pin one. 1680 is the same house as
1379 (same subscription, price, area and rooms; two agencies, and its own
listing now answers 410 Gone), and its note carries that evidence rather than
implying a second reading.
