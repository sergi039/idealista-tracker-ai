#!/bin/bash
# The watcher deploys its own source, so the version that governs a deploy has
# to be the version being deployed (#293).
#
# Observed live on 2026-08-14 16:33:30: the tick that rolled out #285 executed
# the *pre*-#285 deploy_watcher.sh, because bash had read the script from disk
# before that same tick's `git merge --ff-only` replaced it. So the deploy that
# shipped the in-flight survey and the page check ran with neither, and it
# killed a pool backfill at 32 ledger rows silently.
#
# This drives the real deploy_watcher.sh - copied into a throwaway repository,
# which is the only way the self-update path is reachable at all - against a
# stub docker and an HTTP stub. Each scenario gets its own repository and its
# own remote, so nothing has to be rewound between them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WATCHER_SOURCE="${SCRIPT_DIR}/deploy_watcher.sh"
LOCK_SOURCE="${SCRIPT_DIR}/lib/lock.sh"

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
    exit 1
}

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

MARKER="${WORK}/deployed_sha"
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
# A parallel `git fetch` in the same clone, fired from inside the tick: the
# rollback tag is taken after the watcher resolved origin/main and before it
# merges, which is exactly the window a shared checkout leaves open. Once only,
# so the handed-over process sees the ref the remote really has.
if [ -n "${ADVANCE_REF_TO:-}" ] && [ "$1" = "tag" ] && [ ! -e "$ADVANCE_ONCE" ]; then
    : >"$ADVANCE_ONCE"
    git -C "$ADVANCE_REPO" update-ref refs/remotes/origin/main "$ADVANCE_REF_TO"
fi
if [ "${DOCKER_FAIL_BUILD:-0}" = "1" ]; then
    for arg in "$@"; do
        if [ "$arg" = "--build" ]; then
            echo "stub docker: refusing to build" >&2
            exit 1
        fi
    done
fi
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

# --- an app that answers healthz and renders a page -------------------------
HEALTH_PORT=""
for candidate in $(seq 45951 45999); do
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

# --- a repository that contains the watcher ---------------------------------
# The existing harnesses run the real script against a repo that does not hold
# a copy of it, which is precisely why they never exercise this path: with the
# script outside the tree, there is nothing for a fast-forward to replace.
REPO=""
BASE_SHA=""
SCENARIO=0
fresh_repo() {
    SCENARIO=$((SCENARIO + 1))
    local remote="${WORK}/remote-${SCENARIO}.git"
    REPO="${WORK}/repo-${SCENARIO}"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$REPO"
    mkdir -p "${REPO}/tools/autopilot/lib"
    cp "$WATCHER_SOURCE" "${REPO}/tools/autopilot/deploy_watcher.sh"
    cp "$LOCK_SOURCE" "${REPO}/tools/autopilot/lib/lock.sh"
    chmod +x "${REPO}/tools/autopilot/deploy_watcher.sh"
    printf 'first\n' >"${REPO}/file.txt"
    git -C "$REPO" add -A
    git -C "$REPO" commit --quiet -m "first"
    git -C "$REPO" remote add origin "$remote"
    git -C "$REPO" push --quiet origin main
    git -C "$REPO" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true
    BASE_SHA="$(git -C "$REPO" rev-parse HEAD)"

    : >"${WORK}/watcher.log"
    : >"${WORK}/docker.log"
    rm -f "$DEFER_STATE"
    printf '%s\n' "0000000000000000000000000000000000000000" >"$MARKER"
}

# Commit whatever the scenario changed, publish it as the new main, and rewind
# the checkout - so origin/main is one commit ahead, exactly as a tick finds it.
publish() {
    local message="$1"
    git -C "$REPO" add -A
    git -C "$REPO" commit --quiet -m "$message"
    NEW_SHA="$(git -C "$REPO" rev-parse HEAD)"
    git -C "$REPO" push --quiet origin main
    git -C "$REPO" reset --hard --quiet "$BASE_SHA"
}

WATCHER_RC=0
run_watcher() {
    set +e
    PATH="${STUB_BIN}:${PATH}" \
    DOCKER_LOG="${WORK}/docker.log" \
    DOCKER_TOP_FILE="$TOP_FILE" \
    DOCKER_PS_OUTPUT="" \
    DOCKER_FAIL_BUILD="${FAIL_BUILD:-0}" \
    AUTOPILOT_REPO_DIR="$REPO" \
    AUTOPILOT_DEPLOYED_MARKER="$MARKER" \
    AUTOPILOT_LOG_FILE="${WORK}/watcher.log" \
    AUTOPILOT_LOCK_DIR="${WORK}/lock.d" \
    AUTOPILOT_HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/healthz" \
    AUTOPILOT_HEALTH_TIMEOUT="${HEALTH_TIMEOUT_OVERRIDE:-15}" \
    AUTOPILOT_COMPOSE_FILE="docker-compose.yml" \
    AUTOPILOT_INFLIGHT_DIR="$INFLIGHT_DIR" \
    AUTOPILOT_DEFER_STATE="$DEFER_STATE" \
    AUTOPILOT_SELF_UPDATE="${SELF_UPDATE:-1}" \
    ADVANCE_REF_TO="${ADVANCE_REF_TO:-}" \
    ADVANCE_REPO="$REPO" \
    ADVANCE_ONCE="${WORK}/advanced" \
        /bin/bash "${REPO}/tools/autopilot/deploy_watcher.sh" >/dev/null 2>&1
    WATCHER_RC=$?
    set -e
}

builds() {
    grep -c -- "up -d --build" "${WORK}/docker.log" || true
}

logged() {
    grep -q -- "$1" "${WORK}/watcher.log"
}

head_sha() {
    git -C "$REPO" rev-parse HEAD
}

# The mutation every "the new watcher spoke" assertion hangs on: a line the new
# script logs while building, which the old one cannot possibly print.
mark_new_watcher() {
    local target="${REPO}/tools/autopilot/deploy_watcher.sh"
    perl -0pi -e 's/log "building\.\.\."/log "building... NEW-WATCHER-SPEAKING"/' "$target"
    grep -q 'NEW-WATCHER-SPEAKING' "$target" || fail "could not mark the new watcher"
}

# --- scenario 1: the new watcher runs the deploy ----------------------------
# The #293 defect, directly: the deploy has to be governed by the script that
# main brought, not the one the tick started with.
fresh_repo
mark_new_watcher
publish "watcher change"
run_watcher

logged "changes this watcher itself" \
    || fail "scenario 1 never noticed that main changes the watcher"
logged "handing this tick over" \
    || fail "scenario 1 did not hand the tick over to the new watcher"
logged "building\.\.\. NEW-WATCHER-SPEAKING" \
    || fail "scenario 1 built under the old watcher - the #293 defect"
# The handed-over process must keep the flock it was given on fd 9 rather than
# release and re-take it. Re-taking is not caught by "did the tick survive" -
# measured, it succeeds, because `exec 9>file` closes the old description
# before locking the new one. That close IS the defect: it drops the lock for
# the length of a fork and an exec, and another tick that fires in that window
# starts a second concurrent build. So what is asserted here is the branch
# taken, not the outcome - the outcome is identical either way, which is
# exactly why it needs saying out loud in the log a human reads.
logged "still holding the deploy lock handed over with this tick" \
    || fail "scenario 1 re-acquired the lock instead of keeping the one it was handed"
if logged "another deploy is in progress"; then
    fail "scenario 1 lost the deploy lock across the handover"
fi
if logged "already deployed - nothing to build"; then
    fail "scenario 1 handed over and then decided there was nothing to do"
fi
[ "$(builds)" = "1" ] || fail "scenario 1 ran $(builds) builds; a handover must not build twice"
[ "$(cat "$MARKER")" = "$NEW_SHA" ] \
    || fail "scenario 1 recorded '$(cat "$MARKER")' instead of ${NEW_SHA}"
[ "$WATCHER_RC" = "0" ] || fail "scenario 1 exited ${WATCHER_RC}"
printf 'OK: a main that changes the watcher is deployed BY the watcher it brings\n'

# --- scenario 2: a watcher that does not parse is refused before the merge --
# Deploying it would kill the deploy chain at the next tick instead of this
# one, with the checkout already advanced and no one watching. Refusing costs
# nothing here: the previous build keeps serving.
fresh_repo
printf '\nif [ ; then\n' >>"${REPO}/tools/autopilot/deploy_watcher.sh"
publish "watcher change that does not parse"
run_watcher

logged "does not parse" \
    || fail "scenario 2 did not report the broken watcher"
[ "$(builds)" = "0" ] || fail "scenario 2 deployed a watcher that cannot run"
[ "$(head_sha)" = "$BASE_SHA" ] \
    || fail "scenario 2 advanced the checkout before refusing"
[ "$(cat "$MARKER")" = "0000000000000000000000000000000000000000" ] \
    || fail "scenario 2 touched the deployment marker"
printf 'OK: a watcher that does not parse is refused with nothing merged\n'

# --- scenario 3: a commit that leaves the watcher alone is unchanged --------
fresh_repo
printf 'second\n' >>"${REPO}/file.txt"
publish "ordinary change"
run_watcher

if logged "changes this watcher itself"; then
    fail "scenario 3 handed over for a commit that does not touch the watcher"
fi
if logged "handing this tick over"; then
    fail "scenario 3 re-executed for nothing"
fi
[ "$(builds)" = "1" ] || fail "scenario 3 ran $(builds) builds"
[ "$(cat "$MARKER")" = "$NEW_SHA" ] || fail "scenario 3 did not record the deploy"
printf 'OK: an ordinary commit deploys exactly as before, with no handover\n'

# --- scenario 4: the old behaviour is still reachable, and it says so -------
fresh_repo
mark_new_watcher
publish "watcher change"
SELF_UPDATE=0 run_watcher

logged "AUTOPILOT_SELF_UPDATE is off" \
    || fail "scenario 4 deployed under the old watcher without saying so"
if logged "NEW-WATCHER-SPEAKING"; then
    fail "scenario 4 handed over although self-update is off"
fi
[ "$(builds)" = "1" ] || fail "scenario 4 ran $(builds) builds"
[ "$(cat "$MARKER")" = "$NEW_SHA" ] || fail "scenario 4 did not record the deploy"
printf 'OK: with self-update off the tick still deploys, loudly, under the old script\n'

# --- scenario 5: a failed build after a handover rolls back to what served --
# The handed-over process merged before it started, so its own HEAD is the
# commit under test; the commit to return to has to be carried across. Getting
# this wrong leaves the tree on the bad commit while the old image serves.
fresh_repo
mark_new_watcher
publish "watcher change that will not build"
FAIL_BUILD=1 run_watcher

logged "handing this tick over" || fail "scenario 5 never handed over"
logged "ROLLBACK" || fail "scenario 5 did not roll back a failed build"
[ "$(head_sha)" = "$BASE_SHA" ] \
    || fail "scenario 5 left the tree on the commit whose build failed"
if [ -e "$MARKER" ]; then
    fail "scenario 5 recorded a deployment that never served"
fi
printf 'OK: a rollback after a handover returns to the commit that was serving\n'

# --- scenario 6: a second tick after a handover has nothing left to do ------
# The handover fast-forwards and then re-executes, so the checkout is already
# at the commit that was deployed. The next tick must recognise that instead of
# rebuilding it, and must not go silent about it either: a marker equal to HEAD
# is how this watcher once stopped deploying altogether (2026-08-08).
fresh_repo
mark_new_watcher
publish "watcher change"
run_watcher
[ "$(builds)" = "1" ] || fail "scenario 6 setup did not deploy"

: >"${WORK}/docker.log"
run_watcher
[ "$(builds)" = "0" ] || fail "scenario 6 rebuilt a commit that was already deployed"
[ "$(head_sha)" = "$NEW_SHA" ] || fail "scenario 6 moved the checkout off the deployed commit"
[ "$WATCHER_RC" = "0" ] || fail "scenario 6 exited ${WATCHER_RC}, not 0"
printf 'OK: the tick after a handover finds its work already done\n'

# --- scenario 7: main moves after the syntax gate, before the merge ---------
# Several sessions and a human fetch into the clone the watcher deploys, so
# `origin/main` can advance between the `git rev-parse` that names the commit
# and the merge that lands it. Merging the *ref* would hand over to a watcher
# the gate never read. The stub fires that fetch from inside the tick, at the
# rollback tag - which sits in exactly that window.
fresh_repo
mark_new_watcher
publish "watcher change"
# A later commit on top of it, present in the object store the way a fetch
# would leave it, but never in the remote - so the handed-over process's own
# fetch puts the ref back where the remote really is.
git -C "$REPO" checkout -q --detach "$NEW_SHA"
perl -0pi -e 's/NEW-WATCHER-SPEAKING/C-WATCHER-SPEAKING/' "${REPO}/tools/autopilot/deploy_watcher.sh"
git -C "$REPO" add -A
git -C "$REPO" commit --quiet -m "main moved again"
LATER_SHA="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" update-ref refs/test/later "$LATER_SHA"
git -C "$REPO" checkout -q main
rm -f "${WORK}/advanced"
ADVANCE_REF_TO="$LATER_SHA" run_watcher

logged "building\.\.\. NEW-WATCHER-SPEAKING" \
    || fail "scenario 7 did not deploy the commit it had vetted"
if logged "C-WATCHER-SPEAKING"; then
    fail "scenario 7 handed over to a commit that arrived after the syntax gate"
fi
[ "$(head_sha)" = "$NEW_SHA" ] || fail "scenario 7 merged past the vetted commit"
[ "$(cat "$MARKER")" = "$NEW_SHA" ] || fail "scenario 7 recorded '$(cat "$MARKER")', not ${NEW_SHA}"
[ "$(builds)" = "1" ] || fail "scenario 7 ran $(builds) builds"
printf 'OK: the tick merges the commit it vetted, not whatever the ref points at now\n'

# --- scenario 8: the gate must use the interpreter that will run it ---------
# The LaunchAgent execs /bin/bash - 3.2.57 here - while handing the job a PATH
# that starts with /opt/homebrew/bin, where bash is 5.x. Measured on this Mac,
# `cmd &>> file` is exit 0 under `bash -n` 5.3.15 and a syntax error under
# /bin/bash 3.2.57. A gate that asks the wrong bash waves such a watcher
# through, the merge lands it, and then the tick that execs it dies at its
# first byte, every tick, with the checkout already advanced.
fresh_repo
cat >>"${REPO}/tools/autopilot/deploy_watcher.sh" <<'B4'

if false; then
    # Parses under bash 5, syntax error under bash 3.2. Never executed - the
    # point is what the *parser* does with it.
    printf 'unreachable' &>> /dev/null
fi
B4
publish "watcher using syntax the running bash cannot parse"
run_watcher

logged "does not parse" \
    || fail "scenario 8 let through a watcher the interpreter that runs it cannot parse"
[ "$(builds)" = "0" ] || fail "scenario 8 deployed it anyway"
[ "$(head_sha)" = "$BASE_SHA" ] \
    || fail "scenario 8 advanced the checkout to a watcher that cannot start"
printf 'OK: the incoming watcher is vetted by the bash that will execute it\n'
