# Neighbouring buildings (#570)

The measurement uses Catastro INSPIRE Buildings footprints and the full parcel
outline in the parcel's ETRS89 UTM zone. It does not measure around an advert's
privacy-circle centre. Only an explicit Enrich press or a shortlist named with
`utils.backfill_neighbours --ids` reaches this step; ingestion's free-source
pass never does. The step is advisory and writes its own `enrichment.neighbours`
block through `enrichment_write.check_writable` / `locked_write` after network
work. It never changes a score or calls a paid API.

A saved cadastral reference establishes the parcel. Otherwise a precise
coordinate must resolve to one zero-distance parcel; an exact numbered address
can resolve through `cadastre_locate`'s existing province/street/address clients.
Multiple parcel answers are refused; no parcel is guessed from a nearby result,
prose note, or approximate coordinate. `no_parcel` and `unavailable` contain no
counts. A refusal preserves a previous success only for an unchanged subject.
The subject includes the origin, accuracy, parcel references, asked address and
municipality; both the locked writer and descriptor reader check it.

Distances are polygon-to-polygon, including disconnected parcel pieces and
holes. The BBOX contains the complete 100m buffer, never a clipped radius, and
refuses boxes over Catastro's 4km² limit. Buildings linked to the subject parcel
are excluded. With no parcel link, a footprint wholly covered by the parcel is
also excluded; a different parcel's touching building is a zero-distance
neighbour. Features are counted once per building ID. Agricultural and
industrial use counts are separate; missing/unrecognised use is counted as
unknown and cannot support agricultural absence.

The block records `status`, `source`, `subject`, `reference`, `epsg`,
`searched_m`, `measured_at`, `buildings_within_50m`, `buildings_within_100m`,
`nearest_building_m`, and agricultural/industrial/unknown-use counts within 100 m.
Nearest distance is null when none was found within the searched 100 m; it is
not an assertion about the nearest building beyond the search. Complete empty
collections are a measured zero. Exceptions, malformed geometry/CRS, duplicate
IDs, paging, inconsistent WFS counts and the 5000-feature cap are refusals.
Catastro's native GML collection omits counts; it is accepted only as a complete,
well-formed collection below that cap. This cannot independently detect data
missing from the provider's underlying map.

## Calibration blocks categorical privacy

On 2026-11-04, four bounded read-only queries captured two parcel outlines and
two complete building collections in EPSG:25829. No production data was written.
The tests retain the public geometry, parcel links and use fields; unrelated
building metadata was removed. An independent parser confirmed these results:

| owner reference | parcel | within 50 m | within 100 m | nearest footprint |
|---|---|---:|---:|---:|
|969|4463719NH1946S|3|7|17.865m|
|1282|15044A01901483|3|11|3.776m|

Both owners' reference readings say few neighbours. Consequently the proposed
`within 50 m -> low`, `none within 100 m -> high` privacy rule fails calibration.
`neighbor_privacy` stays **unknown** with this reason; no replacement threshold
is invented. Raw `nearby_buildings` is a supported numeric count, incomparable
to the existing categorical photo/owner vocabulary. Agricultural presence or
absence may be supported by use-typed measurements, with absence requiring
complete use coverage. The reranker's executable clauses are unchanged.
Photo `neighbor_privacy` / `nearby_buildings` remain claims, whatever status the
photo extractor supplied. This PR does not complete categorical acceptance of
issue #570; an owner decision and calibrated predicate are still required.

Adding a measured block changes descriptor input fingerprints; saved Taste
references/visual inputs may become stale and need their normal explicit
refresh. Rows without measurements keep their previous input fingerprint.

## Bounded transport and shortlist CLI

The existing `cadastre_service._get` transport retains `max_attempts=1` and the
same `CATASTRO_GATE` shared with `cadastre_locate`. A ContextVar requests 12 s
reservations for a neighbour walk without mutating another thread's policy.
This paces neighbour starts within one process; **it is not an IP-wide quota
across gunicorn workers or separate CLI processes**. The explicit Enrich route
is capped at 5 presses/minute by the existing Flask limiter. An uncached walk
needs 3 requests for an established reference, 4 via a precise coordinate, or up
to 6 via an exact address. Existing parcel outline caches may reduce that total.
A nested 90 s free-lookup budget can refuse a walk before completion and never
extends the surrounding Enrich budget. There is no retry or scheduled sweep.

The CLI requires 1–20 explicit positive IDs and has no `--all` or default cohort.
It defaults to dry-run (no API calls or data writes):

```sh
python -m utils.backfill_neighbours --ids 969,1282 --dry-run --plan /private/path/neighbours-plan.json
```

The plan saves
the exact IDs, subject hashes and previous blocks. `--apply --plan` requires
that unchanged plan and refuses missing IDs or a changed baseline. It never
overwrites an existing plan. After a partial run, generate a new dry-run plan;
its in-flight marker therefore makes no resume claim. Before any real apply,
follow `long-jobs-and-deploys.md`: announce module, rows and zero monetary cost,
run `tools/backfill_status.sh`, and stop on busy/unknown. No backfill was run as
part of implementation. Keep the plan private: it is a rollback snapshot.

Catastro's [official Buildings WFS description](https://www.catastro.hacienda.gob.es/webinspire/documentos/inspire-bu-WFS.pdf)
defines the request syntax, supported CRS, 4 km² / 5000-feature limits and coverage.
The national service excludes the Basque Country and Navarre; its documented
coverage is 95% of the territory under the national cadastre, so this feature
cannot promise every real building is mapped.
