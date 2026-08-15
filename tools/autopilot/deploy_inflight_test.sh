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
        printf 'appuser 4711 4700 0 12:00 ? 00:00:01 %s\n' "$command"
    } >"$TOP_FILE"
    if [ -n "$marker" ]; then
        printf '%s\n' "$marker" >"${INFLIGHT_DIR}/job.4711.json"
    fi
}

set_inflight_wrapped() {
    # The shape `docker exec ... sh -c 'python -m utils.X ... >> log'` really
    # produces, measured on the mini 2026-08-14: the shell and the python it
    # exec'd, both matching, the marker written by the python one.
    # set_inflight_wrapped <inner-command> [marker-json]
    local command="$1" marker="${2:-}"
    rm -f "${INFLIGHT_DIR}"/*.json 2>/dev/null || true
    {
        printf 'UID PID PPID C STIME TTY TIME CMD\n'
        printf 'appuser 4711 4700 0 12:00 ? 00:00:01 sh -c %s >> /app/data/run.log 2>&1\n' "$command"
        printf 'appuser 4713 4711 0 12:00 ? 00:00:01 %s\n' "$command"
    } >"$TOP_FILE"
    if [ -n "$marker" ]; then
        printf '%s\n' "$marker" >"${INFLIGHT_DIR}/job.4713.json"
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
    '{"module":"backfill_pool","pid":4711,"resumable":true,"ledger":"data/pool_backfill.json.ledger.jsonl"}'
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
    '{"module":"recalc_property_travel","pid":4711,"resumable":false}'

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
    '{"module":"backfill_pool","pid":4711,"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
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
    '{"module":"backfill_pool","pid":4711,"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
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
    '{"module":"backfill_pool","pid":4713,"resumable":true,"ledger":"data/p.json.ledger.jsonl"}'
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

# --- scenario 9: the page comes from the shared contract --------------------
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

built || fail "scenario 9 did not deploy - the watcher ignored DEPLOY_RENDER_PATH"
grep -q "/dashboard rendered (200)" "${WORK}/watcher.log" \
    || fail "scenario 9 verified some other page than the one the contract names"
printf 'OK: the page checked is the one the shared contract names\n'

# --- scenario 10: turning the check off says so -----------------------------
# Allowed, and never silent: a build nobody rendered a page for must not read
# like one that was verified.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
printf '302\n' >"$PAGE_STATUS_FILE"
RENDER_PATH_OVERRIDE="" run_watcher
printf '200\n' >"$PAGE_STATUS_FILE"

built || fail "scenario 10 did not deploy although the page check is off"
grep -q "no page was rendered" "${WORK}/watcher.log" \
    || fail "scenario 10 skipped the page check without saying so"
printf 'OK: a skipped page check is reported, not assumed to have passed\n'

# --- scenario 11: the retired name is named, never obeyed -------------------
# AUTOPILOT_PAGE_URL="" used to be how this watcher's page check was switched
# off. It is not read any more - the page is checked - and an environment that
# still carries it is told rather than quietly overruled.
: >"${WORK}/watcher.log"
rm -f "$DEFER_STATE"
set_inflight ""
LEGACY_PAGE_URL_OVERRIDE="" run_watcher

built || fail "scenario 11 did not deploy"
grep -q "AUTOPILOT_PAGE_URL is set but no longer read" "${WORK}/watcher.log" \
    || fail "scenario 11 dropped the retired name without a word"
grep -q "rendered (200)" "${WORK}/watcher.log" \
    || fail "scenario 11 obeyed the retired name and skipped the page check"
printf 'OK: a retired page-check name is reported, and does not switch the check off\n'

# --- scenario 12: a contract that did not load stops the tick ---------------
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

    built && fail "scenario 12 (${shape}) deployed with a contract that never loaded"
    grep -q "FATAL" "${WORK}/watcher.log" \
        || fail "scenario 12 (${shape}) stopped without saying why"
    grep -q "render_check.sh" "${WORK}/watcher.log" \
        || fail "scenario 12 (${shape}) blamed something other than the contract"
done
printf 'OK: a contract that did not load stops the deploy and names itself\n'
