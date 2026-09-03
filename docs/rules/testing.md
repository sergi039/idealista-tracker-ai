# Testing: mocks, CI, evidence, mutation, skips, migrations

Moved verbatim from `CLAUDE.md` (lines 2836–2857, 2868–2878, 2882–3050 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- Mock external API calls in tests. Suites needing live services or
  credentials are reported as skipped, never as passed. **The suite now
  enforces this itself** (issue #307): `tests/network_guard.py`, installed for
  the whole session from `tests/conftest.py`, refuses every connect that leaves
  this machine and names the destination and the line in this repository that
  asked for it. It also *records* each refusal and fails the run on it, because
  raising is not enough on its own — every caller here catches `Exception` and
  degrades (`utils/geocoding.py` falls back to Nominatim and then swallows the
  failure; an enrichment run reports `degraded`, #153), so an unmocked call used
  to leave a green test and no trace anywhere in the output. That is how PR
  #306's sea-view step came to reach live Overpass from three suites, and how,
  in the pre-push gate's sandbox, those connects sat in `SYN_SENT` and stalled
  the gate for tens of minutes.

  Loopback and every non-IP address family stay open, so the CI PostgreSQL
  service and the loopback HTTP servers in the AI-bridge suites are untouched.
  The guard sees Python's socket module and nothing else: measured 2026-08-14,
  `requests`, `urllib`, `http.client` and `imaplib` are all refused by name,
  while psycopg2 dials through libpq in C and a subprocess is a separate
  interpreter — neither is covered, and the guard claims neither.
  `PYTEST_ALLOW_NETWORK=1` switches it off for a deliberate live-API
  investigation; it is not a way to make a red run green.

- CI exists (`.github/workflows/ci.yml`; issue #31 closed 2026-08-07):
  it runs on every PR and on push to `main`, with actions pinned by SHA.
  Three jobs — `pytest` does `uv sync --frozen` + `uv run pytest tests/ -v`
  on Python 3.11; `no-source-bundles` fails when an archive or source dump
  is tracked (issue #29); `ruff` runs `ruff check .` and `ruff format
  --check .` on the same uv setup (issue #81). **All three are *required*
  status checks on `main`** (strict: the branch must be up to date), so a
  red `ruff` blocks the merge exactly as a red `pytest` does — run
  `uv run ruff check .` and `uv run ruff format --check .` before you push,
  or the PR costs a cycle. `ruff` was the optional one until the owner added
  the context; this file said so until issue #264.

- **A pass count says the suite ran. It does not say the fix works.** On
  2026-08-14 four defects in one day survived a green suite because the test
  meant to catch them could not fail: a stub that counted calls instead of
  recording what they saw (#297, found by the mutation in #300); a fixture
  whose text avoided the one input the guard under test keys on, so the test
  "stepped around the defect instead of at it" (#306); a call site pinned by
  three context-free substrings, so inlining the call back to the broken
  position passed all five of its tests (#309); and a `skipif` on the module
  that tests a mechanism, so removing that mechanism's installation gave
  `29 skipped`, exit 0 (#308, fixed in #310). The same
  currency bought the earlier ones: three clean re-runs of the merge-bot test
  that never touched the crashing path (#284), a green `/api/healthz` through 15
  minutes of every `/properties/<id>` redirecting (#283).
- **A change that adds or modifies a test reports the mutation result, not the
  pass count.** Undo the fix — or invert the assertion — and paste which tests
  go red. A fix whose tests stay green when it is removed is unproven, whatever
  the tail says, and saying so costs one re-run. Where a mutation is expected to
  stay green because another line already covers it, say that too rather than
  presenting the green as evidence.

  **CI now asks the same question and does not rely on you asking it**
  (MUT-001, `tools/ci/mutation_check.py`). On every pull request it removes the
  diff's production hunks in a worktree of its own and re-runs only the tests
  the diff touched: `CAUGHT` when at least one of them goes red, `ESCAPED` and
  red when none do, `NOOP` for a docs- or tests-only diff, `WARN` when
  production changed and no test did, and `TOOLING-ERROR` — exit 2, neither
  pass nor fail — when it could not run. Four to nine seconds on the real PRs
  of 2026-08-19, against the suite's six minutes. An `ESCAPED` that is right —
  a refactor, a revert, a test written for behaviour that already existed —
  is answered with a `Mutation-Waiver: <reason>` trailer on any commit in the
  branch. That friction is the point, the way `tests/skip_guard.py`'s `ALLOWED`
  is — but it is weaker than `ALLOWED`, and the difference is worth knowing: a
  skip exemption is a line in a reviewed file that stays there, while a trailer
  is free text in a commit message, visible in the PR and nowhere afterwards.
  Nothing checks that the reason is a good one.

  **It answers "can these tests fail", never "is what they assert correct."**
  Reverting cannot redden a test for a bug the revert removes, and that is two
  cases wearing one face: the diff *introduced* the defect, so the code without
  it never had one — or the defect already lived in shared code and the diff
  only *brought a new consumer to it*, in which case the revert removes the
  consumer and leaves the bug. The second is the one worth carrying: **a new
  call to an existing shared function is a change to that function**, and has
  to be read as one. Measured on #427 the day the check shipped — an
  independent review of that diff found three real wrong answers (a guard that
  was a no-op whenever the geocoder named no province, a fallback the check was
  blind to, and an alias table that had always been wrong in one direction and
  had simply never been asked), and neither that PR's own six mutations nor
  this check could have seen any of them. Its author: *"I mutated what I wrote,
  not what I missed."* Review is what catches those.

  **The two find different classes, and neither is the safe one.** Mutation
  finds defects of the *tests* — measured 2026-08-19, it caught a test that
  reached its feature by a road the mutated flag never touched, a test that
  passed when the tool under test was deleted outright, and a missing case;
  review finds defects of the *code*, and found eleven the same day that no
  mutation on either side could have seen. Neither substitutes.

  Two things about the review half are worth the words, because both cost
  something when skipped. It is not "read the diff" — it is **asking the code
  the specific question you are afraid of the answer to** ("can this emit a
  false `contradicted`?", "how can this checker itself lie?"). A lens without
  a question returns nothing: of the four pointed at #427, one came back
  empty. And review **invents findings at about the rate it finds them** —
  #426's raised 19 and 8 survived reproduction, #427's raised 7 and 5 did — so
  a finding is worth acting on after an attempt to refute it, not before. Both
  of those numbers are a third to a half wrong, and acting on the wrong half
  means fixing something that is not there.

  **The refuter must not be the finding's author**, and that is the part doing
  the work — not the count. An author defends their own wording, and the claims
  that died on both sides were the confidently written ones: coherent, specific,
  and false only once somebody tried to reproduce them. "Three refuters" is a
  number, and a number buys nothing when the refuter is the same agent. Give
  them the opposite instruction as well — default to refuted, reproduce before
  believing.

  **Keep doing it by hand anyway**, because the check cannot see the case that
  cost the most today. A test can execute the mutated line without asserting on
  its effect — measured 2026-08-19, a mutation flipped `include_hidden=True` to
  `False` while the test reached the feature through a different argument
  entirely, and 59 tests passed. That is mutation testing's equivalent-mutant
  problem and nothing solves it; what the tool removes is the *other* two
  failure modes, both of which are about the mutation not happening at all: a
  text substitution that stopped matching after `ruff format` rewrapped the
  line, and a captured patch that came out empty because zsh does not
  word-split. Both read as `26 passed` and `59 passed`. **Revert real hunks
  with git, in a worktree, never a string in a file** — and never
  `git checkout -- <path>` over an uncommitted fix, which on the same day
  deleted one and was caught only by reading `git show --stat` afterwards and
  noticing a file missing from the commit.
- **UI and timing behaviour is proven by measurement on a built image**, never
  by a unit test or a template's static text. The bar #302 arrived at and #309
  was measured against: repeated *loads* (the race resolves once at init, so
  repeated samples
  inside one load agree with each other and prove nothing), at least two
  widths, `elementFromPoint` per control rather than bounding-box overlap, and
  a second sample seconds later because the popup keeps moving. Identical bytes
  behaving differently on two machines is an environment finding, not a code
  one.
- **A skipped test reports success**, which is why `tests/skip_guard.py` pins
  which module may skip and for what reason, and fails the session on anything
  else (#314). A genuinely conditional new test costs one line in `ALLOWED` —
  that friction is the point, because the alternative is a number nothing
  reads: not `.github/workflows/ci.yml` (exit status only), not
  `tools/ci/local_ci.sh`, and not a reviewer, who would have to diff a tail
  against the previous run to notice it move. Both hooks are wired on purpose —
  a module skipped at *import* (`allow_module_level`, `importorskip`) never
  reaches `pytest_runtest_logreport` and would take a whole file out of the
  session unseen. What the guard cannot do is tell a deliberate escape hatch
  from a mechanism that failed to install, because the reason text is
  identical; `tests/test_network_guard_is_installed.py` is what answers that,
  and the two are meant to be read together.
- **Writing a migration?** Everything in `migrations/` is PostgreSQL-only and
  multi-statement, so SQLite cannot execute it and `db.create_all()` proves
  nothing about it. `tests/test_postgres_migrations.py` runs the real files
  against a real server; it skips unless `TEST_DATABASE_URL_POSTGRES` points
  at a **throwaway** database, and the CI `pytest` job sets it plus
  `REQUIRE_POSTGRES_TESTS=1` so a missing server fails instead of skipping. A
  percent sign in migration SQL must be doubled — psycopg2 eats a lone one and
  the statement dies at deploy time.

  **The throwaway server is `tools/ci/migration_test_db.sh`, and 5432 is not
  it** (owner request 2026-08-31). Those tests CREATE and DROP databases on
  whatever that variable names, as whatever role it carries, so the server has
  to be one nobody else is using. Two are not: `127.0.0.1:5434` is the mini's
  `idealista-db`, and **`127.0.0.1:5432` on this Mac is Postgres.app, which is
  the inbox-zero project's database server** — the owner's global rules
  reserve it for that project and forbid this one from connecting to it at
  all.

  It was used anyway, which is the part worth keeping. A session needed a real
  PostgreSQL for migration 025, ran `open -a Postgres` and then `createdb -U
  ss throwaway_nan_test` on 5432, and the cluster holding `inboxzero` spent
  two minutes serving this project's DDL. Nothing was lost — only the
  `throwaway_*` databases were created and dropped, `inboxzero` was never
  opened, and the postgres log shows the whole of it. **The cause was not
  carelessness: the prohibition named no server that was reachable.**
  CONTRIBUTING.md's disposable container was correct and Docker Desktop was
  not running on the laptop, so the one real PostgreSQL within reach was the
  one that must not be touched. A rule that forbids the only available path is
  a rule that gets walked around, and this one was.

  So the permitted path ships with the prohibition, **and it keeps no database
  on the laptop at all** — this project runs in Docker on the Mac mini and the
  laptop is a client over Tailscale (owner, 2026-08-31; the first version of
  this fix raised a local cluster and was itself out of that scheme).
  `tools/ci/migration_test_db.sh start` runs one `docker run --rm` on the mini
  of the same `postgres:15-alpine` the deployment's `idealista-db` runs — its
  own name, the mini's loopback, no volume, no compose labels, so
  `docker_cleanup.sh` ignores it and a deploy does not disturb it — and
  tunnels it to 127.0.0.1:55432 for the run. `stop` closes the tunnel and
  removes the container. Two things that follow: the migrations are exercised
  on **production's own major version** rather than on whatever PostgreSQL the
  laptop happens to have, and offline there is no server at all — `start`
  fails and says so, because the honest fallback is CI and not a database
  here.

  And the rule is mechanical rather than remembered:
  `tests/postgres_server_guard.py` enumerates `pg_database` on the connection
  the `postgres_url` fixture already opens, **before its first CREATE
  DATABASE**, and fails — never skips, a skip reads as success — when the
  cluster holds a database that is neither PostgreSQL's own nor this run's.
  `inboxzero` and `ss` trip it; `migtest` alone in the throwaway cluster and
  `idealista_ci` alone in CI do not, which is why the rule cannot be a port
  number: CI is on 5432 too. What it cannot see is an *empty* foreign cluster
  — there is nothing in it to recognise and equally nothing in it to lose —
  and that limit is written into the module so its absence is not read as
  coverage.
