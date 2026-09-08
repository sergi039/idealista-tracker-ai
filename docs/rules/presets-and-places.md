# Travel presets, places, routing, comparables, run state

Moved verbatim from `CLAUDE.md` (lines 2347–2538 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- **Google Places Nearby Search reaches 50 km, whatever `radius=` asks for.**
  Measured 2026-08-11 at 43.551663,-6.831426 (property 360, La Caridad):
  `radius=50000`, `radius=100000` and `radius=200000` returned the *identical*
  seven places, same seven `place_id`s, farthest 45.21 km. Google clamps to its
  documented 50,000 m maximum silently — no error, no warning, no field saying
  it did — so a call asking for more reads as reaching further than it can, and
  a reviewer cannot tell from the code that it does not. Reach past 50 km comes
  from Places **Text Search**, which takes no `radius` at all (`location` only
  biases its ranking). That is how the legacy `Land` airport lookup finds an
  airport an hour away (`_airport_candidates` in `services/enrichment_service.py`);
  PR #254 does the same for the `/properties` airport preset, where the
  measurement was first taken.
- **What may be recorded as an airport is defined once**, in
  `services/place_rules.py` (the `PlaceRules` matcher) over the patterns on the
  preset in `services/search_profile_service.py` (issue #171). Google's
  `type=airport` covers helipads, aerodromes and aeroclubs; at the coordinate
  above, all seven results were exactly that. `/properties` refused them from
  #171 onward while the legacy `Land` path, holding no copy of the rules, went
  on taking the nearest — which is how 145 of 168 lands came to store a
  "nearest airport" at a median 0.27x the distance of the real one, rendering
  on `/lands/<id>` directly above the correct road distance from
  `Land.distance_airport`. Do not copy the patterns into a third caller; import
  them. `utils/clear_legacy_land_airport.py` removes the values the unfiltered
  search left behind (free — no API call — with a rollback snapshot).
- **The same rules say what may be recorded as a hospital, and a *centro de
  salud* may not** (owner decision 2026-08-15, narrowing the 2026-08-10 one that
  accepted "a hospital, a health centre or a public outpatient clinic"). Primary
  care has no beds and no emergency department, so recording it overstates
  medical access on a number the scorer reads: measured on the Salamir listing
  (43.568817,-6.211955), the app said "hospital 11 min" — the Centro de Salud in
  Muros de Nalón — against ~27 min to Hospital Universitario San Agustín, the
  assigned hospital. 187 of 396 travel rows held such a place. The patterns went
  on the preset, not into a second filter. Two of them are not obvious and were
  measured: Google indexes a hospital campus **room by room, every room tagged
  `hospital`**, so 13 departments of San Agustín sorted ahead of the hospital
  itself at rank 18 of 20 — `hospital de día` (a day unit) and `unidad de
  hospitalización` (one ward) carry the word and must be refused for the parent
  to win. The old rows are not fixed by the deploy —
  `utils/recalc_property_travel.py --ids …` rewrites them and, while
  `OSRM_URL` is unset, **spends money** (with the local routing engine on,
  #416, the routing is free) — either way it rewrites data the app cannot
  roll back, so it needs the owner to ask.
- **The hospital preset is answered from the national register, not from
  Places** (owner decision 2026-08-18, after the invoice read **EUR 190** for
  1-18 August on a project ingesting ~7 listings a day). The whole of that
  bill is enrichment, and it is attributable day by day: 320 travel runs on
  the 16th, 197 on the 15th, 123 on the 10th, against invoice spikes on
  exactly those days. Two beliefs died with the screenshot and neither should
  be rebuilt on: Google's per-SKU free tiers did **not** absorb this volume,
  and the "$0.36 a listing" this file has carried was arithmetic over the
  price list rather than a reading from billing -- read the bill.
  `data/hospitals_cnh.json` is already here, already imported, already read by
  the quality-of-life card: 42 hospitals across the five watched provinces,
  every one with a coordinate, beds and teaching status. So
  `services/reference_places.py` answers the preset from it, the read is free,
  and no request leaves the machine. Measured against 12 random production
  rows: the register names the same hospital as Google for 8, and where it
  differs it is better -- two rows had a Google place literally named
  *"Hospital"* (the register names Covadonga and Jove), and at Ferrol Google
  had a private clinic where the register names the public complex 1.0 km
  away. The rules below are **kept and dormant**: they describe how to survive
  Google's `hospital` type, and one deleted `reference_source` puts that
  search back. A register that cannot answer -- file missing, or a listing
  outside its five provinces -- produces a **refusal and never a fallback to
  the paid search**, because falling through would spend exactly where the
  register is thinnest, which is the opposite of the point. This removes one
  of the seven Places calls per listing; the drive time to the hospital is a
  Distance Matrix element only while `OSRM_URL` is unset — #416 made the
  routing free, so with the engine on the hospital costs nothing end to end
  (`tests/test_hospital_from_the_register.py`).
- **The other five presets are answered from OpenStreetMap** (step 2 of the
  same plan, 2026-08-18). Five of the seven Places calls a listing costs are
  `airport`, `train_station`, `supermarket`, `school` and `police`;
  `services/osm_places.py` resolves them from Overpass, declared on each preset
  as `osm_tag` / `osm_radius_m`. It is not a cost compromise, and the six
  production coordinates it was measured against say why: Google answered
  `police` with **"Traffic radar"** (property 101) and with a private security
  firm (property 67), and `supermarket` with "La luz de mundo" (property 123),
  where `amenity=police` gives the Comisaría and the Cuartel and
  `shop=supermarket` gives Alimerka 0.9 km away. A tag is a claim about what a
  thing *is*.
  **The #171 airport rules work on OSM names verbatim**, which is the finding
  that made this cheap: `aeroway=aerodrome` carries exactly the aeroclubs and
  light-aircraft fields Google's `airport` type does -- at Oviedo the nearest
  is *Aeródromo de La Morgal*, 9.2 km -- and the shipped
  `require_name_patterns` refuse every one while accepting *Aeropuerto de
  Asturias* and *Aeroporto da Coruña*. On all six coordinates that is the
  airport Google named. It also retires the reason `wide_search_query` exists:
  Overpass has no 50 km cap, so Cariño resolves A Coruña at 64.3 km in the same
  query, with no second paid call.
  Three things are load bearing. **One query answers every preset** -- the
  first one to run fetches all declared types and caches the candidates, so
  five presets cost one round trip at the shared 5 s gate rather than five.
  **Candidates are cached, not the nearest**, because the rules walk past what
  they refuse and caching only La Morgal would leave the preset nothing to fall
  back to. And **a refusal never falls through to the paid search**, including
  `wide_search_query`: falling through would spend exactly when the free source
  is down. The transport is `EnrichmentService._overpass_elements`, reached the
  way `services/pool_service.py` already reaches it -- do not grow a second
  Overpass client.
  Two consequences for the suite. `tests/conftest.py` stubs
  `services.osm_places.lookup_candidates` **per test** to "Overpass replied and
  there is nothing here", for the same reason it forces `AUTO_GEOCODING` off:
  six suites written against the Places path mock Google and nothing else, and
  the moment a preset started asking Overpass they reached the live internet
  and `tests/network_guard.py` failed the run. It is reset per test rather than
  once, because a suite that points it at a refusal would otherwise leave it
  there -- that mistake turned three failures into six. And the suites that pin
  the Google machinery now build their preset through a local `_google_path()`
  helper that strips `osm_tag`: what they know cost several tickets, and one
  deleted line puts that path back. `tests/test_osm_places.py` pins the module
  *and the wiring*, because a green unit suite over a dead hook is the defect
  this repository keeps rediscovering (#309).
- **Drive times come from a routing engine on this machine when `OSRM_URL` is
  set** (step 3 of the same plan, #416; set on the mini since 2026-08-20, so
  production routes for free). `services/osrm_routing.py` owns it, and
  `_distance_matrix_batch` in `services/property_travel_service.py` asks it
  first — with `OSRM_URL` set no Distance Matrix request is made at all, so a
  travel run, a pool measurement or a recalc bills nothing for routing; unset
  means Google answers exactly as before (~26 elements a listing). Every cost
  sentence in this file that names Distance Matrix is about the *unset*
  configuration. Three things the module's own docstring records and this
  file should not water down: it is **opt-in** because OSRM's car profile
  runs +26–34% slower than Google on 30–75 km motorway legs, so turning it on
  decides what the stored minutes *mean*, not only what they cost; a routing
  engine that cannot be reached is a **refusal, never a silent fall back to
  the paid API** (the `osm_places` decision again); and the extract carries
  `car.lua` alone, so any other mode is refused rather than answered with a
  driving time.
- **A town crowds the real hospital off the page, so the preset carries
  `wide_search_query` too** (#325). Nearby Search returns **one page of 20**,
  and #323 shipped without the fallback on the strength of one *rural*
  coordinate where the hospital was still on that page. It does not
  generalise: the recalc it authorised left **48 of 187 rows** with no
  hospital, and at 43.3622522,-5.8485461 (Oviedo) all 20 results sit inside
  0.7 km and are private practices — a beauty centre, a driving-licence
  renewal office, several named individuals. HUCA and Monte Naranco are close
  and can never appear. So the refusals were right and the answer was never on
  the page, which is the airport preset's situation exactly (#171/#254), and
  it takes the same cure: Text Search accepts no `radius`, so Nearby's ~50 km
  cap does not apply, and the same preset rules filter the result. Measured
  against the deployed image: Oviedo → "Monte Naranco Hospital" 2.1 km,
  Cudillero → "Hospital Universitario San Agustin" 26.2 km. It fires only
  where Nearby already answered with nothing acceptable, so it bills nothing
  for the rows that resolve. **`wide_search_query` is not part of
  `PlaceRules`**, so adding it leaves the Places cache signature unchanged and
  the already-correct rows keep their cached lookups — which is what let the
  48 be re-run on their own.
- **Amenities are measured for `Property`, through the same one client**
  (#152). `_fetch_osm_amenities` is the whole Overpass amenity client — cache,
  gate, transport, refusals — and `_enrich_with_osm_data` (legacy `Land`) and
  `enrich_osm_amenities` (universal `Property`) are thin writers over it. The
  property one runs inside `PropertyEnrichmentService.enrich_property`, lands
  in `enrichment["infrastructure_extended"]`, and a refusal never fails that
  run: no score reads these counts. Before this the lookup was reachable only
  from the `Land` endpoints, so 213 of 352 listings had no Extended
  Infrastructure card at all — an absence that reads as "nothing nearby". Do
  not add a second amenity client, and do not let a refusal become empty
  counts.
- **What counts as a comparable listing is decided in one place**
  (`services/property_comparables.py`, #386). Price per m² collapses as a plot
  grows — measured 2026-08-17 over 459 production plots, Spearman −0.842, with
  band medians running €120.5/m² under 800 m² down to €4.4/m² above 6,000 —
  so a peer set that ignores size answers a different question and the answer
  reads like this one's. #378 measured that and #383 fixed the *scorer*; the AI
  prompt built its own pool and was still unbanded, which is how property 351
  (1,300 m², €46/m²) came to be judged `OVERPRICED` against a "local peer
  average" of €26/m² carried by two four-thousand-square-metre parcels, on the
  same page where its own Value component put it below the median at 52.6/100.
  Its listed comparables were worse: `ORDER BY score_total DESC`, and
  `size_score` is a component of that score, so the three shown were always the
  largest and therefore the cheapest per m². Import the ladder, do not copy it,
  and pick comparables by size proximity. `band=False` exists for the size
  component alone, where a window around the listing's own area would be
  circular. A prompt whose pool spans mixed sizes **says so** — a bare average
  is read as "what the neighbours ask", which is #98's defect with a number in
  place of a blank. Two consumers already drifted; the third will too.
- **An enrichment run reports how complete it was, not just pass or fail**
  (#153, owner decision 2026-08-09). `EnrichmentService.enrich_land` reduces
  its three sources to `ok` / `degraded` / `unavailable`, stamps that on the
  record as `infrastructure_extended["enrichment_status"]` and returns
  `state != unavailable` — the same shape, and the same boolean facade, as
  `Property.travel["api_status"]` in `services/property_travel_service.py`.
  Google is *decisive*: `_score_infrastructure_extended` reads only the
  `<amenity>_available` keys Places writes, so Google refusing means the run
  did not produce what it was asked for. Overpass is *advisory*: it cannot
  move a score, and it answers 504 whenever both of its two per-IP slots are
  busy, so failing the whole run on it would report failure for lands whose
  Google data arrived intact. That asymmetry is why `degraded` exists — do not
  collapse it into `ok` (a missed source reported as success is #98 again) and
  do not promote it to `unavailable` (option 2 in #153, which the owner
  rejected). `tests/test_issue_153_enrichment_run_state.py` fails on either.
