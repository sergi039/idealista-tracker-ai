# Execution changelog

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
