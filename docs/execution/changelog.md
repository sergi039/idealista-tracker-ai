# Execution changelog

## 2026-11-05

- Reserve rated primary/alias listing URLs across deletion and reimport with a
  new row ID, scoped to the original subscription. Retain URL exposure through
  feedback rollback and disclose legacy markers with missing history (#601).
  Stop stale hard clauses from hiding candidates after owner-rule changes,
  while preserving current binary No exclusions and list/CSV parity (#602).

- Correct numeric taste calibration after the live eight-row probe: unknown
  compatibility starts neutral, exact owner appeal remains observed evidence,
  and missing facts only lower confidence. Omit regional hazard scans from
  numeric candidate and exemplar inputs to prevent speculative privacy/aesthetic
  penalties; retain the stored scans and freshness provenance.

- Include completed appeal answers and their stored evidence in both numeric
  taste prompts, deduplicated by frozen card and scoped to subscription. Keep
  reasonless votes from inventing preferences and trust complaints local;
  invalidate prior numeric rubric scores and require a real model rebuild when
  these inputs change. Record accurate job arguments for safe production runs.

- Route project browser checks to the MacBook's isolated Chrome session over
  SSH; refuse a Mac mini fallback so remote Codex chats do not open test
  windows on the server desktop.

- Preserve one validated live alias after a pilot primary disappears, retain
  exposure history through feedback rollback, and disclose a stale model basis
  independently from current prepared recommendations. Block numeric scoring
  against changed model evidence; keep favorite references independent.
- Connect completed owner appeal reviews to recommendation references and exact
  object exclusions, with atomic idempotent import, private rollback receipts
  and scoped compare-and-swap restore. Preserve favorites and purchase state.
- Read regional hazard uncertainty honestly in taste prompts and invalidate old
  rubric scores; never infer residential neighbours from industrial scan counts.
  Reserve retained feedback from later blind evaluation; do not infer aversions
  from reasonless binary answers.
- Apply the Werkzeug 3.1.9 and pyasn1 0.6.5 security fixes surfaced by
  Dependabot after the initial pending dependency updates were integrated.

- Integrate the pending anyio, urllib3, oauthlib, certifi and dnspython updates
  together against the current application. Retain the canonical project
  instruction entrypoint when reconciling the older Codex-entrypoint branch.

- Compact catalog rows and the heading: give titles more width, align numeric
  and action columns to the top, and keep closed details below the photo.
  Expanded evidence and missing-photo rows retain their full readable content.

## 2026-11-04

- Keep research-sheet parcel areas in `plot_area`, never in house area; add a
  dry-run repair with original-import identity checks, rollback snapshots and
  locked restore. Ambiguous house/outbuilding totals remain unmeasured.
- Read stored renovation and privacy claims, including explicit attached-house
  typology, for recommendation conflicts; preserve their provenance and avoid
  hard exclusions from claims. Count repeated preferences once per aspect.
- Reflect subscription area/budget shortfalls in recommendation rank even for
  reviewed rows; support an explicit EUR budget with identical Python/SQL
  criteria. A likely sea view remains potential evidence.

- Replace the crowded catalog header with labelled collapsible navigation,
  one subscription selector, explicit filter Apply and four readable row
  groups. Keep recommendation status compact and secondary evidence expandable.
- Establish shared Bootstrap tokens, Jinja components and a read-only design
  catalog so future pages follow the same interface contract.
- Preserve the complete taste-retraining confirmation text in its HTML
  attribute; the confirmation now executes before the existing paid POST.
- Accept Fotocasa's legacy listing-photo path `/images/anuncio/` alongside
  `/images/ads/`; seven live listings published photos that the old path guard
  discarded. Agency logos under `/images/client/` remain excluded.

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

## 2026-11-06

- Added My searches and the complete Technical subscriptions inventory, explicit
  promotion, atomic previewed merges and deletion with retained data. Ingestion
  no longer invents owner searches for every new label. Moved feedback remains
  bound through an explicit transfer trail while original receipts stay intact.
