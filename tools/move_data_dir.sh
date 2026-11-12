#!/bin/bash
# Move the deployment's state directory out of the git working copy - and back
# (#639).
#
# On the machine that runs the app, `<repo>/data` is bound at /app/data: the
# mail cursor, the Google spend ledger, attachments, score snapshots, the
# in-flight markers, the watcher's own markers. It is also a directory inside a
# checkout where sessions run git and pytest. This script moves it to a
# directory outside the repository and points the one variable that names it,
# IDEALISTA_DATA_DIR in `<repo>/.env`, at the new place.
#
#   move_data_dir.sh --check  /absolute/destination   # read-only: may it run?
#   move_data_dir.sh --apply  /absolute/destination   # do it (owner's command)
#   move_data_dir.sh --rollback                       # put it back
#
# `--check` is the default and changes nothing. `--apply` stops the app for
# about a minute.
#
# How it moves, and why that way
# ------------------------------
# One rename(2) on the same volume, after a checksummed safety copy. A rename
# means there is never a second authoritative copy to diverge: what the app
# writes after the move is in the directory that `--rollback` renames back.
# The copy is for the day the rename itself goes wrong.
#
# What it refuses to leave behind
# -------------------------------
# From the moment the app is stopped, EVERY exit that is not success - a failed
# check, a failed command, Ctrl-C, a closed terminal, a `kill` - runs `recover`,
# which returns production to the directory inside the checkout and starts the
# app. Three rules make that independent of where the failure struck:
#
#   1. PHASE names an intent and is assigned BEFORE the command it covers. A
#      command that half-succeeded, or succeeded while its own confirmation
#      failed, is always inside a phase whose recovery undoes it.
#   2. `recover` does not trust PHASE for the facts. It inspects what is on
#      disk and every step of it is idempotent, so it is also what
#      `--rollback` runs.
#   3. HUP, INT and TERM are trapped to `exit` (bash runs no EXIT trap for an
#      untrapped fatal signal) and ignored while `recover` runs.
#
# SIGKILL and a power cut cannot be trapped by anything. `--rollback`
# afterwards performs the same idempotent sequence, and DATA_DIR_RUNBOOK.md
# has it as commands to type.
#
# Every `docker compose` call here runs with IDEALISTA_DATA_DIR removed from
# the environment, so only `.env` decides the mount - an export inherited from
# the operator's shell would otherwise outrank the line this script writes.

set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
REPO="${MOVE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd -P)}"
LIB_DIR="${SCRIPT_DIR}/autopilot/lib"

KEY="IDEALISTA_DATA_DIR"
TOMBSTONE=".moved-to"
BRANCH="${MOVE_BRANCH:-main}"
COMPOSE_FILE="${MOVE_COMPOSE_FILE:-docker-compose.yml}"
APP="${MOVE_APP_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-app}"
CONTAINER_STATE="/app/data"
HEALTH_URL="${MOVE_HEALTH_URL:-http://127.0.0.1:5001/api/healthz}"
HEALTH_TIMEOUT="${MOVE_HEALTH_TIMEOUT:-180}"
HEALTH_POLL="${MOVE_HEALTH_POLL:-5}"
BACKUP_ROOT="${MOVE_BACKUP_ROOT:-${HOME}/IdealistaRank-backups}"
# The watcher's and the merge bot's own lock files. Holding both for the
# length of the move means no tick builds and no merge pass writes the review
# journal while the directory is in two places.
DEPLOY_LOCK="${MOVE_DEPLOY_LOCK:-/tmp/idealista-autopilot-deploy.lock.d}"
MERGE_LOCK="${MOVE_MERGE_LOCK:-/tmp/idealista-autopilot-merge.lock.d}"
STATUS_CMD="${MOVE_STATUS_CMD:-${REPO}/tools/backfill_status.sh}"
# The four committed reference files (docs/rules/reference-data.md). They stay
# tracked in <repo>/data and the image carries its own copy.
REFERENCE_FILES="ine_municipal.json hospitals_cnh.json sepe_unemployment.json top_agencies.json"

# The directory inside the checkout, from the one file that names it.
SRC="$(/bin/bash "${LIB_DIR}/data_dir.sh" --default "$REPO")" || {
    printf 'cannot read %s/data_dir.sh - is this the repository that carries it?\n' "$LIB_DIR" >&2
    exit 2
}
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PHASE="preflight"
DEST=""
ASIDE=""
WORK=""

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage:
  move_data_dir.sh [--check] /absolute/destination   read-only: may the move run?
  move_data_dir.sh --apply   /absolute/destination   move the state directory
  move_data_dir.sh --rollback                        move it back into the checkout
EOF
}

# --- small readers ----------------------------------------------------------

# Neither docker nor anything it leaves running may inherit the two lock
# descriptors: a helper process that outlives this script would keep the
# deploy lock held with nobody behind it, and every later watcher tick would
# skip as "another deploy is in progress". Closed for every child, here.
dk() { docker "$@" 8>&- 9>&-; }

compose() {
    ( cd "$REPO" && env -u "$KEY" docker compose -f "$COMPOSE_FILE" "$@" ) 8>&- 9>&-
}

# 0 running, 1 stopped, 2 could not tell. "Could not tell" is its own answer:
# a probe that failed must never read as "stopped".
app_running() {
    local out
    out="$(dk inspect -f '{{.State.Running}}' "$APP" 2>/dev/null)" || return 2
    case "$out" in
        true) return 0 ;;
        false) return 1 ;;
        *) return 2 ;;
    esac
}

mount_source() {
    dk inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$APP" 2>/dev/null
}

phys() { ( cd "$1" 2>/dev/null && pwd -P ); }

device_of() {
    python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_dev)' "$1" 2>/dev/null
}

env_line_count() {
    if [ ! -f "${REPO}/.env" ]; then
        printf '0\n'
        return 0
    fi
    grep -c "^${KEY}=" "${REPO}/.env" || true
}

# The value the one reader gives, with this process's own export out of the
# way: the question is what `.env` says.
resolved_data_dir() {
    env -u "$KEY" /bin/bash "${LIB_DIR}/data_dir.sh" "$REPO"
}

# Write or remove the one line. Never prints the file. A temporary file in the
# same directory with the original's mode, fsync, atomic rename: `.env` holds
# every credential this deployment has, and a half-written one stops it.
set_env_line() {
    python3 - "${REPO}/.env" "$KEY" "${1:-}" <<'PY'
import os
import stat
import sys
import tempfile

path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "rb") as handle:
    lines = handle.read().splitlines(keepends=True)
prefix = (key + "=").encode()
kept = [line for line in lines if not line.startswith(prefix)]
if kept and not kept[-1].endswith(b"\n"):
    kept[-1] += b"\n"
if value:
    kept.append(prefix + value.encode() + b"\n")
mode = stat.S_IMODE(os.stat(path).st_mode)
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.writelines(kept)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise
PY
}

# Every regular file's sha256, plus the name of everything that is not one, so
# an empty directory that went missing is a difference too.
manifest() {
    ( cd "$1" && {
        find . -type f -exec shasum -a 256 {} +
        find . ! -type f -print | sed 's/^/entry  /'
    } | LC_ALL=C sort ) >"$2"
}

# Equal, or different only in `*.log` files. The launchd agents and the AI
# bridge append to logs in this directory from the host, and they are not
# stopped by this script: a log line written between two passes is not a
# failed copy. Anything else that differs is.
manifests_agree() {
    local first="$1" second="$2" label="$3" differing
    if cmp -s "$first" "$second"; then
        return 0
    fi
    differing="$(diff "$first" "$second" | grep '^[<>]' | sed 's/^[<>] *[^ ]*  *//' | LC_ALL=C sort -u || true)"
    if [ -z "$differing" ]; then
        return 0
    fi
    if printf '%s\n' "$differing" | grep -v '\.log$' >/dev/null; then
        say "ERROR: ${label}: these entries differ and are not logs:"
        printf '%s\n' "$differing" | grep -v '\.log$' | sed 's/^/    /'
        return 1
    fi
    note "${label}: only logs differ (a host process appended meanwhile):"
    printf '%s\n' "$differing" | sed 's/^/      /'
    return 0
}

take_lock() {
    # take_lock <fd> <file>: flock(2), the same primitive lib/lock.sh uses, on
    # a descriptor this shell keeps open until it exits.
    # shellcheck disable=SC2034  # `file` is read by the eval below
    local fd="$1" file="$2"
    eval "exec ${fd}>\"\$file\"" || return 1
    python3 -c '
import fcntl
import sys

try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
' "$fd"
}

take_both_locks() {
    if ! take_lock 9 "$DEPLOY_LOCK"; then
        say "STOP: the deploy watcher holds ${DEPLOY_LOCK} - a deploy is in progress. Nothing was touched."
        exit 1
    fi
    if ! take_lock 8 "$MERGE_LOCK"; then
        say "STOP: the merge bot holds ${MERGE_LOCK} - a merge pass is in progress. Nothing was touched."
        exit 1
    fi
    note "holding the deploy lock and the merge lock until this script exits"
}

# --- is the app serving from the directory it should? ------------------------

verify_serving() {
    # verify_serving <expected host directory>
    local expected="$1" source waited=0 url token probe seen name
    source="$(mount_source)" || true
    if [ -z "$source" ] || [ "$(phys "$source")" != "$(phys "$expected")" ]; then
        say "ERROR: ${APP} mounts '${source:-nothing}' at ${CONTAINER_STATE}, expected ${expected}"
        return 1
    fi
    note "mount: ${source} -> ${CONTAINER_STATE}"

    until curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
        waited=$((waited + HEALTH_POLL))
        if [ "$waited" -gt "$HEALTH_TIMEOUT" ]; then
            say "ERROR: ${HEALTH_URL} did not answer within ${HEALTH_TIMEOUT}s"
            return 1
        fi
        sleep "$HEALTH_POLL"
    done
    note "healthz: ok"

    # A green healthz is not acceptance (lib/render_check.sh): a page that
    # renders a template must answer 200, and a 3xx is the failure looked for.
    # shellcheck disable=SC1091
    . "${LIB_DIR}/render_check.sh"
    url="$(deploy_render_url "$(deploy_render_origin "$HEALTH_URL")")"
    if [ -z "$url" ]; then
        say "ERROR: the page check is switched off (DEPLOY_RENDER_PATH is empty); a move cannot be called verified without it"
        return 1
    fi
    if ! deploy_render_ok "$url"; then
        say "ERROR: ${url} answered ${DEPLOY_RENDER_STATUS:-nothing}, not 200"
        return 1
    fi
    note "page: ${url} -> 200"

    # The container's /app/data IS this directory: a token written here on the
    # host is read back through the container. Stronger than comparing the
    # mail cursor, which the scheduler is entitled to advance the moment the
    # app is up.
    token="move-probe-${STAMP}-$$"
    probe="${expected}/.move-probe-${STAMP}"
    printf '%s\n' "$token" >"$probe"
    seen="$(dk exec "$APP" cat "${CONTAINER_STATE}/.move-probe-${STAMP}" 2>/dev/null || true)"
    rm -f "$probe"
    if [ "$seen" != "$token" ]; then
        say "ERROR: a file written to ${expected} on the host is not what ${APP} reads at ${CONTAINER_STATE}"
        return 1
    fi
    note "the container reads the host directory (probe round-trip)"

    for name in $REFERENCE_FILES; do
        # shellcheck disable=SC2016
        if ! dk exec "$APP" sh -c 'test -r "${REFERENCE_DATA_DIR:?}/$1"' sh "$name" >/dev/null 2>&1; then
            say "ERROR: ${APP} cannot read the reference file ${name} from its image"
            return 1
        fi
    done
    note "reference files: readable in the image"
    return 0
}

# --- back to the directory inside the checkout -------------------------------

manual_recovery() {
    cat <<EOF

  Production may be DOWN. Nothing below was done for you; run it by hand, in
  this order, and stop at the first command that fails:

    cd ${REPO}
    env -u ${KEY} docker compose -f ${COMPOSE_FILE} stop app
    # if ${DEST:-<destination>} exists and ${SRC} is the small rebuilt directory
    # (it holds ${TOMBSTONE}):
    mv ${SRC} ${BACKUP_ROOT}/checkout-data-at-rollback-${STAMP}
    mv ${DEST:-<destination>} ${SRC}
    git -C ${REPO} checkout -- data
    # then remove the line '${KEY}=...' from ${REPO}/.env
    env -u ${KEY} docker compose -f ${COMPOSE_FILE} up -d --no-build app
    curl -s -o /dev/null -w '%{http_code}\n' ${HEALTH_URL%/api/healthz}/properties   # must print 200

  The safety copy of the directory as it was before the move, if one was
  taken, is under ${BACKUP_ROOT}/data-pre-move-*; it is older than anything
  written after the move.
EOF
}

# Idempotent, and driven by what is on disk rather than by PHASE. Returns 0
# only when the app is serving from <repo>/data again.
restore_original() {
    local running_rc=0 rebuilt=0

    # Nothing was moved: the original directory is where it was, nothing is at
    # the destination and .env does not name the variable. Then there is
    # nothing to undo and no reason to stop anything - a failure this early
    # (the stop itself refused, the safety copy did not match) may well have
    # left the app running, and stopping it again to "recover" would be the
    # only downtime of the whole attempt. `up` starts a stopped container and
    # leaves a running one alone.
    if { [ -z "$DEST" ] || { [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; }; } \
        && [ -d "$SRC" ] && [ ! -e "${SRC}/${TOMBSTONE}" ] \
        && [ "$(env_line_count)" = "0" ]; then
        note "nothing was moved; making sure ${APP} is up on ${SRC}"
        if ! compose up -d --no-build app >/dev/null 2>&1; then
            say "ERROR: 'docker compose up -d --no-build app' failed"
            return 1
        fi
        verify_serving "$SRC"
        return
    fi

    compose stop app >/dev/null 2>&1 || true
    app_running || running_rc=$?
    if [ "$running_rc" != "1" ]; then
        say "ERROR: cannot confirm ${APP} is stopped (probe answered ${running_rc}); not moving a directory under a container that may be running"
        return 1
    fi

    if [ -n "$DEST" ] && [ -d "$DEST" ]; then
        if [ -e "$SRC" ]; then
            if [ -f "${SRC}/${TOMBSTONE}" ]; then
                rebuilt=1
            elif rmdir "$SRC" 2>/dev/null; then
                # An empty directory: the rebuild got as far as mkdir.
                rebuilt=0
            else
                say "ERROR: both ${SRC} (without ${TOMBSTONE}) and ${DEST} exist; cannot tell which holds the state"
                return 1
            fi
        fi
        if [ "$rebuilt" = "1" ]; then
            ASIDE="${BACKUP_ROOT}/checkout-data-at-rollback-${STAMP}"
            mkdir -p "$BACKUP_ROOT" || return 1
            mv "$SRC" "$ASIDE" || {
                say "ERROR: could not move the rebuilt ${SRC} aside to ${ASIDE}"
                return 1
            }
            note "the rebuilt ${SRC} is kept at ${ASIDE}"
        fi
        mv "$DEST" "$SRC" || {
            say "ERROR: could not rename ${DEST} back to ${SRC}"
            return 1
        }
        note "renamed ${DEST} back to ${SRC}"
        # The tracked reference files as HEAD has them: a merge may have
        # updated one while the old copies sat in the moved directory.
        git -C "$REPO" checkout -- data >/dev/null 2>&1 \
            || note "WARNING: 'git checkout -- data' failed; check the four reference files by hand"
        if [ -n "$ASIDE" ] && [ -f "${ASIDE}/.orchestrator" ]; then
            cp -p "${ASIDE}/.orchestrator" "${SRC}/.orchestrator" \
                || note "WARNING: could not restore the newer .orchestrator from ${ASIDE}"
        fi
    elif [ ! -d "$SRC" ] || [ -f "${SRC}/${TOMBSTONE}" ]; then
        say "ERROR: ${SRC} is not the original directory and ${DEST:-the destination} does not exist"
        return 1
    fi

    if [ "$(env_line_count)" != "0" ]; then
        set_env_line "" || {
            say "ERROR: could not remove the ${KEY} line from ${REPO}/.env"
            return 1
        }
        note "removed the ${KEY} line from .env"
    fi

    if ! compose up -d --no-build app >/dev/null 2>&1; then
        say "ERROR: 'docker compose up -d --no-build app' failed"
        return 1
    fi
    verify_serving "$SRC"
}

recover() {
    local rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    set +e
    [ -n "$WORK" ] && rm -rf "$WORK"
    if [ "$PHASE" = "done" ] || [ "$PHASE" = "preflight" ]; then
        exit "$rc"
    fi
    say ""
    say "FAILED in phase '${PHASE}' (exit ${rc}). Returning production to ${SRC}."
    if restore_original; then
        say "RECOVERED: the app is serving from ${SRC} again. The move did not happen."
        exit 1
    fi
    say "RECOVERY INCOMPLETE."
    manual_recovery
    exit 3
}

die() {
    say "ERROR: $*"
    exit 1
}

# --- preflight: read-only, and every problem is listed, not just the first ---

PROBLEMS=0
problem() {
    PROBLEMS=$((PROBLEMS + 1))
    say "  STOP: $*"
}
fine() { say "  ok:   $*"; }

preflight_common() {
    local tool status_rc=0
    for tool in docker git python3 curl shasum; do
        command -v "$tool" >/dev/null 2>&1 || problem "${tool} is not on PATH"
    done
    if [ -n "${IDEALISTA_DATA_DIR+set}" ]; then
        problem "${KEY} is set in this shell's environment ('${IDEALISTA_DATA_DIR}'); Compose would let it outrank .env. Run: unset ${KEY}"
    else
        fine "${KEY} is not exported in this shell"
    fi
    [ -f "${REPO}/.env" ] || problem "${REPO}/.env does not exist"
    [ -r "${LIB_DIR}/data_dir.sh" ] || problem "${LIB_DIR}/data_dir.sh is missing"
    [ -r "${LIB_DIR}/render_check.sh" ] || problem "${LIB_DIR}/render_check.sh is missing"

    "$STATUS_CMD" >/dev/null 2>&1 || status_rc=$?
    case "$status_rc" in
        0) fine "no backfill is running or expected (${STATUS_CMD##*/})" ;;
        *) problem "${STATUS_CMD##*/} answered ${status_rc} (1 busy, 2 unknown); stopping the app would kill that work. Run it to see what" ;;
    esac
}

preflight_apply() {
    local head deployed parent source count name other running_rc=0 link
    say "preflight (read-only):"
    preflight_common

    # The destination.
    case "$DEST" in
        /*) ;;
        *) problem "the destination must be an absolute path, got '${DEST}'" ;;
    esac
    case "$DEST" in
        *[[:space:]\"\'\#\$\\]*) problem "the destination must be a plain path: no spaces, quotes, '#', '\$' or backslashes (the .env reader refuses them)" ;;
    esac
    DEST="${DEST%/}"
    if [ -e "$DEST" ] || [ -L "$DEST" ]; then
        problem "${DEST} already exists; the move is a rename onto a name that must be free"
    fi
    parent="$(dirname "$DEST")"
    if [ ! -d "$parent" ] || [ ! -w "$parent" ]; then
        problem "${parent} is not a writable directory"
    else
        case "$(phys "$parent")/" in
            "$(phys "$REPO")"/*) problem "${DEST} is inside the repository; the point is to leave it" ;;
        esac
        if [ -d "$SRC" ] && [ "$(device_of "$parent")" != "$(device_of "$SRC")" ]; then
            problem "${parent} and ${SRC} are on different volumes; the move must be one rename"
        else
            fine "${DEST} is free, outside the repository, on the same volume"
        fi
    fi

    # The checkout.
    if [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$BRANCH" ]; then
        problem "${REPO} is not on ${BRANCH}"
    fi
    if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
        problem "${REPO} has uncommitted or untracked files; 'git checkout -- data' must have nothing to lose"
    else
        fine "the checkout is on ${BRANCH} and clean"
    fi
    count="$(env_line_count)"
    [ "$count" = "0" ] || problem "${REPO}/.env already sets ${KEY} (${count} line(s)); the state may already be moved - see --rollback"
    [ ! -e "${SRC}/${TOMBSTONE}" ] || problem "${SRC}/${TOMBSTONE} exists; the state was moved before"
    [ -d "$SRC" ] || problem "${SRC} is not a directory"
    if [ -d "$SRC" ]; then
        link="$(find "$SRC" -type l -print 2>/dev/null | head -n 1)"
        [ -z "$link" ] || problem "${SRC} contains a symbolic link (${link}); resolve it by hand first"
    fi

    # What is serving.
    head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    deployed="$(cat "${SRC}/.deployed_sha" 2>/dev/null || true)"
    if [ -z "$head" ] || [ "$deployed" != "$head" ]; then
        problem "a deploy is pending: .deployed_sha is '${deployed:0:7}', HEAD is '${head:0:7}'. Let the watcher finish"
    else
        fine "what serves is HEAD (${head:0:7})"
    fi
    app_running || running_rc=$?
    if [ "$running_rc" != "0" ]; then
        problem "${APP} is not running (probe answered ${running_rc})"
    else
        source="$(mount_source)" || true
        if [ -z "$source" ] || [ "$(phys "$source")" != "$(phys "$SRC")" ]; then
            problem "${APP} mounts '${source:-nothing}' at ${CONTAINER_STATE}, not ${SRC}"
        else
            fine "${APP} mounts ${SRC}"
        fi
        # The image has to carry its own reference files, or the app loses
        # them the moment /app/data stops being the checkout's directory.
        for name in $REFERENCE_FILES; do
            # shellcheck disable=SC2016
            if ! dk exec "$APP" sh -c 'test -r "${REFERENCE_DATA_DIR:?}/$1"' sh "$name" >/dev/null 2>&1; then
                problem "the running image does not carry ${name} under REFERENCE_DATA_DIR; deploy the build that does first"
            fi
        done
    fi
    while IFS= read -r other; do
        [ -n "$other" ] || continue
        [ "$other" != "$APP" ] || continue
        if dk inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$other" 2>/dev/null \
            | grep -Fx -- "$SRC" >/dev/null; then
            problem "container ${other} also mounts ${SRC}; remove it first (docker rm ${other})"
        fi
    done < <(dk ps -a --format '{{.Names}}' 2>/dev/null || true)
}

preflight_rollback() {
    local value
    say "preflight (read-only):"
    preflight_common
    value="$(resolved_data_dir 2>/dev/null || true)"
    if [ "$(env_line_count)" = "0" ] && [ ! -e "${SRC}/${TOMBSTONE}" ]; then
        problem "nothing to roll back: .env has no ${KEY} line and ${SRC} carries no ${TOMBSTONE}"
        return 0
    fi
    if [ -z "$value" ] || [ "$value" = "$SRC" ]; then
        # The line is gone but the tombstone stayed: take the destination the
        # tombstone recorded.
        value="$(sed -n 's/^moved_to=//p' "${SRC}/${TOMBSTONE}" 2>/dev/null | head -n 1)"
    fi
    DEST="${value%/}"
    if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
        problem "cannot find the moved directory ('${DEST:-unknown}' is not a directory)"
    else
        fine "the state is in ${DEST}"
    fi
}

# --- modes -------------------------------------------------------------------

MODE="check"
case "${1:-}" in
    --check) MODE="check"; shift ;;
    --apply) MODE="apply"; shift ;;
    --rollback) MODE="rollback"; shift ;;
    -h | --help) usage; exit 0 ;;
esac

if [ "$MODE" = "rollback" ]; then
    [ $# -eq 0 ] || { usage; exit 2; }
    preflight_rollback
    if [ "$PROBLEMS" != "0" ]; then
        say "NOT ROLLED BACK: ${PROBLEMS} problem(s) above. Nothing was touched."
        exit 1
    fi
    take_both_locks
    trap recover EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # An intent, set before the first command it covers: a signal from here on
    # runs the same idempotent sequence again instead of leaving it half done.
    PHASE="rolling-back"
    say "rolling back: ${DEST} -> ${SRC} (the app stops for about a minute)"
    if restore_original; then
        PHASE="done"
        trap - EXIT HUP INT TERM
        say "ROLLED BACK: the app is serving from ${SRC}."
        [ -z "$ASIDE" ] || say "  the small directory that stood in ${SRC} meanwhile is kept at ${ASIDE}"
        exit 0
    fi
    PHASE="done"
    trap - EXIT HUP INT TERM
    say "ROLLBACK INCOMPLETE."
    manual_recovery
    exit 3
fi

[ $# -eq 1 ] || { usage; exit 2; }
DEST="$1"
preflight_apply
if [ "$PROBLEMS" != "0" ]; then
    say "NOT MOVED: ${PROBLEMS} problem(s) above. Nothing was touched."
    exit 1
fi
if [ "$MODE" = "check" ]; then
    say "READY: nothing above stops the move. Nothing was touched."
    say "  to move:  $0 --apply ${DEST}"
    say "  it stops ${APP} for about a minute, copies ${SRC} to ${BACKUP_ROOT}/data-pre-move-<UTC>,"
    say "  renames ${SRC} to ${DEST}, writes ${KEY} to .env and starts the app again."
    exit 0
fi

take_both_locks
BACKUP="${BACKUP_ROOT}/data-pre-move-${STAMP}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/idealista-move.XXXXXX")"

# From here on, no exit leaves the app down.
trap recover EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

PHASE="stopping"
say "1/6 stopping ${APP} (downtime starts)"
compose stop app
stopped_rc=0
app_running || stopped_rc=$?
[ "$stopped_rc" = "1" ] || die "${APP} is not confirmed stopped (probe answered ${stopped_rc})"
# Only now, with no container writing: what the directory holds.
manifest "$SRC" "${WORK}/before"

PHASE="copying"
say "2/6 safety copy -> ${BACKUP}"
mkdir -p "$BACKUP_ROOT"
[ ! -e "$BACKUP" ] || die "${BACKUP} already exists"
cp -Rp "$SRC" "$BACKUP"
manifest "$BACKUP" "${WORK}/copy"
manifests_agree "${WORK}/before" "${WORK}/copy" "safety copy" \
    || die "the safety copy does not match the directory"

PHASE="renaming"
say "3/6 renaming ${SRC} -> ${DEST}"
mv "$SRC" "$DEST"
[ ! -e "$SRC" ] || die "${SRC} still exists after the rename"
[ -d "$DEST" ] || die "${DEST} is not a directory after the rename"
manifest "$DEST" "${WORK}/moved"
manifests_agree "${WORK}/before" "${WORK}/moved" "moved directory" \
    || die "the moved directory does not match what was there before"

PHASE="switching"
say "4/6 rebuilding ${SRC} and pointing .env at ${DEST}"
# The tombstone first: from this line on, a <repo>/data that exists carries it,
# which is how recovery tells the small rebuilt directory from the original.
mkdir "$SRC"
printf 'moved_to=%s\nmoved_at=%s\n' "$DEST" "$STAMP" >"${SRC}/${TOMBSTONE}"
git -C "$REPO" checkout -- data
if [ -f "${DEST}/.orchestrator" ]; then
    # An agreement between sessions, read from the checkout; not state.
    cp -p "${DEST}/.orchestrator" "${SRC}/.orchestrator"
fi
set_env_line "$DEST"
[ "$(resolved_data_dir)" = "$DEST" ] \
    || die "tools/autopilot/lib/data_dir.sh does not resolve ${DEST} from .env"

PHASE="starting"
say "5/6 starting ${APP}"
compose up -d --no-build app

say "6/6 verifying"
verify_serving "$DEST" || die "the app is not serving correctly from ${DEST}"

PHASE="done"
trap - EXIT HUP INT TERM
rm -rf "$WORK"
say ""
say "MOVED: the deployment's state is in ${DEST}."
say "  .env:        ${KEY}=${DEST}"
say "  safety copy: ${BACKUP} (delete it once you trust the move)"
say "  ${SRC} now holds the four reference files, ${TOMBSTONE} and .orchestrator."
say "  to undo:     $0 --rollback"
