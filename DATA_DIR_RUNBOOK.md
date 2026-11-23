# Runbook: moving the deployment's state out of the checkout (#639)

On the Mac mini `/Users/ss/IdealistaRank` is both the production checkout and
the place sessions run `git` and `pytest`, and its `data/` is what the app
container mounts at `/app/data`. This runbook moves that directory to
`/Users/ss/IdealistaRank-data` and back with `tools/move_data_dir.sh`.
**The move is the owner's command.** Nothing in the repository performs it on
a merge, a deploy or a schedule.

Background and the rule: `docs/rules/run-and-deploy.md`, "Where the
deployment's state lives". Every message quoted below is the script's own
text; the exit codes are the ones `tools/move_data_dir_test.sh` holds it to.

## What moves and what stays

| | Where it is after the move |
|---|---|
| mail cursor and retry list, `google_spend.jsonl`, `attachments/`, score and coordinate snapshots, `.inflight/`, `.deployed_sha`, deferral and stall markers, autopilot logs, `autopilot-reviews.tsv`, supervisor locks | `/Users/ss/IdealistaRank-data` |
| the four committed reference files | still tracked in `<repo>/data`; the container reads the copy baked into its image (`REFERENCE_DATA_DIR`, `docs/rules/reference-data.md`) |
| `data/.orchestrator` | `<repo>/data` (copied back; sessions read it from the checkout) |
| launchd stdout logs (`*.launchd.log`, `ai-bridge.log`) | `<repo>/data`: their paths are fixed in the installed LaunchAgents, which the move does not touch; they are logs, not state |
| `data/.moved-to` | `<repo>/data`: the tombstone. While it exists, `tools/autopilot/lib/data_dir.sh` refuses to answer `<repo>/data` and the app refuses to start on it |

Why a plain directory and not a Docker volume: a named volume lives in Docker
Desktop's disk image, outside Time Machine; the watcher, `backfill_status.sh`,
the supervisor and `backup_attachments.sh` must read the very files the
container writes; and a snapshot has to be restorable by a person from the
host. `/Users/ss/IdealistaRank-data` is on the internal volume, next to
`~/IdealistaRank-backups`, inside Time Machine.

## Before

1. No backfill, no merge train in flight, not around 03:30 Madrid (the nightly
   dump).
2. A shell on the mini, in the main checkout, with none of the variables the
   script refuses exported:

```bash
cd /Users/ss/IdealistaRank
```

```bash
unset IDEALISTA_DATA_DIR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROJECT_NAME
```

## Check (read-only, repeat as often as you like)

```bash
bash tools/move_data_dir.sh --check /Users/ss/IdealistaRank-data
```

`--check` is the default mode and takes no lock. It prints one `ok:` or
problem line per condition and ends with `READY:` (exit 0) or `NOT MOVED: N
problem(s) above. Nothing was touched.` (exit 1). A wrong argument count is
exit 2. It refuses when:

- `IDEALISTA_DATA_DIR`, `COMPOSE_ENV_FILES`, `COMPOSE_DISABLE_ENV_FILE` or
  `COMPOSE_PROJECT_NAME` is exported in the shell; `.env`,
  `lib/data_dir.sh` or `lib/render_check.sh` is missing; `docker`, `git`,
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
bash tools/move_data_dir.sh --apply /Users/ss/IdealistaRank-data
```

It first takes the deploy lock and the merge lock, and holds both until it
exits; a held lock ends it with `STOP: ... Nothing was touched.` (exit 1).
**Then** it runs the same preflight under the locks, so the checks describe
the world the move acts on; a refusal is `NOT MOVED: N problem(s)` (exit 1),
nothing touched. Then, in order, each step verified before the next:

0. **recording**: writes `IDEALISTA_DATA_DIR=/Users/ss/IdealistaRank-data` to
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
   `data` -> `/Users/ss/IdealistaRank-data`, so there is never a second
   authoritative copy, and compares the manifest again;
4. **switching**: recreates `data/` with the tombstone written and fsynced
   first, `git checkout -- data` (the four reference files), `.orchestrator`
   copied back; then asks `lib/data_dir.sh` that `.env` now resolves to the
   destination;
5. **starting**: `docker compose up -d --no-build app` - nothing is built;
   Compose recreates the container because its mount changed;
6. **verifying**: the mount source; `/api/healthz`; the page check of
   `tools/autopilot/lib/render_check.sh` (`DEPLOY_RENDER_PATH`) answers
   **200** - a 3xx, or the check switched off, is a failure; a token written on
   the host is read back through the container; the reference files are in the
   image.

Its own `docker compose` calls drop `IDEALISTA_DATA_DIR` from the environment,
so only `.env` decides the mount. It ends with `MOVED:` (exit 0) and prints the
`.env` line, the safety copy and the undo command. The script announces about
a minute of downtime.

**If anything fails after the preflight** - a command, a check, Ctrl-C, a
closed terminal (`HUP`, `INT`, `TERM`) - the script prints `FAILED in phase
'<phase>'` and runs the same idempotent recovery `--rollback` uses; it reads
the disk, not the phase. A failure before the rename (phase `recording`) only
removes the `.env` line and makes sure the app is up; nothing is stopped. Later
it stops the app, moves a rebuilt `data/` aside, renames the destination back,
removes the line and starts the app. Then:

| Ends with | Exit | Means |
|---|---|---|
| `RECOVERED: the app is serving from <repo>/data again. The move did not happen.` | 1 | verified like step 6 |
| `RECOVERY INCOMPLETE.` | 3 | **production may be down**; it prints the commands of "By hand" below with the real paths filled in |

SIGKILL and power loss run no trap: see "By hand".

## After

```bash
bash tools/backfill_status.sh
```

```bash
tail -n 5 /Users/ss/IdealistaRank-data/autopilot-deploy.log
```

The next watcher tick (≤ 5 min) logs into the new directory. From now on
`git status`, `pytest` and a build in the checkout cannot reach the state: the
directory is not in the tree, and `data/` holds only reference files, the
tombstone, `.orchestrator` and the launchd logs.

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
`/Users/ss/IdealistaRank-data` back to `data/` (everything written since the
move comes back with it), refreshes the reference files from git, restores the
`.orchestrator` the rebuilt directory got meanwhile and marks it restored,
removes the `.env` line, starts the app and runs the verification of step 6.

| Ends with | Exit | Means / what to do |
|---|---|---|
| `NOT ROLLED BACK: N problem(s) above. Nothing was touched.` | 1 | fix what the lines above say |
| `ALREADY ROLLED BACK: ... Nothing was touched.` | 0 | nothing to undo and the app runs on `data/`; a second `--rollback` ends here |
| `ROLLED BACK: the app is serving from <repo>/data.` | 0 | done; it names the aside it kept, if any |
| `ROLLED BACK, MARKER MISSING: ...` | 4 | the app **is** serving from `data/`, but the script could not write `<aside>/.orchestrator.restored`. Before any later `--rollback`, run the `touch <aside>/.orchestrator.restored` line it prints; without the marker a later rollback would put that older `.orchestrator` over a newer one |
| `ROLLBACK INCOMPLETE.` | 3 | **production may be down**; it prints the commands of "By hand" |

`--rollback` is idempotent and reads what is on disk, so a rollback that was
interrupted, or a move that died, is finished by running it again; with
nothing to undo but the app not up on `data/`, it starts the app and verifies.

**Reverting the code of #639 while the state is moved requires `--rollback`
first.** The deploy watcher that is serving enforces it: while the state
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
cd /Users/ss/IdealistaRank
```

```bash
env -u IDEALISTA_DATA_DIR docker compose -f docker-compose.yml stop app
```

Only if the state was renamed away (`/Users/ss/IdealistaRank-data` exists):

```bash
mkdir -p ~/IdealistaRank-backups
```

```bash
[ ! -e data ] || rmdir data 2>/dev/null || mv data ~/IdealistaRank-backups/checkout-data-at-rollback-manual
```

```bash
mv /Users/ss/IdealistaRank-data data
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

- It does not edit LaunchAgents. Their stdout logs stay in `<repo>/data`.
- It does not delete the old copies of the four reference files that travel
  to the new directory with the rename; nothing reads them there.
- It does not change anything inside the container: `/app/data` is still the
  path, and every documented `--snapshot data/x.json` still writes to the
  persisted directory.
- It does not delete the safety copy or the rollback asides; a person does.
