# Runbook: moving the deployment's state out of the checkout (#639)

On the Mac mini the production checkout is also the place sessions run `git`
and `pytest`, and its `data/` is what the app container mounts at `/app/data`.
This runbook moves that directory to another one and back with
`tools/move_data_dir.sh`. Two placeholders stand for the machine's paths:

- `<checkout>`: the absolute path of the production checkout on the mini (the
  directory that holds `docker-compose.yml` and `.env`);
- `<destination>`: the absolute path the owner chooses for the state. It must
  not exist yet, must be on the same mounted filesystem as `<checkout>/data`
  and outside the repository; `--check` refuses anything else.

**The move is the owner's command.** Nothing in the repository performs it on
a merge, a deploy or a schedule.

Background and the rule: `docs/rules/run-and-deploy.md`, "Where the
deployment's state lives". Every message quoted below is the script's own
text; the exit codes are the ones `tools/move_data_dir_test.sh` holds it to.

## What moves and what stays

| | Where it is after the move |
|---|---|
| mail cursor and retry list, `google_spend.jsonl`, `attachments/`, score and coordinate snapshots, `.inflight/`, `.deployed_sha`, deferral and stall markers, autopilot logs, `autopilot-reviews.tsv`, supervisor locks | `<destination>` |
| the four committed reference files | still tracked in `<checkout>/data`; the container reads the copy baked into its image (`REFERENCE_DATA_DIR`, `docs/rules/reference-data.md`) |
| `data/.orchestrator` | `<checkout>/data` (copied back; sessions read it from the checkout) |
| launchd stdout logs (`*.launchd.log`, `ai-bridge.log`) | `<checkout>/data`: their paths are fixed in the installed LaunchAgents, which the move does not touch; they are logs, not state |
| `data/.moved-to` | `<checkout>/data`: the tombstone. While it exists, `tools/autopilot/lib/data_dir.sh` refuses to answer `<checkout>/data` and the app refuses to start on it |

Why a plain directory and not a Docker volume: a named volume lives in Docker
Desktop's disk image, outside Time Machine; the watcher, `backfill_status.sh`,
the supervisor and `backup_attachments.sh` must read the very files the
container writes; and a snapshot has to be restorable by a person from the
host. Choose `<destination>` on the internal volume, like
`~/IdealistaRank-backups`, so that it is inside Time Machine (`--check` does
not verify Time Machine).

## Before

1. No backfill, no merge train in flight, not around 03:30 Madrid (the nightly
   dump).
2. A shell on the mini, in the main checkout, with none of the variables the
   script refuses exported:

```bash
cd <checkout>
```

```bash
unset IDEALISTA_DATA_DIR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROJECT_NAME
```

## Check (read-only, repeat as often as you like)

```bash
bash tools/move_data_dir.sh --check <destination>
```

`--check` is the default mode and takes no lock. It prints one `ok:` or
problem line per condition and ends with `READY:` (exit 0) or `NOT MOVED: N
problem(s) above. Nothing was touched.` (exit 1). A wrong argument count is
exit 2. It refuses when:

- `IDEALISTA_DATA_DIR`, `COMPOSE_ENV_FILES`, `COMPOSE_DISABLE_ENV_FILE` or
  `COMPOSE_PROJECT_NAME` is exported in the shell; `.env`,
  `tools/autopilot/lib/data_dir.sh` or `tools/autopilot/lib/render_check.sh`
  is missing; `docker`, `git`,
  `python3`, `curl` or `shasum` is not on `PATH`;
- whether `data/` carries the tombstone cannot be established (the directory,
  or the one above it, cannot be searched by this user);
- `tools/backfill_status.sh` answers busy (1) or unknown (2);
- the destination is relative, carries a space, quote, `#`, `$` or backslash,
  already exists, sits inside the repository, its parent is not a writable
  directory, or it is not on the same device and mount point as `data/` (the
  move must be one `rename(2)`);
- `~/IdealistaRank-backups` (`MOVE_BACKUP_ROOT`) is not on the same mounted
  filesystem as `data/`, or any two of `data/`, the destination and the backup
  root contain one another;
- with `IDEALISTA_DATA_DIR` set to the destination, Compose would mount
  anything else at `/app/data`, or cannot say;
- the checkout is not on `main` or is not clean; `.env` already sets the
  variable; `data/.moved-to` exists; `data/` is not a directory, contains a
  symbolic link, or could not be searched in full for one;
- `data/.deployed_sha` is missing, not a regular file, or not `HEAD` (a deploy
  is pending: let the watcher finish);
- the app container is not running, does not mount `data/`, or its image
  does not carry the reference files;
- any other container, running or not, mounts `data/`, something inside it or
  something that holds it - or Docker could not say what a container mounts.

`READY:` does not promise that the rename succeeds: asking would write. The
move finds out, and undoes a failed one.

## Move

```bash
bash tools/move_data_dir.sh --apply <destination>
```

It first takes the deploy lock and the merge lock, and holds both until it
exits; a held lock ends it with `STOP: ... Nothing was touched.` (exit 1).
**Then** it runs the same preflight under the locks, so the checks describe
the world the move acts on; a refusal is `NOT MOVED: N problem(s)` (exit 1),
nothing touched. Then, in order, each step verified before the next:

0. **recording**: writes `IDEALISTA_DATA_DIR=<destination>` to
   `.env` (temporary file, fsync, atomic rename; the file is never printed).
   This is the first write, before the stop: until the rename the line names a
   directory that does not exist, which the reader and Compose
   (`create_host_path: false`) refuse - loud, as a move in progress should be.
   A watcher tick meanwhile meets the deploy lock and skips.
1. **stopping**: `docker compose stop app` and confirms the container is
   stopped - downtime starts; only then records a sha256 manifest of `data/`;
2. **copying**: `cp -Rp data ~/IdealistaRank-backups/data-pre-move-<UTC>/` and
   compares manifests (a difference confined to `*.log` is reported and
   tolerated - host processes this script does not stop append to logs;
   anything else aborts);
3. **renaming**: asks once more that the destination is free, then one rename
   `data` -> `<destination>`, so there is never a second
   authoritative copy, and compares the manifest again;
4. **switching**: recreates `data/` with the tombstone written and fsynced
   first, `git checkout -- data` (the four reference files), `.orchestrator`
   copied back; then asks `tools/autopilot/lib/data_dir.sh` that `.env` now resolves to the
   destination;
5. **starting**: `docker compose up -d --no-build app` - nothing is built;
   Compose recreates the container because its mount changed;
6. **verifying** (`verify_serving` in `tools/move_data_dir.sh`): the mount
   source; `/api/healthz`; the page that
   `tools/autopilot/lib/render_check.sh` names (`DEPLOY_RENDER_PATH`) answers
   **200**. Any other code is a failure (`answered ... not 200`), and so is an
   empty `DEPLOY_RENDER_PATH`: `verify_serving` then returns 1 with `the page
   check is switched off (DEPLOY_RENDER_PATH is empty); a move cannot be
   called verified without it`. Then a token written on the host is read back
   through the container, and the reference files are checked in the image. A
   failed check exits the script, which runs the recovery below.

Its own `docker compose` calls drop `IDEALISTA_DATA_DIR` from the environment,
so only `.env` decides the mount. It ends with `MOVED:` (exit 0) and prints the
`.env` line, the safety copy and the undo command. The script announces about
a minute of downtime.

**If anything fails after the preflight** - a command, a check, Ctrl-C, a
closed terminal (`HUP`, `INT`, `TERM`) - the script prints `FAILED in phase
'<phase>'` and runs the same idempotent recovery `--rollback` uses; it reads
the disk, not the phase (`recover` and `restore_original` in
`tools/move_data_dir.sh`):

- **Before the rename** (phases `recording`, `stopping`, `copying`, and
  `renaming` before the rename itself) `data/` is still the state. Recovery
  removes the `.env` line, runs `docker compose up -d --no-build app` and
  verifies like step 6. Only a failure in `recording` leaves the app running
  throughout. From `stopping` on the app may already be stopped - a failed or
  mismatched safety copy (`copying`) is exactly that case - and it stays down
  until recovery's `up` starts it again.
- **After the rename** it stops the app, moves the rebuilt `data/` aside,
  renames the destination back, refreshes the reference files, removes the
  line, starts the app and verifies.

Then:

| Ends with | Exit | Means |
|---|---|---|
| `RECOVERED: the app is serving from <checkout>/data again. The move did not happen.` | 1 | verified like step 6 |
| `RECOVERY INCOMPLETE.` | 3 | **production may be down**; it prints the commands of "By hand" below with the real paths filled in |

SIGKILL and power loss run no trap: see "By hand".

## After

```bash
bash tools/backfill_status.sh
```

```bash
tail -n 5 <destination>/autopilot-deploy.log
```

The next watcher tick (≤ 5 min) logs into the new directory. From now on the
state is not in the tree: `git status`, `git add` and a build context in the
checkout no longer see it, and `data/` holds only reference files, the
tombstone, `.orchestrator` and the launchd logs. `pytest` is a narrower case:
`tests/data_dir_guard.py` refuses writes to `<checkout>/data` and to
`Config.DATA_DIR`, and to `<destination>` only when `IDEALISTA_DATA_DIR` is
set in the environment of the pytest process (it reads `os.environ`, not
`.env`). A test that builds the path `<destination>` by hand is not stopped by
the guard; do not run the suite with that directory in a fixture.

Delete `~/IdealistaRank-backups/data-pre-move-<UTC>/` once the move is
trusted; it is stale from the first write after the move.

## Rollback

```bash
bash tools/move_data_dir.sh --rollback
```

It takes no argument (anything else is exit 2): it finds the moved directory
from the `.env` line or, when that line is lost, from what `data/.moved-to`
records. Like `--apply` it takes both locks first (`STOP:`, exit 1) and runs a
read-only preflight under them. It refuses, before anything is stopped, when
the tombstone is not a regular file or does not record a destination the way
the script writes one; `.env` and the tombstone name different directories;
both `data/` (without a tombstone) and the destination hold files; the
destination is not on the mounted filesystem of the checkout (the rename back
would fail with the app stopped); neither place holds `.deployed_sha`; the
moved directory cannot be found; the backup root fails the same checks as for
`--apply`; or a previous rollback's aside still holds an unrestored
`.orchestrator` and is not writable (make it writable first, `chmod u+w`).

Then it stops the app, renames the small rebuilt `data/` aside to
`~/IdealistaRank-backups/checkout-data-at-rollback-<UTC>/`, renames
`<destination>` back to `data/` (everything written since the
move comes back with it), refreshes the reference files from git, restores the
`.orchestrator` the rebuilt directory got meanwhile and marks it restored,
removes the `.env` line, starts the app and runs the verification of step 6.

| Ends with | Exit | Means / what to do |
|---|---|---|
| `NOT ROLLED BACK: N problem(s) above. Nothing was touched.` | 1 | fix what the lines above say |
| `ALREADY ROLLED BACK: ... Nothing was touched.` | 0 | nothing to undo and the app runs on `data/`; a second `--rollback` ends here |
| `ROLLED BACK: the app is serving from <checkout>/data.` | 0 | done; it names the aside it kept, if any |
| `ROLLED BACK, MARKER MISSING: ...` | 4 | the app **is** serving from `data/` (this exit is reached only after `restore_original` ended with `verify_serving` passing), but the script could not write `<aside>/.orchestrator.restored`. Before any later `--rollback`, run the `touch <aside>/.orchestrator.restored` line it prints; without the marker a later rollback would put that older `.orchestrator` over a newer one |
| `ROLLBACK INCOMPLETE.` | 3 | **production may be down**; it prints the commands of "By hand" |

`--rollback` is idempotent and reads what is on disk, so a rollback that was
interrupted, or a move that died, is finished by running it again; with
nothing to undo but the app not up on `data/`, it starts the app and verifies.

**Reverting the code of #639 while the state is moved requires `--rollback`
first, and the rollback is the owner's command**, like the move: an agent that
meets the refusal reports it and waits. The deploy watcher that is serving
enforces it (`tools/autopilot/deploy_watcher.sh`, section "a moved state
directory needs a build that knows where it is (#639)", which ends in
`note_refused_tick` and `exit 0`): while the state
directory is not the checkout's own, it refuses - before the checkpoint, the
fast-forward and the handover - a target commit that does not carry
`tools/autopilot/lib/data_dir.sh` as a file, or whose `docker-compose.yml`
would mount anything other than the current state directory at `/app/data`.
Compose is asked about the target commit's whole tree, written to a temporary
directory with this checkout's `.env` linked in (`data_dir.sh
--commit-source`), so a file the compose file pulls in (`include`, `extends`,
`env_file`) is the target's; a tree that cannot be written out is refused. It logs `REFUSING to deploy ...`,
counts the tick toward the stall alarm (#532) and leaves the serving build
untouched. A hand `docker compose up --build` of such a tree is not gated -
one more reason hand builds on the mini are the watcher's job.

## By hand, when the script cannot run (SIGKILL, power loss mid-move)

Try `bash tools/move_data_dir.sh --rollback` first. If it cannot finish, these
are the commands it prints on exit 3, to be run in order, stopping at the
first that fails:

```bash
cd <checkout>
```

```bash
env -u IDEALISTA_DATA_DIR docker compose -f docker-compose.yml stop app
```

Only if the state was renamed away (`<destination>` exists):

```bash
mkdir -p ~/IdealistaRank-backups
```

```bash
[ ! -e data ] || rmdir data 2>/dev/null || mv data ~/IdealistaRank-backups/checkout-data-at-rollback-manual
```

```bash
mv <destination> data
```

Then in every case:

```bash
[ -d data -a ! -e data/.moved-to ] && echo state-is-back
```

It must print `state-is-back`.

```bash
git checkout -- data
```

Remove the line `IDEALISTA_DATA_DIR=...` from `.env` with an editor (do not
print the file), then:

```bash
env -u IDEALISTA_DATA_DIR docker compose -f docker-compose.yml up -d --no-build app
```

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/properties
```

It must print `200`.

The directory set aside holds the `.orchestrator` sessions agreed on while the
state was moved. After a manual rollback copy it back if it is newer than
`data/.orchestrator`, then mark it, so a later `--rollback` does not restore it
again over a newer one:

```bash
touch ~/IdealistaRank-backups/checkout-data-at-rollback-manual/.orchestrator.restored
```

A safety copy from before the move is under
`~/IdealistaRank-backups/data-pre-move-*`; it is older than anything written
since.

## What the move does not do

- It does not edit LaunchAgents. Their stdout logs stay in `<checkout>/data`.
- It does not delete the old copies of the four reference files that travel
  to the new directory with the rename; nothing reads them there.
- It does not change anything inside the container: `/app/data` is still the
  path, and every documented `--snapshot data/x.json` still writes to the
  persisted directory.
- It does not delete the safety copy or the rollback asides; a person does.

## Known limits, merged as they are

Four findings from the second reviews of #713 and #714 were merged unfixed
(the review cap of two rounds was spent) and are kept in `docs/BACKLOG.md`.
None was reproduced on live. Three of them stop the script or the watcher and
move nothing; one is reachable only through a link put there by hand.

- `--rollback` refuses while an **older** set-aside directory is read-only and
  still holds an unmarked `.orchestrator`, even though this rollback sets aside
  into a fresh directory. Make that directory writable (`chmod u+w`) and run it
  again.
- `--rollback` follows a symbolic link put in place of
  `<aside>/.orchestrator.restored`: it creates an empty file where the link
  points and reports success. The script never creates such a link; do not
  leave one there.
- The watcher's gate reads a compose file that the target's `docker-compose.yml`
  includes **by an absolute path into the checkout** as the serving checkout
  has it, not as the target commit does. A target that changes the mount only in such a file would
  pass. The `docker-compose.yml` on `main` has no `include:`; add one only by a
  relative path.
- The watcher's gate resolves a relative mount such as `../state` against its
  temporary tree, so a state directory bound that way is refused and the deploy
  stops (`REFUSING to deploy`, counted toward the stall alarm). Name the state
  directory in `.env` by its absolute path, as `--apply` writes it.
