#!/bin/bash
# A deploy that kills long-running work has to say so (#283), and "healthy"
# has to mean a page rendered.
#
# Observed twice on 2026-08-14: a pool backfill was running inside
# idealista-app when a merge landed, `docker compose up -d --build` recreated
# the container, and the run died mid-flight with nothing anywhere recording
# it - the watcher logged an ordinary successful deploy. Separately, a broken
# template turned every /properties/<id> into a redirect for 15 minutes while
# /api/healthz stayed green, because healthz renders no template.
#
# This drives the real deploy_watcher.sh against a throwaway repository, a
# stub docker whose `top` reports whatever a scenario needs, and an HTTP stub
# serving one page - which page, and with which status, the scenario chooses.
# Everything else it 404s, so a fetch of a path nobody configured cannot pass.

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
    printf '%s\n' "--- watcher log ---" >&2
    cat "${WORK}/watcher.log" >&2 2>/dev/null || true
    printf '%s\n' "--- docker calls ---" >&2
    cat "${WORK}/docker.log" >&2 2>/dev/null || true
    exit 1
}

# --- a repository with something to deploy ---------------------------------
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

REMOTE="${WORK}/remote.git"
REPO="${WORK}/repo"
git init --quiet --bare --initial-branch=main "$REMOTE"
git init --quiet --initial-branch=main "$REPO"
cd "$REPO"
printf 'first\n' >file.txt
git add file.txt
git commit --quiet -m "first"
git remote add origin "$REMOTE"
git push --quiet origin main
git branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true

MARKER="${WORK}/deployed_sha"
INFLIGHT_DIR="${WORK}/inflight"
DEFER_STATE="${WORK}/deferrals"
TOP_FILE="${WORK}/docker-top.txt"
PAGE_STATUS_FILE="${WORK}/page_status"
# Which path the stub app serves as "the page". Everything else is a 404, so a
# watcher that fetched a path nobody configured fails loudly instead of
# stumbling onto a 200.
PAGE_PATH_FILE="${WORK}/page_path"
mkdir -p "$INFLIGHT_DIR"

# --- a docker that answers, and records what it was asked -------------------
STUB_BIN="${WORK}/bin"
mkdir -p "$STUB_BIN"
cat >"${STUB_BIN}/docker" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$1" in
    ps)
        printf '%s\n' "${DOCKER_PS_OUTPUT:-}"
        ;;
    top)
        # A docker that cannot answer is not a docker that answers "nothing".
        if [ "${DOCKER_TOP_RC:-0}" != "0" ]; then
            echo "stub docker: top failed" >&2
            exit "${DOCKER_TOP_RC}"
        fi
        cat "${DOCKER_TOP_FILE:-/dev/null}" 2>/dev/null || true
        ;;
esac
exit 0
STUB
chmod +x "${STUB_BIN}/docker"

# --- an app that answers healthz, and a page that may not -------------------
HEALTH_PORT=""
for candidate in $(seq 45901 45949); do
    if ! nc -z 127.0.0.1 "$candidate" 2>/dev/null; then
        HEALTH_PORT="$candidate"
        break
    fi
done
[ -n "$HEALTH_PORT" ] || fail "no free port for the health stub"

printf '200\n' >"$PAGE_STATUS_FILE"
printf '/properties\n' >"$PAGE_PATH_FILE"

python3 - "$HEALTH_PORT" "$PAGE_STATUS_FILE" "$PAGE_PATH_FILE" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATUS_FILE = sys.argv[2]
PATH_FILE = sys.argv[3]


def _read(path, fallback):
    with open(path) as handle:
        return handle.read().strip() or fallback


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith(_read(PATH_FILE, "/properties")):
            # The 2026-08-14 defect exactly: healthz green, the page a 302.
            code = int(_read(STATUS_FILE, "200"))
            body = b"<html>the page</html>"
            self.send_response(code)
            if code in (301, 302, 303, 307, 308):
                self.send_header("Location", "/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/healthz"):
            body = b'{"ok":true,"checks":{"database":"ok"},"scheduler":"running"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Anything else is a page nobody configured: never a silent 200.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
HEALTH_PID=$!

for _ in $(seq 1 50); do
    curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1 \
    || fail "health stub never came up"

# --- helpers ----------------------------------------------------------------
# Marker file names deliberately carry a PID that `docker top` never reports.
# The container and the host do not share a PID namespace - measured on the
# mini, a marker said pid 41 while docker top said 21974 - so a fixture that
# gave both sides the same number could not fail on the bug that mattered.
# Every marker here is written under a PID the process list does not contain.
MARKER_PID=41

set_inflight() {
    # set_inflight <command> [marker-json]
    local command="$1" marker="${2:-}"
    rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
    if [ -z "$command" ]; then
        : >"$TOP_FILE"
        return
    fi
    {
        printf 'UID PID PPID C STIME TTY TIME CMD\n'
        printf 'appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 main:app\n'
        printf 'appuser 4711 4700 0 12:00 ? 00:00:01 %s\n' "$command"
    } >"$TOP_FILE"
    if [ -n "$marker" ]; then
        printf '%s\n' "$marker" >"${INFLIGHT_DIR}/job.${MARKER_PID}.json"
    fi
}

set_top_rows() {
    # set_top_rows <<'EOF' ... rows without header ... EOF
    rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
    {
        printf 'UID PID PPID C STIME TTY TIME CMD\n'
        printf 'appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 main:app\n'
        cat
    } >"$TOP_FILE"
}

set_top_table() {
    # set_top_table <<'EOF' ... header AND rows, the scenario's own layout ...
    # For the cases where the column layout itself is the subject: `docker top`
    # renders whatever ps format it is handed, so a scenario has to be able to
    # hand it something other than the default eight columns.
    rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
    cat >"$TOP_FILE"
}

add_marker() {
    # add_marker <name> <json>
    printf '%s\n' "$2" >"${INFLIGHT_DIR}/$1.${MARKER_PID}.json"
    MARKER_PID=$((MARKER_PID + 1))
}

set_inflight_wrapped() {
    # The shape `docker exec ... sh -c 'python -m utils.X ... >> log'` really
    # produces, measured on the mini 2026-08-14: the shell and the python it
    # exec'd, both matching. The marker is written under a PID neither row
    # carries, because the container's namespace is not the host's.
    # set_inflight_wrapped <inner-command> [marker-json]
    local command="$1" marker="${2:-}"
    rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
    {
        printf 'UID PID PPID C STIME TTY TIME CMD\n'
        printf 'appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 main:app\n'
        printf 'appuser 4711 4700 0 12:00 ? 00:00:01 sh -c %s >> /app/data/run.log 2>&1\n' "$command"
        printf 'appuser 4713 4711 0 12:00 ? 00:00:01 %s\n' "$command"
    } >"$TOP_FILE"
    if [ -n "$marker" ]; then
        printf '%s\n' "$marker" >"${INFLIGHT_DIR}/job.${MARKER_PID}.json"
    fi
}

run_watcher() {
    printf '%s\n' "0000000000000000000000000000000000000000" >"$MARKER"
    : >"${WORK}/docker.log"
    set +e
    (
    # Only when a scenario asks for it: the point of most scenarios is that the
    # watcher finds the page through the shared default, with nothing set.
    [ -z "${RENDER_PATH_OVERRIDE+set}" ] || export DEPLOY_RENDER_PATH="$RENDER_PATH_OVERRIDE"
    [ -z "${LEGACY_PAGE_URL_OVERRIDE+set}" ] || export AUTOPILOT_PAGE_URL="$LEGACY_PAGE_URL_OVERRIDE"
    PATH="${STUB_BIN}:${PATH}" \
    DOCKER_LOG="${WORK}/docker.log" \
    DOCKER_TOP_FILE="$TOP_FILE" \
    DOCKER_TOP_RC="${DOCKER_TOP_RC:-0}" \
    DOCKER_PS_OUTPUT="idealista-app" \
    AUTOPILOT_REPO_DIR="$REPO" \
    AUTOPILOT_DEPLOYED_MARKER="$MARKER" \
    AUTOPILOT_LOG_FILE="${WORK}/watcher.log" \
    AUTOPILOT_LOCK_DIR="${WORK}/lock.d" \
    AUTOPILOT_HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/healthz" \
    AUTOPILOT_HEALTH_TIMEOUT="${HEALTH_TIMEOUT_OVERRIDE:-15}" \
    AUTOPILOT_COMPOSE_FILE="docker-compose.yml" \
    AUTOPILOT_INFLIGHT_DIR="$INFLIGHT_DIR" \
    AUTOPILOT_DEFER_STATE="$DEFER_STATE" \
    AUTOPILOT_DEFER_ON_INFLIGHT="${DEFER_ON_INFLIGHT:-0}" \
    AUTOPILOT_DEFER_BUDGET="${DEFER_BUDGET:-6}" \
        bash "${WATCHER_UNDER_TEST:-$WATCHER}" >/dev/null 2>&1
    )
    set -e
}

# A copy of the watcher next to a copy of lib/, so a scenario can damage the
# contract it loads without touching the repository. The watcher finds its lib
# through BASH_SOURCE, so the copy has to keep the same shape.
watcher_with_contract() {
    # watcher_with_contract <contents of lib/render_check.sh>
    local where="${WORK}/broken-tools"
    rm -rf "$where"
    mkdir -p "${where}/lib"
    cp "$WATCHER" "${where}/deploy_watcher.sh"
    cp "${SCRIPT_DIR}/lib/lock.sh" "${where}/lib/lock.sh"
    printf '%s' "$1" >"${where}/lib/render_check.sh"
    printf '%s' "${where}/deploy_watcher.sh"
}

built() {
    grep -q -- "up -d --build" "${WORK}/docker.log"
}

# --- scenario 1: nothing in flight ------------------------------------------
# The unchanged path: no job, no extra noise, a normal deploy.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
run_watcher

built || fail "scenario 1 did not deploy with nothing in flight"
if grep -q "in flight" "${WORK}/watcher.log"; then
    fail "scenario 1 reported work in flight although the container had none"
fi
grep -q "rendered (200)" "${WORK}/watcher.log" \
    || fail "scenario 1 never verified that a page renders"
printf 'OK: nothing in flight deploys exactly as before, and the page is verified\n'

# --- scenario 2: a resumable job is named, then killed ----------------------
# The default is unchanged - the watcher does not decide whose work matters -
# but the postmortem now exists without reading a ledger.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/pool_backfill.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/pool_backfill.json"],"resumable":true,"ledger":"data/pool_backfill.json.ledger.jsonl"}'
run_watcher

built || fail "scenario 2 refused to deploy although defer is off"
grep -q "in flight (resumable): python -m utils.backfill_pool" "${WORK}/watcher.log" \
    || fail "scenario 2 never named the job it was about to kill"
grep -q "ledger: data/pool_backfill.json.ledger.jsonl" "${WORK}/watcher.log" \
    || fail "scenario 2 never recorded where the ledger stands"
printf 'OK: a killed job is named in the deploy log, with its ledger\n'

# --- scenario 3: a job with no marker is unknown, not assumed resumable -----
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.bulk_ai_analysis --force"
run_watcher

grep -q "no marker, so resumability is unknown" "${WORK}/watcher.log" \
    || fail "scenario 3 did not report an unmarked job as unknown"
printf 'OK: a job that left no marker is reported as unknown, never as safe\n'

# --- scenario 4: defer, bounded --------------------------------------------
# With deferring on, a job that would lose work buys ticks - but only as many
# as the budget allows. A deploy that never lands is a failure too.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.recalc_property_travel --snapshot data/t.json" \
    '{"module":"recalc_property_travel","argv":["--snapshot","data/t.json"],"resumable":false}'

DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher
built && fail "scenario 4 deployed on the first tick although deferring is on"
grep -q "deferring this tick (1/2)" "${WORK}/watcher.log" \
    || fail "scenario 4 did not defer the first tick"

DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher
built && fail "scenario 4 deployed on the second tick"
grep -q "deferring this tick (2/2)" "${WORK}/watcher.log" \
    || fail "scenario 4 did not defer the second tick"

DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher
built || fail "scenario 4 never deployed - the deferral budget is unbounded"
grep -q "deferral budget exhausted" "${WORK}/watcher.log" \
    || fail "scenario 4 deployed without saying it had exhausted the budget"
printf 'OK: deferring is bounded, and the tick that gives up says so\n'

# --- scenario 5: a resumable job never defers -------------------------------
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/p.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

built || fail "scenario 5 deferred for a job that reports itself resumable"
grep -q "reports itself resumable" "${WORK}/watcher.log" \
    || fail "scenario 5 did not say why it deployed anyway"
printf 'OK: a resumable job is killed knowingly rather than deferred for\n'

# --- scenario 7: a marker from a reused PID vouches for nobody --------------
# Markers are keyed by PID, and the container that replaced a killed run hands
# the same numbers out again. A leftover `resumable: true` must not certify a
# different job: if it did, this tick would deploy instead of defer.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.recalc_property_travel --snapshot data/t.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

built && fail "scenario 7 believed a stale marker left by a different job"
grep -q "no marker, so resumability is unknown" "${WORK}/watcher.log" \
    || fail "scenario 7 did not discard the mismatched marker"
printf 'OK: a marker whose module does not match the process is not believed\n'

# --- scenario 8: a shell-wrapped job is ONE job ----------------------------
# `sh -c 'python -m utils.X ... >> log'` leaves the shell and its python both
# running and both matching. Counting two is wrong in the way this survey
# exists to prevent, and the marker sits on the python PID - so the wrapper
# would report `unknown` while its own child reports `resumable`, and with
# deferring on that alone would hold a deploy for a job that was safe to kill.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight_wrapped "python -m utils.backfill_pool --snapshot data/p.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

# "in flight (" is the per-job prefix; the group header says "is in flight
# inside <container>:" and must not be counted as a job.
reported="$(grep -c "in flight (" "${WORK}/watcher.log" || true)"
if [ "$reported" != "1" ]; then
    fail "scenario 8 reported ${reported} jobs in flight; one process tree is one job"
fi
grep -q "in flight (resumable): python -m utils.backfill_pool" "${WORK}/watcher.log" \
    || fail "scenario 8 kept the sh -c wrapper instead of the python doing the work"
if grep -q "in flight (no marker" "${WORK}/watcher.log"; then
    fail "scenario 8 still reports the wrapper, whose PID carries no marker"
fi
built || fail "scenario 8 deferred for a wrapper whose child is resumable"
printf 'OK: a shell-wrapped job counts once, and its marker is still found\n'

# --- scenario 9: a real parent/child pair is TWO jobs, not one -------------
# `utils.coordinator` spawning `utils.worker` is not a shell wrapper. Dropping
# the parent because something names it as PPID would take the non-resumable
# half out of the count and deploy straight over it.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 100 1 0 12:00 ? 00:00:01 python -m utils.coordinator
appuser 101 100 0 12:00 ? 00:00:01 python -m utils.worker
ROWS
add_marker coordinator '{"module":"coordinator","argv":[],"resumable":false}'
add_marker worker '{"module":"worker","argv":[],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (NOT resumable[^)]*): python -m utils.coordinator" "${WORK}/watcher.log" \
    || fail "scenario 9 lost the coordinator - a real job was dropped as if it were a wrapper"
grep -q "in flight (resumable): python -m utils.worker" "${WORK}/watcher.log" \
    || fail "scenario 9 lost the worker"
built && fail "scenario 9 deployed over a non-resumable coordinator"
grep -q "deferring this tick" "${WORK}/watcher.log" \
    || fail "scenario 9 did not defer although a non-resumable job is in flight"
printf 'OK: a genuine parent/child pair stays two jobs; only shell wrappers collapse\n'

# --- scenario 10: an unreadable process list is UNKNOWN, not empty ---------
# `docker top` exiting non-zero used to become an empty pipeline, which reads
# as "nothing is running" and deploys in silence - the very defect this survey
# exists to prevent, reproduced inside the survey.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/p.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true}'
DOCKER_TOP_RC=1 DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "UNKNOWN, not empty" "${WORK}/watcher.log" \
    || fail "scenario 10 treated an unreadable process list as 'nothing running'"
built && fail "scenario 10 deployed silently while the process list was unknown"
grep -q "deferring this tick" "${WORK}/watcher.log" \
    || fail "scenario 10 did not defer on an unknown process list"
printf 'OK: a docker top that fails blocks like an unmarked job, and says so\n'

# --- scenario 11: argv separates two concurrent runs of one module ---------
# Both rows are the same module; only --snapshot tells them apart. The marker
# carrying the other snapshot must not vouch for this process.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 200 1 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/aaa.json
ROWS
add_marker other '{"module":"backfill_pool","argv":["--snapshot","data/bbb.json"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 11 let a marker for a different --snapshot vouch for this run"
printf 'OK: a marker only vouches for the argv it recorded\n'

# --- scenario 12: a module name inside a path is not a module --------------
# `utils.backfill_pool` occurs inside `--snapshot data/utils.backfill_pool.json`.
# A substring test let that marker vouch for a different module entirely.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 300 1 0 12:00 ? 00:00:01 python -m utils.coordinator --snapshot data/utils.backfill_pool.json
ROWS
add_marker stale '{"module":"backfill_pool","argv":[],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 12 matched a marker on a module name that only appears inside a path"
built && fail "scenario 12 deployed over an unmarked job on the strength of a foreign marker"
printf 'OK: the module must be the -m argument, not a substring of some path\n'

# --- scenario 13: argv tokens are tokens, not substrings -------------------
# A stale marker recording `data/a` must not vouch for a live `data/aaa.json`.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 301 1 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/aaa.json
ROWS
add_marker stale '{"module":"backfill_pool","argv":["--snapshot","data/a"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 13 let a stale marker vouch because its argv was a substring"
printf 'OK: an argv token must match a whole token, not a prefix\n'

# --- scenario 14: a header with no rows is unknown, not empty --------------
# The container always runs gunicorn, so an empty table means the probe
# failed. Reading it as "no processes" is fail-open in a quieter costume.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
printf 'UID PID PPID C STIME TTY TIME CMD\n' >"$TOP_FILE"
rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "UNKNOWN, not empty" "${WORK}/watcher.log" \
    || fail "scenario 14 read a header-only process list as 'nothing running'"
built && fail "scenario 14 deployed on a process list that proved nothing"
printf 'OK: a process table with no rows at all is a failed probe, not an idle container\n'

# --- scenario 15: shell wrappers spelled other ways ------------------------
# `/bin/bash --login -c` and `sh -cx` are wrappers too; missing them reports
# one job twice.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 400 1 0 12:00 ? 00:00:01 /bin/bash --login -c python -m utils.backfill_pool --snapshot data/q.json
appuser 401 400 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/q.json
ROWS
add_marker job '{"module":"backfill_pool","argv":["--snapshot","data/q.json"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

reported="$(grep -c "in flight (" "${WORK}/watcher.log" || true)"
if [ "$reported" != "1" ]; then
    fail "scenario 15 reported ${reported} jobs for one '/bin/bash --login -c' tree"
fi
grep -q "in flight (resumable): python -m utils.backfill_pool" "${WORK}/watcher.log" \
    || fail "scenario 15 kept the bash wrapper instead of the python child"
printf 'OK: --login and clustered short options still read as a shell wrapper\n'

# --- scenario 16: an empty argv is not a wildcard --------------------------
# `bulk_ai_analysis` with no args is resumable; with --force it is not. A
# stale no-args marker vouching for a live --force run is the worst shape of
# this bug: it calls the one genuinely unsafe run safe.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 500 1 0 12:00 ? 00:00:01 python -m utils.bulk_ai_analysis --force
ROWS
add_marker stale '{"module":"bulk_ai_analysis","argv":[],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 16 let a stale empty-argv marker vouch for a --force run"
built && fail "scenario 16 deployed over a --force run believing it resumable"
printf 'OK: an empty recorded argv vouches only for a run that also had none\n'

# --- scenario 17: argv order is part of the identity -----------------------
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 501 1 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/b.json --days 30
ROWS
add_marker stale '{"module":"backfill_pool","argv":["--days","30","--snapshot","data/b.json"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 17 matched a marker whose argv was the same tokens in a different order"
printf 'OK: argv is compared as a sequence, not as a bag of tokens\n'

# --- scenario 18: a path that looks like a script is not the program -------
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 502 1 0 12:00 ? 00:00:01 python -m utils.coordinator --snapshot data/utils/backfill_pool.py
ROWS
add_marker stale '{"module":"backfill_pool","argv":["--snapshot","data/utils/backfill_pool.py"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 18 took an argument path ending in /utils/<module>.py for the program"
printf 'OK: the program is the -m token or the first .py token, never an argument\n'

# --- scenario 19: garbage after the header is not a process table ----------
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
{ printf 'UID PID PPID C STIME TTY TIME CMD\n'; printf 'garbage\n'; } >"$TOP_FILE"
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "UNKNOWN, not empty" "${WORK}/watcher.log" \
    || fail "scenario 19 accepted a line of garbage as a process row"
built && fail "scenario 19 deployed on an unparseable process table"
printf 'OK: a row must look like ps output before it counts as a process list\n'

# --- scenario 20: well-shaped junk is still not a process list -------------
# Eight columns and a numeric PID are satisfiable by garbage. What is not
# satisfiable is the app's own server: this container cannot be running
# without gunicorn, so a table that omits it did not look at the container.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
{
    printf 'UID PID PPID C STIME TTY TIME CMD\n'
    printf 'junk 123 junk junk junk junk junk junk\n'
} >"$TOP_FILE"
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "UNKNOWN, not empty" "${WORK}/watcher.log" \
    || fail "scenario 20 accepted a well-shaped junk row as a real process list"
built && fail "scenario 20 deployed on a process table that never saw the container"
printf 'OK: a process list without the app server is a probe that did not look\n'

# --- scenario 6: healthz green, page broken ---------------------------------
# The 15-minute incident: the route turns a TemplateSyntaxError into a redirect
# and healthz renders no template, so only a real page fetch sees it.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
printf '302\n' >"$PAGE_STATUS_FILE"
HEALTH_TIMEOUT_OVERRIDE=6 run_watcher
printf '200\n' >"$PAGE_STATUS_FILE"

grep -q "answered 302" "${WORK}/watcher.log" \
    || fail "scenario 6 accepted a redirecting page as a healthy deploy"
grep -q "ROLLBACK" "${WORK}/watcher.log" \
    || fail "scenario 6 saw the broken page but did not roll back"
if [ -e "$MARKER" ]; then
    fail "scenario 6 recorded a deployment whose page never rendered"
fi
printf 'OK: a green healthz over a redirecting page is not a healthy deploy\n'

# --- scenario 21: the page comes from the shared contract --------------------
# The rule lived under two names that had to move together (#292). It is one
# now - DEPLOY_RENDER_PATH in lib/render_check.sh - and this watcher reads it
# rather than carrying its own copy. The stub serves only the configured page
# and 404s everything else, so a watcher still hard-coding /properties fetches
# a 404 and rolls back instead of deploying.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
printf '/dashboard\n' >"$PAGE_PATH_FILE"
RENDER_PATH_OVERRIDE=/dashboard HEALTH_TIMEOUT_OVERRIDE=6 run_watcher
printf '/properties\n' >"$PAGE_PATH_FILE"

built || fail "scenario 21 did not deploy - the watcher ignored DEPLOY_RENDER_PATH"
grep -q "/dashboard rendered (200)" "${WORK}/watcher.log" \
    || fail "scenario 21 verified some other page than the one the contract names"
printf 'OK: the page checked is the one the shared contract names\n'

# --- scenario 22: turning the check off says so -----------------------------
# Allowed, and never silent: a build nobody rendered a page for must not read
# like one that was verified.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
printf '302\n' >"$PAGE_STATUS_FILE"
RENDER_PATH_OVERRIDE="" run_watcher
printf '200\n' >"$PAGE_STATUS_FILE"

built || fail "scenario 22 did not deploy although the page check is off"
grep -q "no page was rendered" "${WORK}/watcher.log" \
    || fail "scenario 22 skipped the page check without saying so"
printf 'OK: a skipped page check is reported, not assumed to have passed\n'

# --- scenario 23: the retired name is named, never obeyed -------------------
# AUTOPILOT_PAGE_URL="" used to be how this watcher's page check was switched
# off. It is not read any more - the page is checked - and an environment that
# still carries it is told rather than quietly overruled.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
LEGACY_PAGE_URL_OVERRIDE="" run_watcher

built || fail "scenario 23 did not deploy"
grep -q "AUTOPILOT_PAGE_URL is set but no longer read" "${WORK}/watcher.log" \
    || fail "scenario 23 dropped the retired name without a word"
grep -q "rendered (200)" "${WORK}/watcher.log" \
    || fail "scenario 23 obeyed the retired name and skipped the page check"
printf 'OK: a retired page-check name is reported, and does not switch the check off\n'

# --- scenario 24: a contract that did not load stops the tick ---------------
# Present is not loaded, and loaded is not complete. An empty or half-written
# render_check.sh is readable and sources without error, defining nothing - and
# a page check that cannot run must never read as one that passed, so the tick
# has to stop and say why rather than deploy with the check silently off.
for shape in empty truncated unparseable; do
    case "$shape" in
        empty) contract="" ;;
        # Parses, defines none of the functions: a neighbour mid-write.
        truncated) contract=': "${DEPLOY_RENDER_PATH=/properties}"'$'\n' ;;
        unparseable) contract='deploy_render_url() {'$'\n' ;;
    esac
    : >"${WORK}/watcher.log"
    rm -f "$DEFER_STATE"
    set_inflight ""
    WATCHER_UNDER_TEST="$(watcher_with_contract "$contract")" run_watcher

    built && fail "scenario 24 (${shape}) deployed with a contract that never loaded"
    grep -q "FATAL" "${WORK}/watcher.log" \
        || fail "scenario 24 (${shape}) stopped without saying why"
    grep -q "render_check.sh" "${WORK}/watcher.log" \
        || fail "scenario 24 (${shape}) blamed something other than the contract"
done
printf 'OK: a contract that did not load stops the deploy and names itself\n'
# --- scenario 25: an unknown list deploying is not "0 jobs killed" ----------
# Scenario 10 with deferring off. The tick correctly deploys - that is the
# documented default - but the sentence it leaves behind decides what an
# operator reading data/autopilot-deploy.log believes happened. Counting the
# jobs here reports the failed probe's zero as an observation, which is the
# survey's own fail-open defect surviving in the log after being fixed in the
# logic.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/p.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true}'
DOCKER_TOP_RC=1 DEFER_ON_INFLIGHT=0 run_watcher

built || fail "scenario 25 refused to deploy although deferring is off"
grep -q "0 job(s) above will be killed" "${WORK}/watcher.log" \
    && fail "scenario 25 reported zero jobs killed for a process list it could not read"
grep -q "what this kills is UNKNOWN" "${WORK}/watcher.log" \
    || fail "scenario 25 deployed over an unreadable process list without saying so"
printf 'OK: deploying over an unknown process list does not claim it killed nothing\n'

# --- scenario 26: python's joined -m form is a running job ------------------
# `python3 -mutils.backfill_pool` is what python accepts, not a typo, and the
# survey's default pattern required a space after -m. A job the pattern does
# not match is not reported as unknown - it is not reported at all, and the
# deploy kills it in silence. That is the one outcome this survey exists to
# remove, so it is the one worth a scenario of its own.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python3 -mutils.backfill_pool --snapshot data/p.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/p.json"],"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (" "${WORK}/watcher.log" \
    || fail "scenario 26 did not see a job launched with python's joined -m form"
grep -q "in flight (resumable): python3 -mutils.backfill_pool" "${WORK}/watcher.log" \
    || fail "scenario 26 saw the job but could not read its marker"
printf 'OK: the joined -mMODULE form is seen, and its marker is found\n'

# --- scenario 27: an argument containing a space still matches -------------
# `docker top` returns one whitespace-joined line and the shell's quoting is
# gone by then, so a marker recording ["--snapshot", "data/My Pool.json"]
# faced four tokens against its two. Comparing token lists missed the live
# job's OWN marker and called it unknown - a false unknown holds a deploy for
# a job that had already said it was safe to kill.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/My Pool.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/My Pool.json"],"resumable":true,"ledger":"data/My Pool.json"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

if grep -q "in flight (no marker" "${WORK}/watcher.log"; then
    fail "scenario 27 missed the marker of a job whose argument contains a space"
fi
grep -q "in flight (resumable): python -m utils.backfill_pool --snapshot data/My Pool.json" \
    "${WORK}/watcher.log" \
    || fail "scenario 27 did not resolve the spaced argument to its own marker"
built || fail "scenario 27 deferred for a job that reported itself resumable"
printf 'OK: an argument containing a space resolves to its own marker\n'

# --- scenario 28: an option with an operand does not hide the -c -----------
# `bash -o pipefail -c ...` puts a bare word in the middle of the option run.
# A scan that stops at the first non-option never reaches the -c, so the shell
# is kept alongside its child and one job is reported twice - and the extra
# row carries no marker, so it reports `unknown` and can hold a deploy on its
# own.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_rows <<'ROWS'
appuser 500 1 0 12:00 ? 00:00:01 /bin/bash -o pipefail -c python -m utils.backfill_pool --snapshot data/r.json
appuser 501 500 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/r.json
ROWS
add_marker job '{"module":"backfill_pool","argv":["--snapshot","data/r.json"],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

reported="$(grep -c "in flight (" "${WORK}/watcher.log" || true)"
if [ "$reported" != "1" ]; then
    fail "scenario 28 reported ${reported} jobs for one 'bash -o pipefail -c' tree"
fi
if grep -q "in flight (no marker" "${WORK}/watcher.log"; then
    fail "scenario 28 kept the wrapper, which carries no marker"
fi
built || fail "scenario 28 deferred for a wrapper whose child is resumable"
printf 'OK: an option taking an operand does not hide the -c that marks a wrapper\n'

# --- scenario 29: the column layout comes from the header ------------------
# `docker top` renders whatever ps format it is handed; only the default one
# puts the command at field 8. A parse that blanks fields 1-7 positionally
# mangles any other layout, and a mangled command matches the pattern no more
# - so the job does not become "unknown", it disappears, which is the one
# outcome this survey exists to remove. The header names the columns; read it.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_table <<'TABLE'
USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND
appuser 7 0.1 1.2 40000 20000 ? Ss 12:00 00:00:03 /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 main:app
appuser 4711 0.0 2.0 50000 30000 ? S 12:00 00:00:01 python -m utils.backfill_pool --snapshot data/s.json
TABLE
add_marker job '{"module":"backfill_pool","argv":["--snapshot","data/s.json"],"resumable":true,"ledger":"data/s.json"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (" "${WORK}/watcher.log" \
    || fail "scenario 29 lost the job when the process table used another layout"
grep -q "in flight (resumable): python -m utils.backfill_pool --snapshot data/s.json" \
    "${WORK}/watcher.log" \
    || fail "scenario 29 mis-split the command, so its marker did not match"
printf 'OK: the command column is found from the header, not assumed to be the eighth\n'

# --- scenario 30: a row the header cannot describe is unknown --------------
# The check used to ask for eight fields and a numeric PID while the parse
# assumed the command began at field 8 - two descriptions of one table, so a
# row could satisfy the check and still be split wrongly. One row that the
# header cannot describe now makes the whole table unknown, because a row that
# cannot be read is not a row with nothing in it.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_top_table <<'TABLE'
UID PID PPID C STIME TTY TIME CMD
appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 main:app
appuser 4711 7 0 12:00
TABLE
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "UNKNOWN, not empty" "${WORK}/watcher.log" \
    || fail "scenario 30 read a table it could not parse as 'nothing running'"
built && fail "scenario 30 deployed over a process list it could not read"
printf 'OK: a row the header cannot describe makes the table unknown, not empty\n'

# --- scenario 31: `-um` is one cluster, not an unknown option --------------
# `python -um utils.backfill_pool` is how a background job that writes to a
# log is ordinarily started, and it is the third spelling of this command to
# defeat an attempt to anchor on a literal form - after `-m utils.x` and
# `-mutils.x`. The failure was the worst kind each time: the pattern did not
# match, so the job was not reported as unknown, it was not reported at all.
# The session shipping #315/#319 hit the identical class in its own supervisor
# the same day; this scenario exists so the next form is a test failure rather
# than a silent kill.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -um utils.backfill_pool --snapshot data/u.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/u.json"],"resumable":true,"ledger":"data/u.json"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (" "${WORK}/watcher.log" \
    || fail "scenario 31 did not see a job started with the clustered -um form"
grep -q "in flight (resumable): python -um utils.backfill_pool" "${WORK}/watcher.log" \
    || fail "scenario 31 saw the job but read -um as something other than -u -m"
printf 'OK: a clustered -um is read as python reads it, and its marker is found\n'

# --- scenario 32: a damaged marker is rejected, never normalised -----------
# `argv` coerced to [] when it was missing or malformed gave a corrupt marker
# the identity of a job that takes no arguments - so a damaged file claiming
# `resumable: true` vouched for a live job and the deploy ran over it. Every
# other guard in this reader rejects what it cannot read; this one invented a
# claim out of the damage.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_sea_view" \
    '{"module":"backfill_sea_view","argv":"invalid","resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 32 believed a marker whose argv was not a list"
built && fail "scenario 32 deployed on a claim it read out of a damaged marker"
printf 'OK: a marker whose argv is not a list is rejected, not read as empty\n'

# --- scenario 33: an empty argument is not "no arguments" ------------------
# [""] and [] both render to the empty string, so a stale marker recording one
# empty argument could vouch for a live job that takes none. The ambiguity is
# real and cannot be resolved from a whitespace-joined process table, so it
# resolves to unknown rather than in the deploy's favour.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_sea_view" \
    '{"module":"backfill_sea_view","argv":[""],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 33 let a marker with an empty argument vouch for a job with none"
built && fail "scenario 33 deployed over a job whose resumability was never established"
printf 'OK: an empty recorded argument does not pass for no arguments at all\n'

# --- scenario 34: a tab inside an argument still finds its marker ----------
# The process table renders one whitespace-joined line, so a tab the marker
# recorded arrives as a space. Comparing raw strings made the job miss its own
# marker and spend the deferral budget as unknown - fail-closed, but wrong,
# and wrong in the direction that holds deploys for jobs that are fine.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python -m utils.backfill_pool --snapshot data/My Pool.json" \
    '{"module":"backfill_pool","argv":["--snapshot","data/My\tPool.json"],"resumable":true,"ledger":"data/My Pool.json"}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

if grep -q "in flight (no marker" "${WORK}/watcher.log"; then
    fail "scenario 34 missed the marker because it recorded a tab the table cannot show"
fi
built || fail "scenario 34 deferred for a job that reported itself resumable"
printf 'OK: whitespace inside an argument is normalised on both sides, not just one\n'

# --- scenario 35: the first non-option token is the program ----------------
# Python stops parsing its own options at the first token that is not one, and
# runs it - extension or not. Treating only `.py` as a script meant
# `python worker -m utils.X` read as running utils.X, so a marker for utils.X
# vouched for a process that was running something else entirely.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight "python worker -m utils.backfill_sea_view" \
    '{"module":"backfill_sea_view","argv":[],"resumable":true}'
DEFER_ON_INFLIGHT=1 DEFER_BUDGET=2 run_watcher

grep -q "in flight (no marker" "${WORK}/watcher.log" \
    || fail "scenario 35 read the -m argument as the program although a script preceded it"
built && fail "scenario 35 deployed on a marker that belonged to a different program"
printf 'OK: a script without a .py suffix still ends option parsing\n'
