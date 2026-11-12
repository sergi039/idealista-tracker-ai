#!/bin/bash
# The watcher keeps its files where the container's state is mounted (#639).
#
# deploy_watcher.sh reads the in-flight markers a job writes inside the
# container and writes the deployment marker, the deferral count and the stall
# alarm next to them. All six of its paths used to say `${REPO_DIR}/data`; they
# now come from lib/data_dir.sh, the one place that says where the directory
# bound at /app/data is. Two things are pinned:
#
#   * every file is exactly where it was, in `<repo>/data` - that is what makes
#     the change inert;
#   * what the reader refuses stops the tick before docker is touched. The
#     watcher does not fall back to `<repo>/data`, and it does not follow a
#     variable docker-compose.yml does not read into a directory the container
#     is not writing.
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
printf '%s\n' "$*" >>"$DOCKER_LOG"
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
    git -C "$REPO" add file.txt .gitignore
    git -C "$REPO" commit --quiet -m "first"
    git -C "$REPO" remote add origin "$remote"
    git -C "$REPO" push --quiet origin main
    git -C "$REPO" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true
    HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
    : >"${WORK}/docker.log"
    : >"${WORK}/stderr"
}

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
printf 'OK: with nothing configured the watcher keeps everything in <repo>/data\n'

# --- scenario 2: what the reader refuses stops the tick ----------------------
# docker-compose.yml does not read IDEALISTA_DATA_DIR yet, so the container
# writes its markers in <repo>/data whatever .env says, and the reader refuses
# a .env that names the variable. A watcher that followed it would look for
# markers in STATE, find none, and deploy over the job the marker below stands
# for; one that fell back to <repo>/data would be guessing. It does neither:
# the tick ends before docker and before git, and nothing is written anywhere.
new_repo repo2
STATE="${WORK}/state2"
mkdir -p "${STATE}" "${REPO}/data/.inflight"
printf '%s\n' "$NOT_RESUMABLE" >"${REPO}/data/.inflight/job.41.json"
printf 'DB_PASSWORD=x\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
before="$(ls -A "${REPO}/data")"
set_top "$JOB"
run_watcher AUTOPILOT_DEFER_ON_INFLIGHT=1
[ "$WATCHER_RC" != "0" ] || fail "scenario 2: the watcher ran although the reader refused the state directory"
grep -q "does not read the variable" "${WORK}/stderr" \
    || fail "scenario 2: the refusal does not pass on why the reader refused"
grep -q "FATAL" "${WORK}/stderr" || fail "scenario 2: the watcher did not say it is refusing to run"
[ ! -s "${WORK}/docker.log" ] || fail "scenario 2: docker was called after the refusal"
[ -z "$(ls -A "$STATE")" ] || fail "scenario 2: the watcher wrote into the directory the variable names"
[ "$(ls -A "${REPO}/data")" = "$before" ] || fail "scenario 2: the watcher wrote into <repo>/data after the refusal"
printf 'OK: a state directory the reader refuses stops the tick before anything is touched\n'
