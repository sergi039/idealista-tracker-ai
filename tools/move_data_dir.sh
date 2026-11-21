#!/bin/bash
# May the deployment's state directory be moved out of the git working copy?
# (#639)
#
# On the machine that runs the app, `<repo>/data` is bound at /app/data: the
# mail cursor, the Google spend ledger, attachments, score snapshots, the
# in-flight markers, the watcher's own markers. It is also a directory inside a
# checkout where sessions run git and pytest. Moving it to a directory outside
# the repository means pointing the one variable that names it,
# IDEALISTA_DATA_DIR in `<repo>/.env`, at the new place - with the app stopped
# for about a minute.
#
#   move_data_dir.sh [--check] /absolute/destination
#
# This file is the question before that move, and only the question: it reads
# and reports, and holds no command that stops a container, renames a
# directory or writes `.env`. Every problem is listed, not just the first, and
# the exit status is the verdict: 0 ready, 1 not. The move itself, and the way
# back, are the next change.

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
STATUS_CMD="${MOVE_STATUS_CMD:-${REPO}/tools/backfill_status.sh}"
# The four committed reference files (docs/rules/reference-data.md). They stay
# tracked in <repo>/data and the image carries its own copy.
REFERENCE_FILES="ine_municipal.json hospitals_cnh.json sepe_unemployment.json top_agencies.json"

# The directory inside the checkout, from the one file that names it.
SRC="$(/bin/bash "${LIB_DIR}/data_dir.sh" --default "$REPO")" || {
    printf 'cannot read %s/data_dir.sh - is this the repository that carries it?\n' "$LIB_DIR" >&2
    exit 2
}
DEST=""

say() { printf '%s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage:
  move_data_dir.sh [--check] /absolute/destination   read-only: may the move run?
EOF
}

# --- small readers ----------------------------------------------------------

dk() { docker "$@"; }

# 0 running, 1 stopped, 2 could not tell. "Could not tell" is its own answer:
# a probe that failed must never read as "stopped".
app_running() {
    local out names
    if ! out="$(dk inspect -f '{{.State.Running}}' "$APP" 2>/dev/null)"; then
        # inspect fails the same way for a daemon that does not answer and
        # for a container that does not exist - which is what a `compose up`
        # that failed while replacing it leaves. A container that is not
        # there is not running: said only when the daemon's own list was
        # read and does not have the name.
        names="$(dk ps -a --format '{{.Names}}' 2>/dev/null)" || return 2
        if printf '%s\n' "$names" | grep -Fx -- "$APP" >/dev/null; then
            return 2
        fi
        return 1
    fi
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

# The tombstone is the FACT that an entry of that name exists - a file, a
# directory, a FIFO, a link whose target is gone - read the same way by
# lib/data_dir.sh and app.py. Never decided by reading it.
has_tombstone() {
    [ -e "${1}/${TOMBSTONE}" ] || [ -L "${1}/${TOMBSTONE}" ]
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
        problem "${KEY} is set in this shell's environment; Compose would let it outrank .env. Run: unset ${KEY}"
    else
        fine "${KEY} is not exported in this shell"
    fi
    # Compose's own switches for whether and which env file it reads and which
    # project a call addresses. Exported here they hold for this script's
    # calls and not for the watcher's: the stop and the start would not be its.
    # COMPOSE_PROJECT_NAME is here and not in lib/data_dir.sh on purpose: it
    # does not move the mount (the reader's question), but it does change
    # which containers `stop` and `up` address (this script's).
    for tool in COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROJECT_NAME; do
        if printenv "$tool" >/dev/null 2>&1; then
            problem "${tool} is set in this shell's environment; Compose here would not address the deployment the watcher runs. Run: unset ${tool}"
        fi
    done
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
    local head deployed parent source count name other others mounts dirty running_rc=0 link would_mount
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

    # The contract, asked of Compose itself: with the variable naming the
    # destination, would it mount the destination? In a checkout whose compose
    # file does not carry the mount the app would come back on ${SRC}, the
    # verification would fail and the move would be undone - after the
    # downtime. Asked here, it costs nothing. The directory need not exist.
    would_mount="$(env "${KEY}=${DEST}" /bin/bash "${LIB_DIR}/data_dir.sh" --compose-source "$REPO" "$COMPOSE_FILE" 2>/dev/null)" || would_mount=""
    if [ "$would_mount" = "$DEST" ]; then
        fine "Compose would mount ${DEST} once .env names it"
    else
        # What Compose rendered is not quoted: it can carry another variable's
        # value, and this output is pasted into reports.
        problem "with ${KEY}=${DEST}, ${COMPOSE_FILE} in ${REPO} would mount something else at /app/data, or Compose cannot say - not ${DEST}"
    fi

    # The checkout.
    if [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$BRANCH" ]; then
        problem "${REPO} is not on ${BRANCH}"
    fi
    if ! dirty="$(git -C "$REPO" status --porcelain 2>/dev/null)"; then
        problem "git could not say whether ${REPO} is clean"
    elif [ -n "$dirty" ]; then
        problem "${REPO} has uncommitted or untracked files; 'git checkout -- data' must have nothing to lose"
    else
        fine "the checkout is on ${BRANCH} and clean"
    fi
    count="$(env_line_count)"
    [ "$count" = "0" ] || problem "${REPO}/.env already sets ${KEY} (${count} line(s)); the state may already be moved"
    ! has_tombstone "$SRC" || problem "${SRC}/${TOMBSTONE} exists; the state was moved before"
    [ -d "$SRC" ] || problem "${SRC} is not a directory"
    if [ -d "$SRC" ]; then
        # In a directory this user may not search, a tombstone that is there
        # reads as one that is not.
        [ -x "$SRC" ] || problem "${SRC} cannot be searched by this user; whether it carries ${TOMBSTONE} cannot be established"
        # A find that could not read every directory has not looked: under
        # pipefail its status used to end the script here without a word.
        if ! link="$(find "$SRC" -type l -print 2>/dev/null)"; then
            problem "${SRC} could not be searched in full for symbolic links"
        elif [ -n "$link" ]; then
            problem "${SRC} contains a symbolic link (${link%%$'\n'*}); resolve it by hand first"
        fi
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
    # A list or a mount that could not be read is not "nobody else mounts it".
    if ! others="$(dk ps -a --format '{{.Names}}' 2>/dev/null)"; then
        problem "docker could not list the containers; whether another one mounts ${SRC} is not known"
        others=""
    fi
    while IFS= read -r other; do
        [ -n "$other" ] || continue
        [ "$other" != "$APP" ] || continue
        if ! mounts="$(dk inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$other" 2>/dev/null)"; then
            problem "docker could not say what container ${other} mounts"
        elif printf '%s\n' "$mounts" | grep -Fx -- "$SRC" >/dev/null; then
            problem "container ${other} also mounts ${SRC}; remove it first (docker rm ${other})"
        fi
    done <<<"$others"
}

# --- the one mode -------------------------------------------------------------

case "${1:-}" in
    --check) shift ;;
    -h | --help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
esac

[ $# -eq 1 ] || { usage; exit 2; }
DEST="$1"
preflight_apply
if [ "$PROBLEMS" != "0" ]; then
    say "NOT MOVED: ${PROBLEMS} problem(s) above. Nothing was touched."
    exit 1
fi
say "READY: nothing above stops the move. Nothing was touched."
