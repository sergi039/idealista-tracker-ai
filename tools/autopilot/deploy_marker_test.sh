#!/bin/bash
# The deployment marker must never name a build that is not serving.
#
# Observed 2026-08-08: the rollback rebuild failed, the previous container was
# still up and answered healthz, and the watcher recorded HEAD as deployed. The
# marker then equalled HEAD on every later tick, so the watcher skipped the
# deploy forever while the app served the build before it. A silent stall is
# the worst failure mode this script has, because everything downstream - the
# merge bot, the issue runner - reports success.
#
# This drives the real deploy_watcher.sh against a throwaway repository with a
# docker that always fails and a health endpoint that always says yes: exactly
# the shape of that incident.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER="${SCRIPT_DIR}/deploy_watcher.sh"

WORK="$(mktemp -d)"
HEALTH_PID=""
cleanup() {
    # Keep the verdict: reaping the stub yields 143, and letting that be the
    # trap's result would report a passing run as a SIGTERM failure.
    local rc=$?
    if [ -n "$HEALTH_PID" ]; then
        kill "$HEALTH_PID" 2>/dev/null
        # Reap it, or bash prints its own "Terminated" line over the verdict.
        # `|| true` is load-bearing: wait reports 143 for a signalled child and
        # set -e would end the trap there, making every run exit 143.
        wait "$HEALTH_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
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

head_sha="$(git rev-parse HEAD)"

# The marker names an older build, so the watcher has work to do. Any sha that
# is not HEAD will do - it is only ever compared, never resolved.
MARKER="${WORK}/deployed_sha"
printf '%s\n' "0000000000000000000000000000000000000000" >"$MARKER"

# --- a docker that cannot build --------------------------------------------
STUB_BIN="${WORK}/bin"
mkdir -p "$STUB_BIN"
cat >"${STUB_BIN}/docker" <<'STUB'
#!/bin/bash
echo "stub docker: refusing to build" >&2
exit 1
STUB
chmod +x "${STUB_BIN}/docker"

# --- a health endpoint that always answers ok -------------------------------
# This is the crux: the container that was already running is untouched by a
# failed build, so health says yes while the new code is nowhere.
HEALTH_PORT=""
for candidate in $(seq 45871 45899); do
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
        body = b'{"ok":true,"checks":{"database":"ok"}}'
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

# --- run the watcher --------------------------------------------------------
set +e
PATH="${STUB_BIN}:${PATH}" \
AUTOPILOT_REPO_DIR="$REPO" \
AUTOPILOT_DEPLOYED_MARKER="$MARKER" \
AUTOPILOT_LOG_FILE="${WORK}/watcher.log" \
AUTOPILOT_LOCK_DIR="${WORK}/lock.d" \
AUTOPILOT_HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/" \
AUTOPILOT_HEALTH_TIMEOUT=10 \
AUTOPILOT_COMPOSE_FILE="docker-compose.yml" \
    bash "$WATCHER" >/dev/null 2>&1
set -e

# --- the assertion ----------------------------------------------------------
if [ -e "$MARKER" ]; then
    recorded="$(cat "$MARKER")"
    if [ "$recorded" = "$head_sha" ]; then
        printf '%s\n' "--- watcher log ---" >&2
        cat "${WORK}/watcher.log" >&2 2>/dev/null || true
        fail "the marker names ${head_sha:0:7} although the build failed and nothing was deployed"
    fi
    fail "the marker survives a failed deploy holding '${recorded}'; it must be cleared"
fi

printf 'OK: a failed rollback rebuild leaves no deployment marker\n'
