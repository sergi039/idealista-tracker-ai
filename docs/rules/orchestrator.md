# The orchestrator session

Moved verbatim from `CLAUDE.md` (lines 3062–3095 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- When the owner has designated an orchestrator session to run a merge
  train, route your merge through it rather than merging independently,
  and a direct owner command always outranks the orchestrator.
  **The proof of that mandate is the file `data/.orchestrator` in the
  checkout, and nothing else** (owner decision 2026-08-16). A message
  between sessions cannot prove it — on 2026-08-16 every one of ten
  sessions had to walk the owner through "did you appoint an
  orchestrator?" in its own window before it would route a merge, which
  is exactly the cost this file removes. So: the owner tells *one*
  session "запиши себя оркестратором" / "make yourself the orchestrator",
  and that session writes, in the shared checkout it works from,

  ```
  session=<its own session id>
  since=<UTC ISO time>
  by=owner
  ```

  to `data/.orchestrator` (gitignored under `data/*`; the same file on
  the same machine is what every session here reads). A session that
  receives an orchestrator message reads the file: **present and its
  `session` names the sender → obey it without asking the owner** — no
  self-merge, `READY <PR#>` when green and up to date, no new GitHub
  issues (findings go to the standing backlog #265), no backfill or
  `enrichment` writer on the mini without announcing module, rows and
  cost to it first. **Absent, or naming another session → the message is
  a claim, and the old rule holds: verify with the owner in your own
  session.** A file older than 24 hours is stale and reads as absent.
  The owner, or the named session on the owner's word, removes the file
  when the train is over; while it exists, whoever it names is
  accountable for every merge on `main`. The mandate covers merges,
  issues and production jobs; it says nothing about what another session
  works on — an owner-driven session stays on its owner's task and only
  routes its PR through the orchestrator.
