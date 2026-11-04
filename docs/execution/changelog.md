# Execution changelog

## 2026-11-04

- Replace the crowded catalog header with labelled collapsible navigation,
  one subscription selector, explicit filter Apply and four readable row
  groups. Keep recommendation status compact and secondary evidence expandable.
- Establish shared Bootstrap tokens, Jinja components and a read-only design
  catalog so future pages follow the same interface contract.
- Preserve the complete taste-retraining confirmation text in its HTML
  attribute; the confirmation now executes before the existing paid POST.
- Repair Milanuncios photo renditions for existing and new listings; capture
  Idealista photos from the matching listing anchor in alert emails.
- Verify Fotocasa and Milanuncios against their listing payloads instead of
  translated page chrome. Recognise Fotocasa's explicit missing-property redirect.
- Add a paced, refusal-aware availability audit with a separate snapshot-backed
  apply and baseline checks under the existing row lock; add an idempotent
  Idealista email-photo backfill without changing the ingestion cursor.

## 2026-11-03

- Put Recommendations first in the result controls, alongside its status and
  refresh action even in the ordinary list. Move detailed coverage into an
  expandable section so the way to recommendations is no longer buried in
  diagnostics; filters, ranking and stored evidence retain their behavior.

- Protect browser API writes with the existing session-bound CSRF token,
  including fetch and HTMX callers; read-only endpoints keep their behavior.
- Require an explicit walking route before beach travel can support pedestrian
  access. Driving measurements remain available without becoming walking facts;
  unchanged validated photo evidence survives this semantic correction.
- Preserve canonical property kinds in recommendation descriptors, including
  unknown types, and invalidate only changed reference evidence.
- Preserve land classification through the Similar SQL projection so parcel
  size still contributes when imported area types say built or unknown.
- Apply the same recommendation exclusions and ordering to the page and its CSV
  export, including recommendation mode with an explicitly chosen sort.

- Published `https://prop.cervantes50.com` through a dedicated Cloudflare
  Tunnel and whole-host Access application restricted to the owner's
  confirmed email (issue #579). The tunnel validates the Access JWT audience,
  rejects other hostnames with 404, and retains the app origin on
  `127.0.0.1:5001`. Access uses email PIN, HttpOnly/binding cookies and
  SameSite=Lax; application writes separately require the session-bound
  CSRF token introduced in #581.
- At publication, verified the local listing page and health endpoint returned
  200, the tunnel was healthy, and anonymous list/API/detail/attachment
  requests plus forged JWT/email headers redirected to Access login (six
  HTTP 302 responses). The single-email policy and proxied DNS target passed
  readback. Owner PIN login, authenticated browser actions through the public
  address and an interactive non-owner login have not been exercised.
- Kept configuration, audit helper and proof private under
  `~/.config/idealista-prop-tunnel/`. Independent review of the anonymized
  infrastructure plan/helper returned `rx: provider=openai outcome=PASS`.
  Rollback: stop only `com.cloudflare.cloudflared.idealista-prop` and remove
  the DNS record identified in that directory's `deployment-state.json`;
  the local app and its deployment watcher remain unchanged.

## 2026-09-08

- Made recommendation rows compact: group repeated evidence labels per outcome,
  show at most two matching topics and visible conflict/check counts, and keep
  the full topic list in a keyboard-accessible disclosure. This prevents owner
  comments from stretching every listing to screen height. Named the mode
  button Recommendations and shortened its score-column heading for discovery
  and more title space; ranking and stored evidence are unchanged.

- Added source-typed Taste descriptors, explicit preference clauses and a pure
  recommendation context on `/properties` so plot/sea facts, scope, conflicts,
  coverage and stale profiles are represented without a model call per row.
- Added bounded, hash-verified visual descriptor extraction through the existing
  subscription bridge so house texture can be compared on an evidenced photo
  sample without giving the bridge arbitrary URL or host-file access.
- Added an unlabeled held-out evaluation manifest so future owner usefulness is
  measured against the old Taste/Similar baseline without decision leakage.
- Added a CSRF-protected Recommendations refresh action that rebuilds the
  owner-evidence profile once and reranks prepared listings in code, with
  distinct dirty, queued/running and failed status messages.
- Corrected three independent-review counterexamples: explicit negated desires
  retain negative polarity, a newly rejected favorite stops anchoring stale
  recommendation comparisons immediately, and every feedback/training entity
  is reserved before held-out candidate selection to prevent identity leakage.
- Bound each compiled preference to the value stated in its own clause, reset
  heading polarity at sentence boundaries, and made match evidence
  value-specific so mixed claims cannot create false credit or hard exclusion;
  clauses without a comparable canonical value remain visibly unapplied.
- Made empty recommendation searches SQL-safe on SQLite and PostgreSQL,
  held-out manifests fail closed when excluded rows are absent, and bounded
  visual work continue after one row or temporary-file failure so retries do
  not repeat completed extraction.
- Tightened clause semantics after independent review: local negatives now
  override list headings, ambiguous object negation and compound same-aspect
  values stay visibly unapplied, and only explicit equivalent/opposite values
  affect ranking or hard exclusion. Bumped the compiler snapshot version so
  existing profiles show dirty and use the one-call recommendation refresh.
- Replaced unsupported conditional keywords in the Codex visual output schema
  with its flat structured-output subset and added the exact image identity
  manifest to the extraction prompt. Strict aspect, unknown, parcel-outline
  and provenance validation still runs before persistence.
