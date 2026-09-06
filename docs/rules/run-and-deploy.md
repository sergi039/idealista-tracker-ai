# Running, building and deploying

Moved verbatim from `CLAUDE.md` (lines 25–237 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**The stack runs in Docker on the Mac mini, and nowhere else** (owner,
2026-08-31). The mini is the server: `idealista-app`, `idealista-db` (5434),
`idealista-redis` and `osrm-spain` are containers there, and code reaches it by
push → the deploy watcher pulls, rebuilds and health-checks. A MacBook is a
*client*: it writes code, and it opens `http://127.0.0.1:5001/` through the
permanent ssh tunnel (`com.idealista.mini-tunnel`), which is the **mini's** app.
`docker ps` on the laptop being empty is the correct state, not a broken one.

So the commands below describe **the mini**. Do not bring a second stack up on
a laptop: the two databases diverged by 324 rows once already, and the merge
back cost an afternoon. There is no local PostgreSQL for this project — not the
deployment's and not a test server (see "Writing a migration?" in the hard
rules for the one throwaway, which is also a container on the mini).

```bash
docker compose up -d --build                 # the mini; app on 127.0.0.1:5001
docker compose -f docker-compose.dev.yml up  # dev variant with --reload
pytest tests/ -v                             # test suite (runs anywhere)
pytest tests/ --cov=app --cov-report=html    # coverage report
```

Building by hand on the mini is the deploy watcher's job, not yours — see
"Keeping the container current" below before you type the first line.

### Local CI gate

`tools/ci/local_ci.sh` runs the same checks as `.github/workflows/ci.yml`
(ruff check, ruff format --check, no-source-bundles, `uv run pytest tests/
-q`) locally, standalone or as a git `pre-push` hook — a red run costs agent
cycles even though the repo's Actions minutes are free (issue #74). Enable it
once per clone:

```bash
tools/ci/install_hooks.sh    # git config core.hooksPath .githooks
```

Bypass a single push with `SKIP_LOCAL_CI=1 git push`. `.github/workflows/
ci.yml` stays the merge gate for autopilot; since issue #81 it runs the same
ruff commands, so the two really are in sync.

**A shell stub written by a test needs its shebang at byte 0** (issue #284,
fixed 2026-08-14). `tests/test_merge_bot_dry_run.py` failed only under
full-suite runs, never in isolation, with a different test each time and a
message naming a binary that was never involved:
`merge_bot.sh: line 624: Segmentation fault: 11 git fetch ...`. There is no
git there — the harness's `git` is a bash stub and `bin_dir` leads `PATH`.
`_write_executable` dedented the stub bodies without `lstrip`, so line 1 was
blank and the `#!/bin/bash` under it was not a shebang; `execve` returned
ENOEXEC, bash re-executed each stub with *itself*, and Homebrew bash's locale
init (gettext → CoreFoundation) segfaults on the child side of a fork in a
multi-threaded parent. Apple's `/bin/bash` links neither library, which is why
the crash needed this Mac *and* a full-suite run. All three merge_bot test
files now assert the shebang, because three diverging copies of
`_write_executable` is how the one that mattered lost it.

Two lessons outlive that fix. **Three clean re-runs in isolation clear
nothing**: the file passed alone in half a second every time, so a session
that re-ran it three times and wrote "flake" had proved only that it was not
running the thing that crashed. And **the shell attributes a crash to the
command it was executing, not to the thing that died** — the crash reports
were named `bash`, 14 of them, one carrying the exact pid from a captured
failure. Reading the message got the symptom right and the cause wrong; the
crash reports got it right.

The hook's shared-`.git/config` canary (issue #74) compares config keys and
skips the four a parallel session writes (`branch.<name>.remote`, `.merge`,
`.rebase`, `.vscode-merge-base`): sessions share this clone, so a parallel
`git push -u` or `git worktree add -b` is not this gate leaking (issue #155).
Any other changed key is named and blocks the push. Only `core.bare`,
`core.worktree`, `core.repositoryformatversion`, `extensions.*` and
`include.*` are written back — only git's plumbing writes those, while
`user.email` or `core.hooksPath` may belong to another session.
`SKIP_LOCAL_CI=1` is not the answer to a config complaint any more.

Ruff itself is a locked dev dependency (`uv.lock`), so `uv run ruff` is one
pinned version for CI, the hook and you. Rule selection is explicit in
`pyproject.toml` (`[tool.ruff.lint] select`) because ruff's *default* set is
not stable across releases — do not delete it expecting the default to be
equivalent.

### Keeping the container current

The same installer enables `.githooks/post-merge`: **when main lands in a
clone, the running container is rebuilt from it.** The image is a `COPY . .`
snapshot and nothing re-takes it, so a merged fix does not reach the app until
someone rebuilds — on 2026-08-14 a container served a template that had been
fixed 15 seconds after the build, through the fix, its commit and its merge,
for 15 minutes.

**One deployer per machine.** Where `tools/autopilot/deploy_watcher.sh` is
installed — the Mac mini — the hook stands down completely and says so: the
watcher polls main every five minutes, tags a rollback image, rebuilds,
health-checks and writes `data/.deployed_sha`, and a rebuild started behind
its back would become the image its next tick treats as the last known good
one. The hook is the answer for a machine with no deployer, which is the
laptop: the watcher correctly refuses a checkout that is on a branch or dirty,
and a shared agent checkout is that nearly all the time. The hook never writes
`data/.deployed_sha` — that marker has exactly one writer.

Otherwise it acts only on `main`, only from the main worktree, only when the
app service is running, and never while a deploy holds the autopilot lock. It
does not guess the stack: `docker compose ps` and `docker compose port` name
the container and the published port, because `COMPOSE_CONTAINER_PREFIX` and
`APP_HOST_PORT` live in the project's `.env` (docs/DEV_RULES.md), not in the
shell — guessing them gates one stack and rebuilds another.

Before building it parses every template with jinja2 and every `.py` with
`ast`, and **refuses** rather than snapshot a tree that does not parse:
`COPY . .` takes the working tree, and in a shared checkout that includes a
parallel session's half-finished edit, which is exactly how the 2026-08-14
image was made. That is per-file syntax and nothing more — a missing include,
an unknown filter or a dropped jinja global all parse and still fail at
render. When no interpreter can import jinja2 the check cannot run at all, and
a check that did not run must never read as one that passed: a dirty tree is
refused outright, a clean one is built with *"not parsed locally"* carried
into the final line. Uncommitted files are always named before they ride in.

**A green `/api/healthz` is not acceptance.** It renders no template, and
`routes/main_routes.py` turns a `TemplateSyntaxError` into a redirect — which
is precisely why the 2026-08-14 container looked healthy for 15 minutes. So
the hook also requires a page that renders a template to answer **200**; a 302
is a failure. Which page that is is not the hook's to decide — it reads
`DEPLOY_RENDER_PATH` (default `/properties`) from
`tools/autopilot/lib/render_check.sh`, the one home of that rule, shared with
the deploy watcher (#292; see the hard rule below). Failing either check rolls
the *image* back to the tag taken before the build. It never rolls the tree
back the way the watcher does: this checkout is shared, and `git reset --hard`
would delete another session's uncommitted work.

A single pull skips it with `SKIP_AUTO_REBUILD=1 git pull`.
`tests/test_post_merge_hook.py` pins all of it, with `docker` and `curl` stubs
that assert their own arguments — an earlier version answered anything, and a
mutation run kept 12 tests green while pointing the hook at the wrong
container, the wrong compose file and a dead port.

### Building by hand in the shared checkout

**`docker compose up -d --build` snapshots the whole working tree, so run
`git status --porcelain` first and read it.** `Dockerfile` copies with
`COPY . .`, and several agent sessions share `/Users/ss/IdealistaRank`. A
build you start to look at your own change therefore bakes in every other
session's uncommitted files — including one that is half-written, because
nobody edits atomically.

That is not a hypothetical: it is what actually happened on 2026-08-14. A
session working on `templates/map.html` rebuilt at 11:59:24 to see its own
change on `/map`, 65 seconds into an 80-second window in which another session
had `templates/property_detail.html` mid-refactor with one stray `{% endif %}`
in it. The template was fixed 15 seconds after that build. The builder checked
`/map`, saw it fine, and moved on; every `/properties/<id>` was a 302 with an
error flash for the next 15 minutes, and the owner found it, not us.

So, before a hand build: check the tree is yours, parse what you are about to
bake (`.githooks/post-merge` does exactly this and can be read as the
reference), and **check a page that renders a template afterwards — not
`/api/healthz`, which renders none and stayed green through the whole
incident.** Check the page *your* change does not touch, too: the builder
above verified the only page that could not have caught the defect.

**A hand build also kills whatever is running inside the container**, and
unlike a deploy it leaves no trace at all: `docker compose up -d --build`
recreates `idealista-app`, so an hours-long backfill in there dies mid-row and
nothing logs it. `tools/backfill_status.sh` answers the question — run it, and
not `docker top idealista-app` alone, which names one container and therefore
misses both a respawn a supervisor is about to make and a job someone moved
into a `docker compose run` sibling (#338). The in-flight machinery the watcher
grew for this (#283)
lives inside `deploy_watcher.sh` and does not reach a build you start by hand,
so here the check is yours to make. A killed backfill is recoverable — the
tools commit per row and skip finished ones — but only if someone knows to
restart it.

If the tree holds someone else's work in progress and you only need to see
your own, the cheap way out is a `git worktree` with its own
`COMPOSE_CONTAINER_PREFIX` and `APP_HOST_PORT` (docs/DEV_RULES.md), not a
build of the shared tree.

**`git add -A` takes the same snapshot the build does, and commits it.**
Everything above is about `COPY . .`; this is the same hazard through git, and
it is easier to walk into because a commit feels like a smaller act than a
deploy. Measured 2026-08-17: a commit whose own content was two files carried
eight more — `migrations/020_add_search_profile_is_hidden.sql`, `models.py`,
`routes/main_routes.py`, `services/search_profile_service.py`, three templates
and `utils/i18n.py` — another session's half-finished feature, swept up by one
`git add -A` and pushed to a PR.

**What caught it was CI, and not for the reason you would hope.** No reviewer
read the diff and noticed foreign files; `tests/test_postgres_migrations.py`
compares the exact list of migration files and refused a `020` that is not on
`main`. A test about migrations found a commit-hygiene defect, which means the
same mistake in files that migration test does not see would have merged. So
the check has to be yours and it has to happen before the commit: **read `git
status` before `git add`, and add by path.** `git commit -a` is the same trap
with fewer characters.

The repair, if it has already happened, keeps the other session's work: `git
reset --soft HEAD~1` then `git reset` leaves every change in the working tree
exactly as it was, and you re-add your own files by name. Force-push is fine on
your own branch and is what removes the foreign files from the remote; check
afterwards that they are still in the tree, because the point is that nobody
loses anything.

Two more things this costs that are not obvious. **A `git merge` of `main` can
be impossible while another session holds uncommitted edits to the same
files** — git refuses rather than overwrite them, which is correct and leaves
you unable to update a branch that protected `main` requires to be current. Do
the merge in a `git worktree`, the same escape the paragraph above offers for
builds. And **a squash-merged branch conflicts with its own follow-up**: a
branch cut before the squash landed carries the same file as an unrelated
`add/add`, and the resolution is your version, since it is the squashed one
plus whatever you fixed since — verify that by diffing the two rather than
assuming it.
