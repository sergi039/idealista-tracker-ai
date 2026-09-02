#!/bin/bash
# Production quietly stopped receiving deploys, and nothing said so (#532).
#
# On 2026-09-01 the mini's checkout sat on branch codex/issue-473 with five
# uncommitted files from 07:43 to 16:03. deploy_watcher.sh refused every tick -
# correctly: the refusal is what keeps it off another session's work - while
# two merged commits never reached production. healthz was green (the old
# image was healthy), the page check passed (the old page rendered), and the
# gap was found by accident, in the log, eight hours later.
#
# The watcher must keep refusing. What it must also do is count the ticks it
# refuses while origin/main is ahead of what serves, from a threshold say
# STALLED once per tick and leave data/.deploy_stalled for anyone who reads
# files rather than logs - and never, on any path here, deploy, stash, switch
# branches or reset a tree. An alarm that leads to an automatic override would
# be a new incident, so every scenario asserts what did NOT happen as well.
#
# Drives the real deploy_watcher.sh against a throwaway repository and bare
# remote, a stub docker that records what it was asked, and an HTTP stub that
# answers healthz and renders a page.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WATCHER="${SCRIPT_DIR}/deploy_watcher.sh"

# The interpreter that runs the watcher under test: /bin/bash is what the
# LaunchAgent execs (3.2.57 on the owner's Macs), and the wrapper runs this
# suite under the PATH bash as well, for the reason deploy_self_update_test.sh
# records.
WATCHER_BASH="${WATCHER_BASH:-/bin/bash}"
[ -x "$WATCHER_BASH" ] || { printf 'no interpreter at %s\n' "$WATCHER_BASH" >&2; exit 1; }

WORK="$(mktemp -d)"
HEALTH_PID=""
cleanup() {
    # Keep the verdict: reaping the stub yields 143, and letting that be the
    # trap's result would report a passing run as a SIGTERM failure.
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
    printf '%s\n' "--- stall counter / marker ---" >&2
    cat "${WORK}/stall_ticks" >&2 2>/dev/null || true
    cat "${WORK}/deploy_stalled" >&2 2>/dev/null || true
    exit 1
}

# --- a repository with a remote that can move on its own -------------------
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
BASE_SHA="$(git rev-parse HEAD)"

MARKER="${WORK}/deployed_sha"
STALL_STATE="${WORK}/stall_ticks"
STALLED="${WORK}/deploy_stalled"
INFLIGHT_DIR="${WORK}/inflight"
DEFER_STATE="${WORK}/deferrals"
TOP_FILE="${WORK}/docker-top.txt"
mkdir -p "$INFLIGHT_DIR"
: >"$TOP_FILE"

# --- a docker that answers, and records what it was asked -------------------
STUB_BIN="${WORK}/bin"
mkdir -p "$STUB_BIN"
cat >"${STUB_BIN}/docker" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$1" in
    ps) printf '%s\n' "${DOCKER_PS_OUTPUT:-}" ;;
    top) cat "${DOCKER_TOP_FILE:-/dev/null}" 2>/dev/null || true ;;
    # The container's main pid, which the in-flight survey corroborates its
    # process table against; every table below gives the app server pid 7.
    inspect) printf '7\n' ;;
esac
exit 0
STUB
chmod +x "${STUB_BIN}/docker"

# --- an app that answers healthz and renders a page -------------------------
HEALTH_PORT=""
for candidate in $(seq 46001 46049); do
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
        if self.path.startswith("/properties"):
            body = b"<html>properties</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"ok":true,"checks":{"database":"ok"},"scheduler":"running"}'
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
    curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS --max-time 1 "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1 \
    || fail "health stub never came up"

# --- helpers ----------------------------------------------------------------
WATCHER_RC=0
run_watcher() {
    : >"${WORK}/docker.log"
    set +e
    (
    # Only when a scenario asks for it: the default threshold is the watcher's
    # own, and a scenario that passed 3 explicitly could not notice the
    # default changing.
    [ -z "${STALL_THRESHOLD+set}" ] || export AUTOPILOT_STALL_THRESHOLD="$STALL_THRESHOLD"
    PATH="${STUB_BIN}:${PATH}" \
    DOCKER_LOG="${WORK}/docker.log" \
    DOCKER_TOP_FILE="$TOP_FILE" \
    DOCKER_PS_OUTPUT="${PS_OUTPUT:-}" \
    AUTOPILOT_REPO_DIR="$REPO" \
    AUTOPILOT_DEPLOYED_MARKER="$MARKER" \
    AUTOPILOT_LOG_FILE="${WORK}/watcher.log" \
    AUTOPILOT_LOCK_DIR="${WORK}/lock.d" \
    AUTOPILOT_HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/healthz" \
    AUTOPILOT_HEALTH_TIMEOUT=15 \
    AUTOPILOT_COMPOSE_FILE="docker-compose.yml" \
    AUTOPILOT_INFLIGHT_DIR="$INFLIGHT_DIR" \
    AUTOPILOT_DEFER_STATE="$DEFER_STATE" \
    AUTOPILOT_DEFER_ON_INFLIGHT="${DEFER_ON_INFLIGHT:-0}" \
    AUTOPILOT_STALL_STATE="$STALL_STATE" \
    AUTOPILOT_STALLED_MARKER="$STALLED" \
        "$WATCHER_BASH" "$WATCHER" >/dev/null 2>&1
    )
    WATCHER_RC=$?
    set -e
}

builds() {
    grep -c -- "up -d --build" "${WORK}/docker.log" || true
}

logged() {
    grep -q -- "$1" "${WORK}/watcher.log"
}

stalled_lines() {
    grep -c -- "STALLED:" "${WORK}/watcher.log" || true
}

# The counter's value, or "none" when there is no counter - the two are
# different facts and a scenario asks about both.
stall_count() {
    if [ -e "$STALL_STATE" ]; then
        cut -d' ' -f1 "$STALL_STATE"
    else
        printf 'none'
    fi
}

head_sha() {
    git -C "$REPO" rev-parse HEAD
}

branch_now() {
    git -C "$REPO" rev-parse --abbrev-ref HEAD
}

# Move the REMOTE's main one commit ahead WITHOUT moving this clone's
# refs/remotes/origin/main - the shape of the incident, where other sessions'
# fetches were the only thing keeping origin/main fresh in the mini's clone.
# The commit is built detached, its objects reach the remote through a parked
# ref, and then the remote's own main is pointed at it. Needs a clean tree,
# and returns to the branch it was called from.
NEW_SHA=""
advance_remote() {
    local message="$1" from
    from="$(branch_now)"
    git -C "$REPO" checkout --quiet --detach "$(git -C "$REMOTE" rev-parse main)"
    printf '%s\n' "$message" >>"${REPO}/file.txt"
    git -C "$REPO" commit --quiet -am "$message"
    NEW_SHA="$(git -C "$REPO" rev-parse HEAD)"
    git -C "$REPO" push --quiet origin "${NEW_SHA}:refs/heads/parked"
    git -C "$REMOTE" update-ref refs/heads/main "$NEW_SHA"
    git -C "$REPO" checkout --quiet "$from"
}

# --- scenario 1: the incident ----------------------------------------------
# main moved while the checkout sat on somebody's branch with uncommitted
# work, and origin/main in the clone is stale. Three refused ticks are the
# threshold; the fourth must not go quiet.
printf '%s\n' "$BASE_SHA" >"$MARKER"
advance_remote "merged while the checkout was elsewhere"
AHEAD_SHA="$NEW_SHA"
# A developer's earlier fetch of another ref, so FETCH_HEAD names it - what a
# plain fetch in the refusal path would silently rewrite (found by the plan
# review of this change).
git -C "$REPO" fetch --quiet origin parked
FETCH_HEAD_BEFORE="$(cat "${REPO}/.git/FETCH_HEAD")"
grep -q "parked" "${REPO}/.git/FETCH_HEAD" || fail "scenario 1 setup: FETCH_HEAD does not name the parked ref"
[ "$(git -C "$REPO" rev-parse origin/main)" = "$BASE_SHA" ] \
    || fail "scenario 1 setup: origin/main moved in the clone before any tick ran"
git -C "$REPO" checkout --quiet -b codex/issue-473
printf 'uncommitted\n' >>"${REPO}/file.txt"
: >"${WORK}/watcher.log"

run_watcher
logged "on branch 'codex/issue-473'" || fail "scenario 1 tick 1 did not refuse the foreign branch"
[ "$(stalled_lines)" = "0" ] || fail "scenario 1 alarmed on the first refused tick"
[ ! -e "$STALLED" ] || fail "scenario 1 wrote the stall marker before the threshold"
[ "$(stall_count)" = "1" ] || fail "scenario 1 counted '$(stall_count)' after one refused tick, not 1"
[ "$(git -C "$REPO" rev-parse origin/main)" = "$AHEAD_SHA" ] \
    || fail "scenario 1 refused without fetching, so it cannot know that main is ahead"

run_watcher
[ "$(stalled_lines)" = "0" ] || fail "scenario 1 alarmed on the second refused tick"
[ "$(stall_count)" = "2" ] || fail "scenario 1 counted '$(stall_count)' after two refused ticks, not 2"

run_watcher
[ "$(stalled_lines)" = "1" ] \
    || fail "scenario 1 logged $(stalled_lines) STALLED lines after three refused ticks, not exactly one"
# The line without log()'s "date time  " prefix, which the marker carries verbatim.
stalled_log_line="$(grep -- "STALLED:" "${WORK}/watcher.log" | sed 's/^[^ ]* [^ ]*  //')"
case "$stalled_log_line" in
    *"last reason: on branch 'codex/issue-473', not 'main'"*) ;;
    *) fail "scenario 1's STALLED line does not name the reason: ${stalled_log_line}" ;;
esac
case "$stalled_log_line" in
    *"deployed ${BASE_SHA:0:7}"*) ;;
    *) fail "scenario 1's STALLED line does not name the deployed sha: ${stalled_log_line}" ;;
esac
case "$stalled_log_line" in
    *"main ${AHEAD_SHA:0:7}"*) ;;
    *) fail "scenario 1's STALLED line does not name the remote's new main: ${stalled_log_line}" ;;
esac
case "$stalled_log_line" in
    *"1 commit(s) behind main"*) ;;
    *) fail "scenario 1's STALLED line does not carry the gap: ${stalled_log_line}" ;;
esac
case "$stalled_log_line" in
    *"3 refused tick(s) since 20"*) ;;
    *) fail "scenario 1's STALLED line does not say since when: ${stalled_log_line}" ;;
esac
[ -e "$STALLED" ] || fail "scenario 1 reached the threshold without writing the stall marker"
[ "$(head -n 1 "$STALLED")" = "$stalled_log_line" ] \
    || fail "scenario 1's marker does not open with the STALLED line the log carries"
grep -qx "deployed=${BASE_SHA}" "$STALLED" || fail "scenario 1's marker does not record the deployed sha in full"
grep -qx "main=${AHEAD_SHA}" "$STALLED" || fail "scenario 1's marker does not record main's sha in full"
grep -qx "gap=1" "$STALLED" || fail "scenario 1's marker does not record the gap"
grep -qx "ticks=3" "$STALLED" || fail "scenario 1's marker does not record the tick count"
grep -qx "since=$(cut -d' ' -f2 "$STALL_STATE")" "$STALLED" \
    || fail "scenario 1's marker and its counter disagree about since when"

run_watcher
[ "$(stalled_lines)" = "2" ] \
    || fail "scenario 1 went quiet after the threshold: $(stalled_lines) STALLED lines after four refused ticks, not two"
grep -qx "ticks=4" "$STALLED" || fail "scenario 1's marker was not rewritten on the fourth refused tick"

# What must NOT have happened, over all four ticks.
[ "$(builds)" = "0" ] || fail "scenario 1 built from a refused tick - the alarm deployed"
[ "$(branch_now)" = "codex/issue-473" ] || fail "scenario 1 switched branches: now on $(branch_now)"
[ "$(head_sha)" = "$BASE_SHA" ] || fail "scenario 1 moved HEAD off the foreign branch's commit"
grep -q "uncommitted" "${REPO}/file.txt" \
    || fail "scenario 1 lost the uncommitted change - something stashed or reset the tree"
[ "$(cat "${REPO}/.git/FETCH_HEAD")" = "$FETCH_HEAD_BEFORE" ] \
    || fail "scenario 1 rewrote FETCH_HEAD in the shared clone; a developer's pending merge would take main instead"
[ "$(cat "$MARKER")" = "$BASE_SHA" ] || fail "scenario 1 touched the deployment marker while refusing"
printf 'OK: three refused ticks with main ahead raise exactly one STALLED line and the marker; nothing was deployed, stashed or switched\n'

# --- scenario 2: refusing with nothing to deploy is not a stall -------------
rm -f "$STALL_STATE" "$STALLED"
git -C "$REPO" checkout --quiet -- file.txt
git -C "$REPO" checkout --quiet main
git -C "$REPO" branch --quiet -D codex/issue-473
git -C "$REPO" merge --quiet --ff-only "$AHEAD_SHA"
printf '%s\n' "$AHEAD_SHA" >"$MARKER"
git -C "$REPO" checkout --quiet -b codex/another
printf 'uncommitted\n' >>"${REPO}/file.txt"
: >"${WORK}/watcher.log"
run_watcher
run_watcher
run_watcher
run_watcher
logged "on branch 'codex/another'" || fail "scenario 2 did not refuse the foreign branch"
[ "$(stalled_lines)" = "0" ] || fail "scenario 2 alarmed although production was current"
[ ! -e "$STALLED" ] || fail "scenario 2 wrote a stall marker although production was current"
[ ! -e "$STALL_STATE" ] || fail "scenario 2 counted refused ticks although there was nothing to deploy"
[ "$(builds)" = "0" ] || fail "scenario 2 built from a refused tick"
printf 'OK: refusing with nothing to deploy counts nothing and alarms about nothing\n'

# --- scenario 3: a dirty main counts too, at the threshold the environment sets
git -C "$REPO" checkout --quiet -- file.txt
git -C "$REPO" checkout --quiet main
git -C "$REPO" branch --quiet -D codex/another
advance_remote "merged over a dirty checkout"
DIRTY_AHEAD_SHA="$NEW_SHA"
printf 'half-written\n' >>"${REPO}/file.txt"
: >"${WORK}/watcher.log"
STALL_THRESHOLD=2 run_watcher
logged "working tree is dirty" || fail "scenario 3 did not refuse the dirty tree"
[ "$(stalled_lines)" = "0" ] || fail "scenario 3 alarmed below its threshold of two"
STALL_THRESHOLD=2 run_watcher
[ "$(stalled_lines)" = "1" ] || fail "scenario 3 did not alarm at the threshold it was given"
logged "last reason: working tree is dirty" || fail "scenario 3's STALLED line does not name the dirty tree"
[ "$(builds)" = "0" ] || fail "scenario 3 built over uncommitted work"
grep -q "half-written" "${REPO}/file.txt" || fail "scenario 3 lost the uncommitted change"
printf 'OK: a dirty main counts with its own reason, at the threshold the environment sets\n'

# --- scenario 4: the next successful deploy clears it; counting restarts ----
git -C "$REPO" checkout --quiet -- file.txt
: >"${WORK}/watcher.log"
run_watcher
logged "DEPLOYED ${DIRTY_AHEAD_SHA:0:7} successfully" || fail "scenario 4 did not deploy once the tree was clean"
[ "$(builds)" = "1" ] || fail "scenario 4 ran $(builds) builds, not one"
logged "stall cleared (deployed ${DIRTY_AHEAD_SHA:0:7})" || fail "scenario 4 deployed without saying the stall was over"
[ ! -e "$STALLED" ] || fail "scenario 4 left the stall marker behind after a successful deploy"
[ ! -e "$STALL_STATE" ] || fail "scenario 4 left the refused-tick counter behind after a successful deploy"
[ "$(cat "$MARKER")" = "$DIRTY_AHEAD_SHA" ] || fail "scenario 4 did not record the deploy it made"

advance_remote "merged again"
git -C "$REPO" checkout --quiet -b codex/later
: >"${WORK}/watcher.log"
run_watcher
[ "$(stall_count)" = "1" ] \
    || fail "scenario 4's next stall started at '$(stall_count)', not 1 - consecutive means consecutive"
[ "$(stalled_lines)" = "0" ] || fail "scenario 4 alarmed on the first refused tick of a new stall"
[ "$(builds)" = "0" ] || fail "scenario 4 built from a refused tick"
printf 'OK: a successful deploy clears the alarm, and the next stall counts from one\n'

# --- scenario 5: a deferred tick is a tick that did not deploy --------------
# The issue names the in-flight deferral as a reason: bounded, but fifteen
# minutes of it is still fifteen minutes production spends behind main.
git -C "$REPO" checkout --quiet main
git -C "$REPO" branch --quiet -D codex/later
rm -f "$STALL_STATE" "$STALLED" "$DEFER_STATE"
LATER_SHA="$NEW_SHA"
{
    printf 'UID PID PPID C STIME TTY TIME CMD\n'
    printf 'appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 main:app\n'
    printf 'appuser 4711 4700 0 12:00 ? 00:00:01 python -m utils.recalc_property_travel\n'
} >"$TOP_FILE"
printf '%s\n' '{"module":"recalc_property_travel","pid":41,"resumable":false,"argv":[]}' \
    >"${INFLIGHT_DIR}/job.41.json"
: >"${WORK}/watcher.log"
PS_OUTPUT="idealista-app" DEFER_ON_INFLIGHT=1 STALL_THRESHOLD=2 run_watcher
logged "deferring this tick" || fail "scenario 5 did not defer to the job in flight"
[ "$(stall_count)" = "1" ] || fail "scenario 5 did not count the deferred tick"
[ "$(stalled_lines)" = "0" ] || fail "scenario 5 alarmed below the threshold"
PS_OUTPUT="idealista-app" DEFER_ON_INFLIGHT=1 STALL_THRESHOLD=2 run_watcher
[ "$(stalled_lines)" = "1" ] || fail "scenario 5 did not alarm after two deferred ticks"
logged "last reason: deferring this tick" || fail "scenario 5's STALLED line does not name the deferral"
[ "$(builds)" = "0" ] || fail "scenario 5 built over the job it deferred to"
printf 'OK: deferred ticks count, with the deferral as the reason\n'

# --- scenario 6: a marker written by hand says production is current -------
# The one way to "nothing new" without this watcher deploying is a person
# writing data/.deployed_sha, which is that person asserting production is
# current. The alarm ends; nothing is built.
: >"$TOP_FILE"
rm -f "${INFLIGHT_DIR}"/*.json
git -C "$REPO" merge --quiet --ff-only "$LATER_SHA"
printf '%s\n' "$LATER_SHA" >"$MARKER"
: >"${WORK}/watcher.log"
run_watcher
[ "$(builds)" = "0" ] || fail "scenario 6 rebuilt a commit the marker says is serving"
logged "stall cleared (production is current at ${LATER_SHA:0:7})" \
    || fail "scenario 6 found no gap and kept the alarm"
[ ! -e "$STALLED" ] || fail "scenario 6 left the stall marker although production is current"
[ ! -e "$STALL_STATE" ] || fail "scenario 6 left the counter although production is current"
printf 'OK: a tick that finds production current clears the alarm without deploying\n'

# --- scenario 7: a threshold that is not a number stops the tick loudly -----
rm -f "$DEFER_STATE"
advance_remote "merged during a misconfiguration"
: >"${WORK}/watcher.log"
STALL_THRESHOLD=abc run_watcher
logged "FATAL: AUTOPILOT_STALL_THRESHOLD must be a whole number" \
    || fail "scenario 7 accepted a threshold that is not a number"
[ "$WATCHER_RC" != "0" ] || fail "scenario 7 exited 0 on a misconfigured threshold"
[ "$(builds)" = "0" ] || fail "scenario 7 built under a misconfigured alarm"
[ ! -e "$STALL_STATE" ] || fail "scenario 7 counted a tick it could not judge"
printf 'OK: a threshold that is not a number is refused before anything is counted\n'

# --- scenario 8: no deployment marker means no measurable gap ---------------
# The deploy path already shouts about a missing marker and about one naming
# no commit here; the counter stays out of it rather than guess.
MISCONF_SHA="$NEW_SHA"
rm -f "$MARKER" "$STALL_STATE" "$STALLED"
git -C "$REPO" checkout --quiet -b codex/unmeasured
: >"${WORK}/watcher.log"
run_watcher
logged "on branch 'codex/unmeasured'" || fail "scenario 8 did not refuse the foreign branch"
[ ! -e "$STALL_STATE" ] || fail "scenario 8 counted a refused tick with no deployment marker to measure against"
[ "$(stalled_lines)" = "0" ] || fail "scenario 8 alarmed about a gap it cannot measure"
printf '%s\n' "0000000000000000000000000000000000000000" >"$MARKER"
run_watcher
[ ! -e "$STALL_STATE" ] || fail "scenario 8 counted against a marker naming no commit here"
[ "$(stalled_lines)" = "0" ] || fail "scenario 8 alarmed against a marker naming no commit here"
[ "$(builds)" = "0" ] || fail "scenario 8 built from a refused tick"
printf 'OK: a gap that cannot be measured is not counted, and not alarmed about\n'

# --- scenario 9: a measured gap of zero ends the count ----------------------
# The plan review's failing input, verbatim: one refused tick is counted; an
# operator deploys that commit by hand and writes the marker while the
# checkout stays on the branch; the next refused tick measures no gap. A count
# that survived that tick would alarm after ONE refused tick of the next
# stall, so it must not survive.
rm -f "$STALL_STATE" "$STALLED"
printf '%s\n' "$LATER_SHA" >"$MARKER"
: >"${WORK}/watcher.log"
STALL_THRESHOLD=2 run_watcher
[ "$(stall_count)" = "1" ] || fail "scenario 9 tick 1 did not count the refused tick"
printf '%s\n' "$MISCONF_SHA" >"$MARKER"
STALL_THRESHOLD=2 run_watcher
logged "on branch 'codex/unmeasured'" || fail "scenario 9 tick 2 did not refuse the foreign branch"
[ ! -e "$STALL_STATE" ] || fail "scenario 9 kept a count through a tick that measured no gap"
logged "stall cleared (production is current at ${MISCONF_SHA:0:7}" \
    || fail "scenario 9 tick 2 found no gap and did not say the stall was over"
[ "$(stalled_lines)" = "0" ] || fail "scenario 9 alarmed while production was current"
advance_remote "the next merge"
STALL_THRESHOLD=2 run_watcher
[ "$(stall_count)" = "1" ] \
    || fail "scenario 9 tick 3 counted '$(stall_count)' - a stale count carried into the new stall"
[ "$(stalled_lines)" = "0" ] || fail "scenario 9 alarmed after one refused tick of a new stall"
STALL_THRESHOLD=2 run_watcher
[ "$(stalled_lines)" = "1" ] || fail "scenario 9 tick 4 did not alarm at the threshold"
[ "$(builds)" = "0" ] || fail "scenario 9 built from a refused tick"
[ "$(branch_now)" = "codex/unmeasured" ] || fail "scenario 9 switched branches: now on $(branch_now)"
printf 'OK: a refused tick that measures no gap ends the count; the next stall starts from one\n'

# --- scenario 10: a counter with a leading zero still counts ----------------
# The range gate's failing input, verbatim: `08` passes the digits-only
# validation and then bash 3.2 -- the shell launchd execs -- reads it as an
# invalid octal literal, so `$((count + 1))` dies and the tick exits before
# counting or alarming. The alarm would be silent in exactly the stall it
# exists for. A counter can acquire that shape from a hand edit or a restore
# in this shared clone, so the value is normalised where it is validated.
rm -f "$STALLED"
printf '%s %s\n' "08" "2026-09-01T05:43:14Z" >"$STALL_STATE"
: >"${WORK}/watcher.log"
STALL_THRESHOLD=3 run_watcher
logged "on branch 'codex/unmeasured'" || fail "scenario 10 did not refuse the foreign branch"
[ "$(stall_count)" = "9" ] \
    || fail "scenario 10 counted '$(stall_count)' from a leading-zero counter, expected 9"
[ "$(stalled_lines)" = "1" ] || fail "scenario 10 did not alarm past the threshold"
logged "after 9 refused tick(s) since 2026-09-01T05:43:14Z" \
    || fail "scenario 10 lost the count or the since-when carried by the counter"
[ "$(builds)" = "0" ] || fail "scenario 10 built from a refused tick"
printf 'OK: a counter with a leading zero is read as base 10, counted and alarmed\n'
