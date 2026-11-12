# Runbook: moving the deployment's state out of the checkout (#639)

On the Mac mini `/Users/ss/IdealistaRank` is both the production checkout and
the place sessions run `git` and `pytest`, and its `data/` is what the app
container mounts at `/app/data`. This runbook moves that directory to
`/Users/ss/IdealistaRank-data` and back. **The move is the owner's command.**
Nothing in the repository performs it on a merge, a deploy or a schedule.

Background and the rule: `docs/rules/run-and-deploy.md`, "Where the
deployment's state lives".

## What moves and what stays

| | Where it is after the move |
|---|---|
| mail cursor and retry list, `google_spend.jsonl`, `attachments/`, score and coordinate snapshots, `.inflight/`, `.deployed_sha`, deferral and stall markers, autopilot logs, `autopilot-reviews.tsv`, supervisor locks | `/Users/ss/IdealistaRank-data` |
| the four committed reference files | still tracked in `<repo>/data`; the container reads the copy baked into its image (`REFERENCE_DATA_DIR`) |
| `data/.orchestrator` | `<repo>/data` (copied back; sessions read it from the checkout) |
| launchd stdout logs (`*.launchd.log`, `ai-bridge.log`) | `<repo>/data` — their paths are fixed in the installed LaunchAgents, which this change does not touch; they are logs, not state |
| `data/.moved-to` | `<repo>/data`: the tombstone. With it present, the app and every host tool refuse to use `<repo>/data` as the state directory |

Why a plain directory and not a Docker volume: a named volume lives in Docker
Desktop's disk image, outside Time Machine; the watcher, `backfill_status.sh`,
the supervisor and `backup_attachments.sh` must read the very files the
container writes; and a snapshot has to be restorable by a person from the
host. `/Users/ss/IdealistaRank-data` is on the internal volume, next to
`~/IdealistaRank-backups`, inside Time Machine.

## Before

1. The build that carries this runbook is what serves:
   `cat data/.deployed_sha` equals `git rev-parse HEAD` in the main checkout.
2. No backfill, no merge train in flight, not around 03:30 (the nightly dump).
3. A shell on the mini, in the main checkout, with the variable NOT exported:

```bash
cd /Users/ss/IdealistaRank
```

```bash
unset IDEALISTA_DATA_DIR
```

## Check (read-only, repeat as often as you like)

```bash
bash tools/move_data_dir.sh --check /Users/ss/IdealistaRank-data
```

It prints one line per condition and ends with `READY:` or `NOT MOVED: N
problem(s)`. It refuses when: the variable is exported in the shell; the
destination exists, is relative, is inside the repository or on another
volume; the checkout is not on `main` or is not clean; `.env` already names
the variable or a tombstone exists; a deploy is pending; the app is not
running or does not mount `<repo>/data`; the running image does not carry the
reference files; another container mounts the directory; `data/` contains a
symbolic link; `tools/backfill_status.sh` answers busy or unknown.

## Move

```bash
bash tools/move_data_dir.sh --apply /Users/ss/IdealistaRank-data
```

What it does, in order — each step is verified before the next:

1. the same preflight; then takes the deploy lock and the merge lock and holds
   them to the end (a watcher tick during the move logs "another deploy is in
   progress" and skips);
2. `docker compose stop app` and confirms the container is stopped — downtime
   starts; only then records a sha256 manifest of `data/`;
3. copies `data/` to `~/IdealistaRank-backups/data-pre-move-<UTC>/` and
   compares manifests (a difference confined to `*.log` is reported and
   tolerated; anything else aborts);
4. `mv data /Users/ss/IdealistaRank-data` — one rename, so there is never a
   second authoritative copy — and compares the manifest again;
5. recreates `data/` with the tombstone, `git checkout -- data` (the four
   reference files), `.orchestrator` copied back; writes
   `IDEALISTA_DATA_DIR=/Users/ss/IdealistaRank-data` to `.env` (temporary
   file, fsync, atomic rename; the file is never printed);
6. `docker compose up -d --no-build app` — nothing is built; Compose recreates
   the container because its mount changed;
7. verifies: the mount source; `/api/healthz`; `/properties` answers **200**
   (`tools/autopilot/lib/render_check.sh`; a 3xx is a failure); a token written
   on the host is read back through the container; the reference files are
   readable in the image.

It ends with `MOVED:`. Expected downtime: about a minute (stop ≤ 10 s, the
copy and three checksum passes over ~180 MB in seconds, container start with
the migration runner 15–30 s). Ingestion is scheduled; a missed minute is
picked up by the next tick.

**If anything fails after the app was stopped** — a command, a check, Ctrl-C, a
closed terminal — the script itself returns the directory to `data/`, removes
the `.env` line and starts the app, and says `RECOVERED:` (exit 1). If it
cannot finish that, it says `RECOVERY INCOMPLETE` (exit 3) and prints the
commands below with the real paths filled in.

## After

```bash
bash tools/backfill_status.sh
```

```bash
tail -n 5 /Users/ss/IdealistaRank-data/autopilot-deploy.log
```

The next watcher tick (≤ 5 min) should log into the new directory. From now on
`git status`, `pytest` and a build in the checkout cannot reach the state:
the directory is not in the tree, and `data/` holds only reference files, the
tombstone and logs.

Delete `~/IdealistaRank-backups/data-pre-move-<UTC>/` once the move is
trusted; it is stale from the first write after the move.

## Rollback

```bash
bash tools/move_data_dir.sh --rollback
```

Stops the app, moves the small rebuilt `data/` aside to
`~/IdealistaRank-backups/checkout-data-at-rollback-<UTC>/`, renames
`/Users/ss/IdealistaRank-data` back to `data/` (everything written since the
move comes back with it), refreshes the reference files from git, restores the
newer `.orchestrator`, removes the `.env` line, starts the app and runs the
same verification. Ends with `ROLLED BACK:`.

**Reverting the code of this change while the state is moved requires
`--rollback` first.** The deploy watcher enforces it: while
`IDEALISTA_DATA_DIR` names another directory, it refuses to deploy a commit
that no longer carries `tools/autopilot/lib/data_dir.sh` or whose
`docker-compose.yml` no longer mounts the variable, and the refusal counts
toward the stall alarm (#532). A hand `docker compose up --build` of such a
tree is not gated — which is one more reason hand builds on the mini are the
watcher's job.

## By hand, when the script cannot run (SIGKILL, power loss mid-move)

`--rollback` is idempotent and reads what is on disk, so try it first. The
same sequence as commands, to be run in order, stopping at the first failure:

```bash
cd /Users/ss/IdealistaRank
```

```bash
env -u IDEALISTA_DATA_DIR docker compose stop app
```

Only if `/Users/ss/IdealistaRank-data` exists and `data/.moved-to` exists:

```bash
mv data ~/IdealistaRank-backups/checkout-data-at-rollback-manual
```

```bash
mv /Users/ss/IdealistaRank-data data
```

```bash
git checkout -- data
```

Remove the line `IDEALISTA_DATA_DIR=...` from `.env` with an editor, then:

```bash
env -u IDEALISTA_DATA_DIR docker compose up -d --no-build app
```

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/properties
```

It must print `200`.

If `/Users/ss/IdealistaRank-data` does not exist and `data/` has no
`.moved-to`, nothing was moved: only the last two commands apply.

## What the move does not do

- It does not edit LaunchAgents. Their stdout logs stay in `<repo>/data`.
- It does not delete the old copies of the four reference files that travel
  to the new directory with the rename; nothing reads them there.
- It does not change anything inside the container: `/app/data` is still the
  path, and every documented `--snapshot data/x.json` still writes to the
  persisted directory.
