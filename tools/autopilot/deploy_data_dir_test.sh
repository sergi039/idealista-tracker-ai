#!/bin/bash
# The watcher keeps its files where the container's state is mounted (#639).
#
# deploy_watcher.sh reads the in-flight markers a job writes inside the
# container and writes the deployment marker, the deferral count and the stall
# alarm next to them. All six of its paths used to say `${REPO_DIR}/data`; they
# now come from lib/data_dir.sh, the one reader of IDEALISTA_DATA_DIR - the
# variable docker-compose.yml takes the bind mount from. Two things are pinned:
#
#   * nothing configured -> every file is exactly where it was, in
#     `<repo>/data`. That is what makes the change inert until a machine's
#     .env says otherwise;
#   * a configured directory moves ALL of them together. One left behind is a
#     watcher looking for markers in a directory the container no longer
#     writes, i.e. a deploy that kills a job it reported as absent.
#
# And a value that cannot be used stops the tick before docker is touched,
# rather than falling back to `<repo>/data`.
#
# Drives the real deploy_watcher.sh with none of the AUTOPILOT_* path
# overrides the other harnesses set - those bypass the very defaults under
# test.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER="${SCRIPT_DIR}/deploy_watcher.sh"

WORK="$(mktemp -d)"
HEALTH_PID=""
cleanup() {
    local rc=$?
    if [ -n "$HEALTH_PID" ]; then
        kill "$HEALTH_PID" 2>/dev/null
        wait "$HEALTH_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    printf '%s\n' "--- watcher stderr ---" >&2
    cat "${WORK}/stderr" >&2 2>/dev/null || true
    printf '%s\n' "--- docker calls ---" >&2
    cat "${WORK}/docker.log" >&2 2>/dev/null || true
    local log
    for log in "${WORK}"/*/data/autopilot-deploy.log "${WORK}"/state*/autopilot-deploy.log; do
        [ -f "$log" ] || continue
        printf -- '--- %s ---\n' "$log" >&2
        cat "$log" >&2
    done
    exit 1
}

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

# --- a docker that works, and records what it was asked ---------------------
STUB_BIN="${WORK}/bin"
mkdir -p "$STUB_BIN"
TOP_FILE="${WORK}/docker-top.txt"
cat >"${STUB_BIN}/docker" <<'STUB'
#!/bin/bash
printf '%s | IDEALISTA_DATA_DIR=%s\n' "$*" "${IDEALISTA_DATA_DIR-<unset>}" >>"$DOCKER_LOG"
case "$1" in
    ps) printf '%s\n' "idealista-app" ;;
    top) cat "$DOCKER_TOP_FILE" 2>/dev/null || true ;;
    inspect) printf '%s\n' "7" ;;
esac
exit 0
STUB
chmod +x "${STUB_BIN}/docker"

JOB='python -m utils.backfill_pool --snapshot data/pool_backfill.json'
set_top() {
    # set_top [job command]: the app server, plus one job when given.
    {
        printf 'UID PID PPID C STIME TTY TIME CMD\n'
        printf 'appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 main:app\n'
        if [ $# -gt 0 ]; then
            printf 'appuser 4711 4700 0 12:00 ? 00:00:01 %s\n' "$1"
        fi
    } >"$TOP_FILE"
}
RESUMABLE='{"module":"backfill_pool","argv":["--snapshot","data/pool_backfill.json"],"resumable":true,"ledger":"data/pool_backfill.json.ledger.jsonl"}'
NOT_RESUMABLE='{"module":"backfill_pool","argv":["--snapshot","data/pool_backfill.json"],"resumable":false}'

# --- an app that is healthy and renders its page ----------------------------
HEALTH_PORT=""
for candidate in $(seq 45931 45959); do
    if ! nc -z 127.0.0.1 "$candidate" 2>/dev/null; then
        HEALTH_PORT="$candidate"
        break
    fi
done
[ -n "$HEALTH_PORT" ] || fail "no free port for the health stub"

python3 - "$HEALTH_PORT" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true,"scheduler":"running","checks":{"database":"ok"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
HEALTH_PID=$!
for _ in $(seq 1 50); do
    curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/" >/dev/null 2>&1 \
    || fail "health stub never came up"

# --- a repository with something to deploy, per scenario --------------------
new_repo() {
    # new_repo <name>: sets REPO and HEAD_SHA.
    local remote="${WORK}/$1.git"
    REPO="${WORK}/$1"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$REPO"
    printf 'first\n' >"${REPO}/file.txt"
    # What a real checkout has: `data/*` and `.env` are ignored, so neither
    # the state nor the variable's home makes the tree dirty.
    printf 'data/*\n.env\n' >"${REPO}/.gitignore"
    # The state-directory contract, as a commit that may be deployed while the
    # state is moved has to carry it (the gate of scenario 5 reads both).
    mkdir -p "${REPO}/tools/autopilot/lib"
    cp "${SCRIPT_DIR}/lib/data_dir.sh" "${REPO}/tools/autopilot/lib/data_dir.sh"
    # shellcheck disable=SC2016
    printf 'services:\n  app:\n    volumes:\n      - type: bind\n        source: ${IDEALISTA_DATA_DIR:-./data}\n        target: /app/data\n' \
        >"${REPO}/docker-compose.yml"
    git -C "$REPO" add file.txt .gitignore tools docker-compose.yml
    git -C "$REPO" commit --quiet -m "first"
    git -C "$REPO" remote add origin "$remote"
    git -C "$REPO" push --quiet origin main
    git -C "$REPO" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true
    HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
    : >"${WORK}/docker.log"
    : >"${WORK}/stderr"
}

# Send the throwaway repository's main to its throwaway bare remote.
publish_main() { git -C "$REPO" push --quiet origin main; }

run_watcher() {
    # run_watcher [VAR=value ...]: no path overrides - the defaults are the
    # subject. Leaves WATCHER_RC.
    set +e
    env -u IDEALISTA_DATA_DIR \
        PATH="${STUB_BIN}:${PATH}" \
        DOCKER_LOG="${WORK}/docker.log" \
        DOCKER_TOP_FILE="$TOP_FILE" \
        AUTOPILOT_REPO_DIR="$REPO" \
        AUTOPILOT_LOCK_DIR="${WORK}/lock.d" \
        AUTOPILOT_HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/healthz" \
        AUTOPILOT_HEALTH_TIMEOUT=15 \
        AUTOPILOT_COMPOSE_FILE="docker-compose.yml" \
        "$@" \
        /bin/bash "$WATCHER" >/dev/null 2>"${WORK}/stderr"
    WATCHER_RC=$?
    set -e
}

built() { grep -q -- "--build" "${WORK}/docker.log"; }

expect_all_in() {
    # expect_all_in <label> <directory>: a finished deploy left its log and
    # its marker there, and read the in-flight marker from there.
    local label="$1" dir="$2"
    [ -f "${dir}/autopilot-deploy.log" ] \
        || fail "${label}: no deploy log in ${dir}"
    [ "$(cat "${dir}/.deployed_sha" 2>/dev/null || true)" = "$HEAD_SHA" ] \
        || fail "${label}: ${dir}/.deployed_sha does not name the deployed commit"
    grep -q "in flight (resumable): ${JOB}" "${dir}/autopilot-deploy.log" \
        || fail "${label}: the in-flight marker in ${dir}/.inflight was not the one read"
}

expect_nothing_in() {
    # expect_nothing_in <label> <directory>: none of the watcher's files.
    local label="$1" dir="$2" name
    for name in autopilot-deploy.log .deployed_sha .deploy_deferrals \
        .deploy_stall_ticks .deploy_stalled; do
        [ ! -e "${dir}/${name}" ] || fail "${label}: ${dir}/${name} was written"
    done
}

# --- scenario 1: nothing configured -----------------------------------------
# Every file where it has always been. The marker in <repo>/data/.inflight is
# what turns the job from "unknown" into "resumable", so reading it is
# observable in the log.
new_repo repo1
mkdir -p "${REPO}/data/.inflight"
printf '%s\n' "$RESUMABLE" >"${REPO}/data/.inflight/job.41.json"
set_top "$JOB"
run_watcher
built || fail "scenario 1 did not deploy"
expect_all_in "scenario 1" "${REPO}/data"
# With nothing configured the watcher's Compose calls are what they were: the
# variable is not handed to them.
grep '^compose ' "${WORK}/docker.log" | grep -v 'IDEALISTA_DATA_DIR=<unset>' >/dev/null \
    && fail "scenario 1: the watcher exported IDEALISTA_DATA_DIR with nothing configured"
printf 'OK: with nothing configured the watcher keeps everything in <repo>/data\n'

# --- scenario 2: .env names a directory outside the repository --------------
# All of it moves, and nothing is left in <repo>/data. The decoy marker there
# says NOT resumable: a watcher still reading the old directory would say so.
new_repo repo2
STATE="${WORK}/state2"
mkdir -p "${STATE}/.inflight" "${REPO}/data/.inflight"
printf '%s\n' "$RESUMABLE" >"${STATE}/.inflight/job.41.json"
printf '%s\n' "$NOT_RESUMABLE" >"${REPO}/data/.inflight/job.41.json"
printf 'DB_PASSWORD=x\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
set_top "$JOB"
run_watcher
built || fail "scenario 2 did not deploy"
expect_all_in "scenario 2" "$STATE"
expect_nothing_in "scenario 2" "${REPO}/data"
# The mount and the watcher's files come from one resolution: every Compose
# call was handed the directory the reader resolved.
grep -q '^compose ' "${WORK}/docker.log" || fail "scenario 2: Compose was never called"
grep '^compose ' "${WORK}/docker.log" | grep -v "IDEALISTA_DATA_DIR=${STATE}\$" >/dev/null \
    && fail "scenario 2: a Compose call was not handed the resolved state directory"
printf 'OK: a configured state directory takes the log, the marker and the in-flight markers\n'

# --- scenario 3: the deferral count and the stall alarm move with it --------
# A job that does not claim to be resumable, deferral switched on, and an
# alarm threshold of one tick: this tick defers, counts itself, and raises the
# alarm - three more files, all of which have to be in the same directory.
new_repo repo3
STATE="${WORK}/state3"
mkdir -p "${STATE}/.inflight"
printf '%s\n' "$NOT_RESUMABLE" >"${STATE}/.inflight/job.41.json"
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
# Something older is serving, so main is measurably ahead of it.
printf '%s\n' "$HEAD_SHA" >"${STATE}/.deployed_sha"
printf 'second\n' >>"${REPO}/file.txt"
git -C "$REPO" commit --quiet -am "second"
git -C "$REPO" push --quiet origin main
git -C "$REPO" reset --quiet --hard HEAD~1
set_top "$JOB"
run_watcher AUTOPILOT_DEFER_ON_INFLIGHT=1 AUTOPILOT_STALL_THRESHOLD=1
built && fail "scenario 3 deployed over a job that does not claim to be resumable"
for name in .deploy_deferrals .deploy_stall_ticks .deploy_stalled; do
    [ -e "${STATE}/${name}" ] || fail "scenario 3: ${name} is not in the configured directory"
done
expect_nothing_in "scenario 3" "${REPO}/data"
printf 'OK: the deferral count and the stall alarm live in the configured directory too\n'

# --- scenario 4: a directory that does not exist stops the tick -------------
# Before docker, before git, and without creating either the missing
# directory or a <repo>/data to fall back on.
new_repo repo4
MISSING="${WORK}/missing"
printf 'IDEALISTA_DATA_DIR=%s\n' "$MISSING" >"${REPO}/.env"
set_top
run_watcher
[ "$WATCHER_RC" != "0" ] || fail "scenario 4: the watcher exited 0 on a state directory that does not exist"
grep -q "not an existing directory" "${WORK}/stderr" \
    || fail "scenario 4: the refusal does not say what is wrong"
grep -q "FATAL" "${WORK}/stderr" \
    || fail "scenario 4: the watcher did not say it is refusing to run"
[ ! -s "${WORK}/docker.log" ] || fail "scenario 4: docker was called after the refusal"
[ ! -e "$MISSING" ] || fail "scenario 4: the missing directory was created"
[ ! -e "${REPO}/data" ] || fail "scenario 4: the watcher fell back to <repo>/data"
printf 'OK: an unusable state directory stops the tick before anything is touched\n'

# --- scenario 5: a commit without the contract is not deployed while the ----
# --- state is moved ----------------------------------------------------------
# A revert of #639 reaches main while the state lives outside the checkout.
# Its docker-compose.yml binds ./data again - the checkout's directory, which
# by now has no mail cursor. Nothing in that commit can refuse itself, so the
# watcher that is serving now does: before the fast-forward, with the checkout,
# the container and the marker untouched. Two shapes of "no contract".
for shape in compose-binds-data reader-removed; do
    new_repo "repo5-${shape}"
    STATE="${WORK}/state5-${shape}"
    mkdir -p "$STATE"
    printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
    printf '%s\n' "$HEAD_SHA" >"${STATE}/.deployed_sha"
    serving_sha="$HEAD_SHA"
    case "$shape" in
        compose-binds-data)
            # The variable survives only in a comment: not a mount.
            # shellcheck disable=SC2016
            printf '# was: ${IDEALISTA_DATA_DIR:-./data}\nservices:\n  app:\n    volumes:\n      - ./data:/app/data\n' \
                >"${REPO}/docker-compose.yml"
            ;;
        reader-removed)
            git -C "$REPO" rm --quiet tools/autopilot/lib/data_dir.sh
            ;;
    esac
    git -C "$REPO" commit --quiet -am "revert the state-directory contract (${shape})"
    publish_main
    git -C "$REPO" reset --quiet --hard "$serving_sha"
    : >"${WORK}/docker.log"
    set_top
    run_watcher AUTOPILOT_STALL_THRESHOLD=1
    built && fail "scenario 5 (${shape}): a commit without the contract was built while the state is moved"
    [ ! -s "${WORK}/docker.log" ] || fail "scenario 5 (${shape}): docker was touched before the refusal"
    [ "$(git -C "$REPO" rev-parse HEAD)" = "$serving_sha" ] \
        || fail "scenario 5 (${shape}): the checkout was fast-forwarded to the refused commit"
    [ "$(cat "${STATE}/.deployed_sha")" = "$serving_sha" ] \
        || fail "scenario 5 (${shape}): the deployment marker moved"
    grep -q "REFUSING to deploy" "${STATE}/autopilot-deploy.log" \
        || fail "scenario 5 (${shape}): the refusal is not in the deploy log"
    grep -q "move_data_dir.sh --rollback" "${STATE}/autopilot-deploy.log" \
        || fail "scenario 5 (${shape}): the refusal does not say how to proceed"
    [ -e "${STATE}/.deploy_stalled" ] \
        || fail "scenario 5 (${shape}): the refusal did not reach the stall alarm"
done
printf 'OK: a commit without the state-directory contract is refused while the state is moved\n'

# --- scenario 6: the same commit deploys once the state is back -------------
# The gate is about a moved state directory, not about the commit: with
# nothing configured the watcher has no opinion on what docker-compose.yml
# mounts, exactly as before #639.
new_repo repo6
mkdir -p "${REPO}/data"
printf '%s\n' "$HEAD_SHA" >"${REPO}/data/.deployed_sha"
serving_sha="$HEAD_SHA"
printf 'services:\n  app:\n    volumes:\n      - ./data:/app/data\n' >"${REPO}/docker-compose.yml"
git -C "$REPO" rm --quiet tools/autopilot/lib/data_dir.sh
git -C "$REPO" commit --quiet -am "revert the state-directory contract"
publish_main
target_sha="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" reset --quiet --hard "$serving_sha"
: >"${WORK}/docker.log"
set_top
run_watcher
built || fail "scenario 6: with the state in <repo>/data the commit was not deployed"
[ "$(cat "${REPO}/data/.deployed_sha")" = "$target_sha" ] \
    || fail "scenario 6: the deployed marker does not name the new commit"
grep -q "REFUSING to deploy" "${REPO}/data/autopilot-deploy.log" \
    && fail "scenario 6: the gate fired although the state was never moved"
printf 'OK: with the state in <repo>/data the gate has no opinion\n'
