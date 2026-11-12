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

### Browser checks on the MacBook

Owner decision, 2026-11-05: visible browser checks for this project belong on
`MacBook-Pro007.local`, including checks started by a Codex chat whose shell
runs on the mini. The desktop window's location does not determine where a
shell-launched Chrome runs. Do not launch Chrome, Chromium or a headed
Playwright process on the mini for this project.

Use the project launcher from either machine:

```bash
bash tools/browser.sh --check-route
bash tools/browser.sh open http://127.0.0.1:5001/properties
bash tools/browser.sh snapshot
bash tools/browser.sh screenshot
bash tools/browser.sh close
```

The launcher runs the MacBook's installed Playwright CLI over the existing
SSH route (`ss@100.127.248.71`) and verifies the remote hostname before
launching. `open` selects headed Google Chrome in its own test profile, with
a ten-minute idle timeout. The app remains on the mini; the MacBook's existing
loopback tunnel supplies port 5001. No new app stack, browser debug port or
network binding is needed. If SSH, Playwright or the tunnel is unavailable,
report that failure; never substitute a mini browser.

Sessions default to `idealista-$CODEX_THREAD_ID`, or `idealista-manual` outside
Codex. Set `IDEALISTA_BROWSER_SESSION=idealista-<task>` consistently for a
different task. Use only your own session and close it when finished;
`close-all` and `kill-all` are refused. Do not attach to or close the owner's
ordinary Chrome windows. Snapshots and screenshots are written on the MacBook
under `/Users/ss/Library/Caches/IdealistaRank/browser`; copy the specific
reported artifact back over SSH when it is needed in the mini-hosted chat.

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

## Nightly database dump (#640)

The database is in a named Docker volume on Docker Desktop's disk image, on an
external SSD that Time Machine excludes. `tools/backup_db.sh`, run by the
LaunchAgent `com.idealista.db-backup` at 03:30 local time, writes a verified
`pg_dump --format=custom` to `~/IdealistaRank-backups/daily/` -- the internal
disk, which Time Machine does cover.

- A dump counts only after it is non-empty and `pg_restore --list` reads it;
  then it is renamed to `idealista-YYYYMMDDTHHMMSSZ.dump` (mode 600). A failed
  dump is deleted and rotates nothing. The newest `IDEALISTA_BACKUP_KEEP`
  (14) are kept; `pre-merge-*.dump` and other files are never touched.
- Status for a health check: `last_success` and `last_failure` beside the dumps
  (UTC timestamp, then file name and size or the reason). A run refused by the
  lock (exit 75) writes neither. A stopped `idealista-db` fails; it is not started.
- Log: `data/db-backup.launchd.log`.

Install on the mini (paths in the plist are the production checkout):

```bash
cp tools/com.idealista.db-backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.idealista.db-backup.plist
launchctl kickstart gui/$UID/com.idealista.db-backup   # optional first run now
launchctl bootout gui/$UID/com.idealista.db-backup     # to remove
```

Verify a dump (lists it, never restores): `bash tools/backup_db.sh --verify FILE`.
Restore is the manual command in `MIGRATION_RUNBOOK.md` /
`tools/backup_attachments.sh`: `docker exec -i idealista-db pg_restore -U idealista -d idealista_universal --clean < FILE`.

## Where the deployment's state lives (#639)

The app container persists one directory, mounted at `/app/data`: the mail
cursor and its retry list, the Google spend ledger, attachments, score
snapshots, the in-flight markers. The deploy watcher keeps its own files next
to them (`.deployed_sha`, the deferral count, the stall alarm, its logs, the
review journal), because it has to read what a job writes inside the container.

On the host that directory has **one home: `IDEALISTA_DATA_DIR` in the
checkout's `.env`**. `docker-compose.yml` mounts `${IDEALISTA_DATA_DIR:-./data}`
and every host tool asks `tools/autopilot/lib/data_dir.sh <repo>` -- the deploy
watcher, `merge_bot.sh`, `autopilot.sh`, `run_issue.sh`,
`tools/backfill_status.sh`, `tools/backfill_supervisor.sh` and
`tools/backup_attachments.sh`. Unset, both answer `<repo>/data`, which is what
all eight spelled out by hand before; so the variable existing changes nothing
until a machine's `.env` sets it. **Setting it is the move, and the move is the
owner's command**: `tools/move_data_dir.sh`, steps and rollback in
`DATA_DIR_RUNBOOK.md`. Whether a machine has moved is what its `.env` says;
`tools/move_data_dir.sh --check <dir>` reports it without touching anything.

- The name is not `DATA_DIR` on purpose. `.env` is also the app service's
  `env_file` and `config.py` reads `DATA_DIR`; a host path under that name
  would become the directory the app writes *inside* the container. That side
  stays `/app/data`, and every documented `--snapshot data/x.json` stays true.
- The reader follows Compose's precedence (process environment, then `.env`)
  and refuses what Compose would resolve silently: the variable set in both
  places to different values (an empty export counts), the key twice, or any
  spelling other than `IDEALISTA_DATA_DIR=/absolute/path`. An explicit value
  must be an existing directory; nothing creates it and nothing falls back to
  `<repo>/data`. A watcher tick that cannot resolve it dies on stderr
  (`data/autopilot-deploy.launchd.log`) before touching docker.
- Run it, do not source it: `"$(/bin/bash tools/autopilot/lib/data_dir.sh "$repo")"`.
  It lives in `tools/autopilot/lib/`, so the watcher's handover parse gate
  (#293) vets it before a fast-forward like the other contracts there.
- A new host script that needs the directory asks the reader;
  `tests/test_data_dir_single_home.py` fails on a ninth `${REPO_DIR}/data`.
- What stays in the checkout's `data/` whatever the variable says: the four
  committed reference files (`docs/rules/reference-data.md`),
  `data/.orchestrator` (an agreement between sessions, not state) and the
  launchd stdout logs, whose paths are fixed in the installed plists.

**Moving it is one rename under a recovery trap**
(`tools/move_data_dir.sh --check | --apply <dir> | --rollback`). `--apply`
takes the deploy lock and the merge lock, stops the app, takes a checksummed
safety copy to `~/IdealistaRank-backups/data-pre-move-<UTC>/`, renames
`<repo>/data` to the destination on the same volume (so there is never a
second authoritative copy), rebuilds a small `<repo>/data`, writes the `.env`
line, starts the app with `--no-build` and verifies mount, healthz, the page
(`lib/render_check.sh`, 200 or it failed) and a host-to-container probe. From
the moment the app is stopped every non-success exit, signals included, runs
the same idempotent `recover` that `--rollback` runs; phases are intents
assigned before the command they cover, and recovery reads the disk, not the
phase. Its own `docker compose` calls drop `IDEALISTA_DATA_DIR` from the
environment so only `.env` decides the mount.

- **The tombstone.** After the move `<repo>/data` holds the four reference
  files, `.orchestrator`, launchd logs and `data/.moved-to`. A directory that
  carries the tombstone is where the state *used* to be: `lib/data_dir.sh`
  refuses to resolve to it and the app refuses to start on it
  (`app._state_directory_problem`), so a lost `.env` line is a loud stop
  rather than an app with no mail cursor.
- **A build that does not know about the move is not deployed onto it.** The
  tombstone readers are part of what a revert of #639 removes, so they cannot
  be what stops that revert. The watcher that is *serving* can: while the
  resolved directory is not the checkout's own, it refuses, before the
  fast-forward, a target commit without `tools/autopilot/lib/data_dir.sh` or
  whose `docker-compose.yml` does not mount `${IDEALISTA_DATA_DIR`; the refusal
  counts toward the stall alarm (#532). `--rollback` first, then the revert
  deploys normally. A hand `docker compose up --build` of such a tree is not
  gated.
- **`create_host_path: false`.** The mount is in long form for that flag: the
  short form creates a missing source as an empty directory and starts the app
  on it. Measured on Compose v5.7: with the flag, a missing source is "bind
  source path does not exist" and nothing is created.
- **The reference files ride in the image.** `.dockerignore` used to exclude
  `data/` whole, so the four committed files reached the container only
  through the bind mount; once `/app/data` is another directory they would be
  gone. The Dockerfile copies them to `/opt/idealista/reference_data` and sets
  `REFERENCE_DATA_DIR`; readers ask `utils/reference_data.py`
  (`docs/rules/reference-data.md`).

**A test run cannot write that directory** (`tests/data_dir_guard.py`). #627
moved the two paths that had already done damage -- the cursor, after a test
wrote UID 99 into the production one on 2026-11-09, and the ledger. A list is
as long as its incidents: on 2026-11-12 a full run still left `data/.inflight/`
behind, the directory the watcher reads to decide what a deploy would kill. So
the session now moves `Config.DATA_DIR` and `ATTACHMENTS_DIR` to a temporary
directory, and an audit hook refuses any open-for-writing, create, rename,
delete, truncate, chmod or utime whose real path is inside `<checkout>/data`
(or inside `IDEALISTA_DATA_DIR` / the original `DATA_DIR` when the environment
names one). A refusal is recorded as well as raised, so a caller that swallows
it still fails the session by name. Reads are allowed -- the reference files
live there. Not covered, and said in the module: a subprocess, and a relative
name given with a `dir_fd`.
