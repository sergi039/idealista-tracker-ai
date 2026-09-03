# Portals: fotocasa, milanuncios, yaencontre; pins and hand-set locations

Moved verbatim from `CLAUDE.md` (lines 1150–1355 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**Listings arrive from fotocasa by link, and the reader is 60 lines because
the page hands over JSON** (#389). Fotocasa is not Idealista, measured
2026-08-17 from this machine: it answers **200** to the bare product token in
`utils/http.HTTP_USER_AGENT` and 403 to `python-requests`, `curl` and a bare
`Mozilla/5.0`, with no DataDome, no captcha and no JS challenge in either body.
The filter is on the client name, so identifying ourselves honestly is
*sufficient* — nothing here spoofs a browser, and if that stops being true the
answer is to stop fetching, not to dress up as one. `robots.txt` allows the
listing page for `*` and disallows `/buscar/`, which is why nothing accepts a
search URL and there is no sweep. The data is in
`<script type="application/json" id="__initial_props__">`, so `parse_listing`
is one `json.loads` — no LLM, no HTML parser, and `trafilatura` (declared in
`pyproject.toml`, imported nowhere) stays unimported.

Three things in that payload are traps and all three are pinned by
`tests/test_fotocasa_source.py` against the real 40 KB block in `tests/data/`.
**The two address blocks disagree about `municipality`**: `realEstate.address`
says `Avilés` (`cityId: 33004`, its INE code) and
`realEstateAdDetailEntityV2.address` says `Llaranes`, the district — read the
wrong one and `utils/municipality_grouping.py` groups four surfaces on a
municipality no INE join can resolve. **`0` is fotocasa's blank**, not a
measurement: the measured plot carries `rooms: 0, bathrooms: 0, heating: 0`.
And **the portal declares its own coordinate inexact** (`coordinates.accuracy:
0`, `address.isExact: false`), so a fotocasa row is stored `approximate` and
never `precise` — `precise` grants zero slack in `services/coordinate_quality.py`
and unlocks a travel run (billed via Distance Matrix only while `OSRM_URL` is
unset; free from the local routing engine otherwise, #416). This paragraph
said "no page claiming exactness has ever been seen" until 2026-09-01, and
that is stale: 8 production rows (825–1549) carry `portal_accuracy` with
`is_exact: true`. The rule survives its own counter-example for a better
reason — the one declared-exact pin a person actually checked (property 421)
records *"EXACT per portal, but the pin is a meadow 170 m S of the house"* —
so the portal's exactness claim is itself unreliable, and a fotocasa row
stays `approximate` whatever the flag says. Both portal flags ride verbatim
into `enrichment["import"]` so that measurement, when somebody takes it,
needs no re-fetch.

**And so does the pin itself**, because a re-geocode used to throw it away
(#393). `refresh=True` clears the coordinate *before* geocoding, and
`_build_geocoding_queries` reads the text after "in" in the title -- which for
a plot is a district, not a street. Measured on property 733: the refresh
answered with the Llaranes district centroid, 2447 m from fotocasa's pin, still
`approximate`, so nothing was unlocked and the listing-specific point was gone;
the advert text places the plot in Valliniello, so the query named the wrong
neighbourhood as well. `services/coordinate_quality.py` owns the rule now --
`portal_coordinate` reads the pin off `enrichment["import"]["coordinate"]` and
`improves_on` says only `precise` is worth a swap, because every consumer reads
`approximate` and `approximate` identically. A refresh that answers nothing
puts the pin back too: clearing first meant a refusal left the row with *no*
coordinate, which is worse than the one it started with. Only a portal pin is
defended; a coordinate this geocoder wrote last month has no better claim than
the one it writes today. The 56 rows the out-of-band script imported carry no
such block and are therefore unprotected -- their coordinates *are* portal
pins by that script's own docstring, but the row does not say so, and writing
an inference into a provenance field is the STATUS-002 mistake in a new
column.

**And a coordinate a *person* established outranks the geocoder, in its own
key** (GEO-002). Only a portal pin was defended, so a `precise` somebody
curated returned to `approximate` on the next refresh and the components that
label unlocks went with it. The curation is not hypothetical and neither was
the exposure: measured 2026-08-20, three production rows carried a
hand-established location in **three different ad-hoc shapes** -- 161 and 792
under `enrichment["coordinate_provenance"]` with `method` values that do not
match and timestamps under two different names, 774 under
`enrichment["cadastre"]` -- and **nothing in the repository read any of them**.
161 and 792 both carry a `precise` their own `enrichment["geocoding"]` record
contradicts, the fingerprint of a write made outside the geocoder; 161 survives
today only by accident, because it also happens to carry a portal pin, and 130
of the 132 `precise` rows carry none. **Re-measure rather than quoting those
two**, the way the `location_accuracy` paragraph above says: the morning's
answer was 129 of 130, because the set grows with every ingest.

**And within the same afternoon the pressure this creates produced the wrong
write.** By 15:02Z rows 161 and 792 both carried
`enrichment["import"]["coordinate"]` -- `source: cadastre_manual` and
`cadastre_parcel` -- put there by hand-run scripts. That field means *the
coordinate the source portal published*, and a cadastral parcel centroid is not
that. It works, because `_apply_geocode_outcome` defends the portal pin, which
is precisely why it is the STATUS-002 mistake and not merely untidy: the row now
answers "the portal placed this pin" to anyone who asks, and no reader can tell
those two rows from the 57 fotocasa ones. Nobody was being careless -- there was
nowhere honest to put it, which is the hole this section closes. Moving them is
one `utils/set_property_location.py --source cadastre` per row, by the person
who established them, and is deliberately not done here.

The reason those blocks were ad-hoc is that there was no hand-set path for a
coordinate at all -- the only writers of `location_accuracy` are the geocoder,
the fotocasa import, the `Land` migration and the restore half of
`utils/refresh_property_accuracy.py`, so everything else went through
`docker exec`, the boundary `services/ingest_policy.py` records as the one a
flag cannot close. So the defence ships with its writer:
`enrichment["location"]`, read by `manual_coordinate` and written by
`record_manual_coordinate` in `services/coordinate_quality.py`, set by
`utils/set_property_location.py`. `ensure_coordinates` refuses in front of it
**before the geocode**, making no request at all, the shape `advertiser.enrich`
uses for a hand-set seller verdict; `improves_on` is not consulted, because a
person outranks a better label.

Five things about it are deliberate. It is **not** written where
`portal_coordinate` looks -- a conclusion drawn from the cadastre stored under
"the pin the portal published" is the STATUS-002 mistake above, in a new
column, and that is why the three rows are **not backfilled**: no column
distinguishes a curated `precise` from a Google one, so a person converts them
with the note their own block already holds, or nobody does. A **malformed
block does not stop a geocode**, since the alternative is a row pinned to a
coordinate nothing can correct and nothing can explain, and a **note is
required** for the same reason -- `owner`/`agency` describe themselves, two
numbers do not. **Clearing leaves the coordinate columns alone**: the block is
not guaranteed to be newer than them, so restoring what it displaced could undo
a later deliberate act rather than the one being cleared. And
`utils/refresh_property_accuracy.py` **counts and names what it skipped** --
it is the one caller that runs `refresh=True` over a scope, and folding a
hand-set row into the rows that came back unchanged would report
`precise -> precise` for a row Google was never asked about, which is #98's
defect inside a report. That last one is worth more than its size: it is a
defect of *existing* code that only review could find, because a new call to a
shared function is a change to that function and neither side's mutation can
see it. `tests/test_hand_set_location_survives_a_refresh.py` pins all of it.

**The import reads, shows, and only then writes, because this app cannot delete
a property.** There is no delete route and no `db.session.delete` on `Property`
anywhere in the tree, so a row built from a misread page stays in the table, in
the `/municipalities` medians and in its subscription's comparable pool. The
preview is the only undo there is; do not collapse the two steps. They are also
split by time — ninety links at the 3 s courtesy gate is four and a half
minutes against one gunicorn worker with four threads and the default 30 s
timeout — so reading is a background job and confirming, which makes no network
call, runs in the request. A deploy that kills the container mid-fetch (#283)
therefore costs nothing.

A fotocasa row is created with `listing_status_source` **NULL**, written as
`null()` and not `None`: the column carries a Python-side default of `"ingest"`,
which SQLAlchemy applies to any attribute that is None at flush, so the obvious
assignment reads like the intent and stores the opposite.

**`RefusalBreaker` is per host** (`HostBreakers`). It was process-wide, which
was right while every listing was on idealista.com and became wrong the moment
a second site arrived: idealista refuses this machine permanently, so its
breaker is open essentially always, and a shared one did not degrade a fotocasa
check — it forbade it for thirty minutes at a time without a request going out.
`tests/conftest.py` resets **every** host between tests. In the same family,
`_looks_like_listing_page` knew only `/inmueble/<id>/` and fell through to "any
200 is the listing" for everything else, so a fotocasa URL redirected to a
search page would have been recorded as live — #136's false confirmation at a
second host. All 56 stored fotocasa URLs end in `/<id>/d`, which is what the
second anchor matches.

**Portal alerts arrive by email too -- fotocasa, milanuncios and yaencontre --
and the email contributes only what the portal cannot** (2026-08-30, two PRs
the same day). The Gmail query is
`(from:noresponder@idealista.com label:...) OR from:fotocasa.es OR
from:milanuncios.com OR from:yaencontre.com`: **the label gates the idealista
term alone**, because the owner's Gmail filter labels idealista mail only --
measured that day, all 65 fotocasa, 26 milanuncios and 29 yaencontre mails in
the mailbox carried no label, so the first version of this feature, which
demanded the label for portal senders too, would never have fetched one. The
UID cursor is what keeps historical portal mail out (only UIDs past it are
fetched); a `run_full_sync` deliberately re-reads everything, portal promos
included. Per-portal senders live in `FOTOCASA_ALERT_SENDERS` /
`MILANUNCIOS_ALERT_SENDERS` / `YAENCONTRE_ALERT_SENDERS` (defaults are the
bare domains; the real senders were read off the real mail:
enviosfotocasa@fotocasa.es, no-responder@milanuncios.com,
no-reply@envios.yaencontre.com); empty turns that portal off.

The three portals differ in what the email can say, and each got the model
its measurements dictate. **Fotocasa**: direct `/<id>/d` links, page answers
the honest UA -- email names the listing, page supplies every field
(validated against the first real alert, committed token-redacted as
`tests/data/fotocasa_alert_arteixo.html`). **Milanuncios**: the digest
carries *no direct links at all* -- every anchor is an opaque SparkPost
tracker -- so `services/milanuncios_source.py` resolves only the *card*
trackers (anchors wrapping an `images*.milanuncios.com` photo; the same
template wraps Eliminar/Desactívala/Dar de baja in identical trackers, and
resolving those would knock on alert-management doors), redirects OFF,
`Location` only, then fetches the ad page, which answers 200 with
`window.__INITIAL_PROPS__` -- price, surface, **coordinates**, and
`sellerType` (`private` -> the advertiser verdict for free). Two payload
traps are measured and pinned: `sellType: demand` is somebody *searching*
and is refused, and `location.city.name` is the locality with the
municipality in parentheses ("Los Quintanales (Mieres)" -> Mieres).
**Yaencontre**: the portal answers DataDome to every request from both
machines (403 even for robots.txt), so `services/yaencontre_source.py` reads
the email card itself -- title, price, hab/baños/m², municipality from the
title's last comma -- and the row gets no coordinate (the geocoder fills it
at ingest) and **no advertiser block** (`record_advertiser=False`: an
absent key reads "not established", which is true). The listing identity is
the *second* number in `inmueble-<a>-<b>` -- the first repeats across one
seller's adverts.

One builder for every portal row: `fotocasa_import.build_property` (the
module keeps its historical name; the writers are portal-generic) writes the
`<source>:<id>` dedup key -- ids are only unique within a portal, so the
LIKE patterns in `PORTAL_URL_PATTERNS` are anchored per source -- the NULL
`listing_status_source`, and the portal pin. Dedup runs *before* any fetch.
All fetches run in `run_ingestion`, never inside the open IMAP connection,
each portal paced by its own gate and counted by its own refusal counter
(`PORTAL_MAX_CONSECUTIVE_REFUSALS`, the `backfill_advertiser` pattern): a
refusal **holds the UID cursor** so the email is re-read when the block
lifts; only an *answered* "gone" (`not_the_listing_page`, a 404, a demand
ad) is consumed, because tomorrow's answer is the same. Profile resolution
is the existing chain (matchers, then the catch-all). The fixtures under
`tests/data/` -- both alert bodies, the milanuncios payload -- are the real
2026-08-30 artifacts, token-redacted; `tests/test_portal_alert_ingestion.py`
and `tests/test_fotocasa_email_ingestion.py` pin all of it.
