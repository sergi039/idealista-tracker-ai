# Owner searches and technical subscriptions

`/profiles` opens My searches: independent owner searches, with archived searches
under a disclosure. Technical subscriptions shows every underlying row, its exact
saved-search identity where known, stored-object count and destination. A routed
stub has zero direct objects; its destination link opens the shared collection.
Counts link to all stored objects (`hide_removed=off&criteria=all`).

New ingestion-created profiles are technical and hidden until the owner assigns
them or promotes them. Unbound ones are disclosed as Needs attention on My searches
and through the catalog's existing hidden-data disclosure. There is no inferred
city-to-region matcher. Existing saved-search identities and label resolution
remain authoritative; an ambiguous alert is never assigned by guessing.

Operations have a session-bound 30-minute preview. The confirmation rechecks
profile and object snapshots under locks; stale previews require another review.
Merges are atomic, use `SearchProfileService.route_profile`, and retain the target
configuration. Nonempty differing source configuration requires acknowledgment.
Routes remain one hop. Catch-all, already-routed sources, pattern carriers and
sources with inbound routes cannot be merged. The last two can instead be removed
without transfer; their objects and inbound ingestion remain accessible technically.

Delete from My searches is a retained technical tombstone, not physical deletion
or a portal unsubscribe. No properties, profiles, favorites, review history or
attachments are deleted. Restore uses Make an independent search. Routed stubs
cannot be restored or shown independently. Ordinary settings saves cannot revive
technical/deleted/routed rows.

`ui_config.search_management.state` stores owner/technical/deleted without changing
other UI configuration. Existing profiles without the namespace remain owner
searches. The existing hidden flag still owns selection across list/map/export.

Each merge appends `enrichment.profile_transfers` under property row locks. Import
receipts and exposure history remain byte-equivalent JSON values. Feedback and
frozen URL reservations project through this explicit trail; a plain reassignment
or changed listing URL still invalidates a receipt. Old taste ledgers remain;
changed scope/basis requires the existing explicit refresh, never a paid call
from a management page.

## Operations and recovery

`python -m utils.manage_searches --source 32 --target 24` is read-only. Its JSON
fingerprint can be supplied to `--apply --expected <fingerprint> --snapshot <new
private path>`. A receipt is written exclusively before the guarded transaction;
its presence proves an attempt, not completion. Read the resulting profile routes
and exact property IDs to establish completion before retrying. Conflicts are
acknowledged through the web preview, not bypassed by the CLI.

Take the database/attachment backup first, run `tools/backfill_status.sh`, and
rehearse on a disposable clone. Do not use raw SQL to write `routed_to`. A failed
transaction rolls back completely. Deploy rollback uses the previous image via
the project watcher; these additive JSON values require no schema rollback and
remain readable by the old application. A committed merge has no automatic
unmerge: never restore a whole database over newer ingestion. A later split needs
an explicitly scoped, reviewed corrective operation using the retained row IDs,
receipts and source mapping.
