# Execution changelog

## 2026-09-08

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
