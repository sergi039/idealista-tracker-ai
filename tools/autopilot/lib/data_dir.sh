#!/bin/bash
# Where this deployment keeps its state: one answer for every host-side tool
# (#639).
#
# The app container persists exactly one directory, mounted at /app/data: the
# mail cursor and its retry list, the Google spend ledger, attachments, score
# snapshots, the in-flight markers. On the host that directory is spelled
# `${REPO_DIR}/data` in seven scripts and `./data` in docker-compose.yml - eight
# copies of one fact, and the fact is "inside the git working copy", which is
# how a `pytest` run in the checkout came to overwrite the production mail
# cursor (2026-11-09, #627).
#
# This file is the one place the host side of that fact is written down, so the
# seven scripts can ask instead of spelling it.
#
# The directory is configured in one place: IDEALISTA_DATA_DIR in the
# checkout's `.env`, which docker-compose.yml mounts as
# `${IDEALISTA_DATA_DIR:-./data}`. Compose is the reader that decides where the
# app's state really is, so this file does not keep a second opinion: it parses
# neither docker-compose.yml nor `.env`. A second parser of either can disagree
# with Compose, and each disagreement is a host tool looking at a directory
# the app is not using.
#
#   * The variable is named nowhere -> `<repo>/data`, without asking anybody.
#   * Its name occurs anywhere in `<repo>/.env`, comments included -> the
#     answer is whatever `docker compose config` binds at /app/data for the
#     `app` service, asked the way the deploy watcher runs Compose: from the
#     checkout, with `-f docker-compose.yml`. No Docker, no python3 to read
#     its answer, or an answer that is not exactly one existing host
#     directory: refused.
#   * That answer is taken only if it is, character for character, what the
#     ONE line of `.env` naming the variable says: `IDEALISTA_DATA_DIR=<it>`.
#     Whatever Compose derives from the process that asks - `~` from its
#     HOME, `${X}` from its environment, a relative path from where it runs,
#     an export or a switch that changes which env file is read - renders
#     something other than the text written. So every answer given is the
#     text of `.env`, and no two processes are ever given two.
#   * It is SET in this process's environment, empty included (so is
#     COMPOSE_ENV_FILES or COMPOSE_DISABLE_ENV_FILE, which decide whether .env
#     is read) -> refused. Compose would follow the export only when run from
#     this very process: the watcher under launchd would name another
#     directory. One home means `<repo>/.env`, which every process reads alike.
#
# Nothing here creates the directory and nothing falls back to `<repo>/data`
# when Compose's answer cannot be used.
#
# A linked git worktree is refused as a root: the stack is not started from
# one, so its `data/` is nobody's state.
#
# What this does not see: a second clone or a plain copy of the checkout (it
# looks exactly like the first), and an override file or another compose file
# that a person passes to Compose by hand. The watcher passes exactly the one
# file.
#
# The name is deliberately not DATA_DIR. `.env` is also the container's
# `env_file`, config.py reads DATA_DIR, and a host path must never become the
# path the app uses inside the container.
#
# Usage - run it, do not source it; a subprocess reads one complete version of
# this file even while git replaces it, and its exit status is the verdict:
#
#   data_dir="$(/bin/bash "${repo}/tools/autopilot/lib/data_dir.sh" "$repo")" || exit 1
#
# `<repo>` is the checkout Compose is run from, as an absolute path. Prints the
# directory on stdout. On any refusal prints nothing there, says why on stderr
# and exits 1.

set -u

IDEALISTA_DATA_DIR_KEY="IDEALISTA_DATA_DIR"
# What Compose takes from the environment that moves the mount (measured on
# v5.7; tests/test_data_dir_single_home.py asks it about the other switches).
IDEALISTA_DATA_DIR_ENVIRONMENT="IDEALISTA_DATA_DIR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE"

# Prints the first that is SET here, empty included: an exported empty value
# outranks .env in Compose, so `IDEALISTA_DATA_DIR=` renders ./data in this
# process while every other one mounts what .env names.
_data_dir_set_in_environment() {
    local name
    for name in $IDEALISTA_DATA_DIR_ENVIRONMENT; do
        if [ -n "${!name+set}" ]; then
            printf '%s\n' "$name"
            return 0
        fi
    done
    return 1
}

_data_dir_refuse() {
    printf 'data_dir: %s\n' "$*" >&2
    return 1
}

# Trailing slashes are not a different directory.
_data_dir_trim() {
    local path="$1"
    while [ "${#path}" -gt 1 ] && [ "${path%/}" != "$path" ]; do
        path="${path%/}"
    done
    printf '%s' "$path"
}

# Where the variable is named, if anywhere. Sets DATA_DIR_NAMED_IN to that
# place or to the empty string. Returns 1, having said why, when `.env` exists
# and cannot be read: then it cannot be ruled out.
#
# Nothing is parsed, and the question is deliberately coarse: it only decides
# whether Compose has to be asked. `.env` counts when the name occurs anywhere
# in it. The file is never sourced and no line of it is printed.
_data_dir_where_named() {
    local repo="$1" env_file="${1}/.env" hits
    DATA_DIR_NAMED_IN=""
    DATA_DIR_NAMED_TIMES=0

    [ -e "$env_file" ] || [ -L "$env_file" ] || return 0
    if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
        _data_dir_refuse "${env_file} exists but cannot be read, so it cannot be ruled out that it names ${IDEALISTA_DATA_DIR_KEY}"
        return 1
    fi
    # Bytes, not text (`-a`, LC_ALL=C): a NUL or a byte that is not valid in
    # the locale would otherwise make grep call the file binary and report
    # nothing of what is in it.
    hits="$(LC_ALL=C grep -a -c -F "$IDEALISTA_DATA_DIR_KEY" "$env_file")" || true
    # `grep -c` always prints a count. Anything else is a search that did not
    # run - no grep on a stripped PATH - and "could not look" is not "not
    # there": read as zero, it would answer for a file nobody searched.
    case "$hits" in
        '' | *[!0-9]*)
            _data_dir_refuse "${env_file} could not be searched for ${IDEALISTA_DATA_DIR_KEY} (grep gave no count), so it cannot be ruled out that it names it"
            return 1
            ;;
    esac
    if [ "$hits" != "0" ]; then
        DATA_DIR_NAMED_IN="$env_file"
        DATA_DIR_NAMED_TIMES="$hits"
    fi
    return 0
}

# Reads `docker compose config --format json` on stdin and prints the host
# directory bound at /app/data for the `app` service. Exits 1 unless that is
# exactly one bind with a source and nothing else is mounted on or under it.
_data_dir_bind_from_config() {
    python3 -c '
import json
import sys

try:
    config = json.load(sys.stdin)
    volumes = config["services"]["app"].get("volumes") or []
except Exception:
    sys.exit(1)
state = [
    v for v in volumes
    if isinstance(v, dict)
    and isinstance(v.get("target"), str)
    and (v["target"].rstrip("/") == "/app/data" or v["target"].startswith("/app/data/"))
]
if len(state) != 1:
    sys.exit(1)
mount = state[0]
source = mount.get("source")
if mount.get("type") != "bind" or mount["target"] != "/app/data":
    sys.exit(1)
if not isinstance(source, str) or not source or "\n" in source:
    sys.exit(1)
print(source)
'
}

# Asks Compose. Prints the host directory it would bind at /app/data for the
# `app` service, from the checkout `repo`. The rendered configuration carries
# every value of `.env`; it stays in a variable and a pipe, none of it printed.
idealista_compose_source() {
    local repo="${1:-}" compose_file="${2:-docker-compose.yml}"
    local docker="${DOCKER_BIN:-docker}" rendered source

    if ! command -v "$docker" >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        _data_dir_refuse "cannot ask Compose what it mounts at /app/data: '${docker}' or python3 is not on PATH"
        return 1
    fi
    if ! rendered="$(cd "$repo" && "$docker" compose -f "$compose_file" config --format json 2>/dev/null)"; then
        _data_dir_refuse "'${docker} compose -f ${compose_file} config' failed in ${repo}, so what Compose would mount at /app/data is unknown; run it there to see why"
        return 1
    fi
    if ! source="$(printf '%s' "$rendered" | _data_dir_bind_from_config)"; then
        _data_dir_refuse "${compose_file} in ${repo} does not bind exactly one host directory at /app/data for the app service"
        return 1
    fi
    printf '%s\n' "$(_data_dir_trim "$source")"
}

idealista_data_dir() {
    local repo="${1:-}" value exported

    if [ -z "$repo" ]; then
        _data_dir_refuse "usage: data_dir.sh <repository root>"
        return 1
    fi
    repo="$(_data_dir_trim "$repo")"
    # `./data` in docker-compose.yml is relative to the directory Compose is
    # run from. A relative root here would name a different directory from
    # every working directory, and one that does not exist names nothing.
    case "$repo" in
        /*) ;;
        *)
            _data_dir_refuse "the repository root must be an absolute path, got '${repo}'"
            return 1
            ;;
    esac
    if [ ! -d "$repo" ]; then
        _data_dir_refuse "the repository root '${repo}' is not an existing directory"
        return 1
    fi
    # The stack is started from one checkout, and that is where its state
    # is. A linked git worktree is a second tree of the same repository with a
    # `data/` of its own that no container mounts: a tool run from one would
    # look for the supervisor lock, the markers and the attachments in a
    # directory the deployment has never written. Git marks the difference: a
    # linked worktree's `.git` is a file, the checkout's own is a directory.
    if [ -f "${repo}/.git" ]; then
        _data_dir_refuse "'${repo}' is a linked git worktree (its .git is a file), and the deployment does not run from one: its state is not in '${repo}/data'. Run this tool from the checkout the stack was started in - the first line of 'git worktree list'"
        return 1
    fi

    if exported="$(_data_dir_set_in_environment)"; then
        _data_dir_refuse "${exported} is set in this process's environment (an empty value counts: Compose lets it outrank .env). Compose would follow it only when run from here: set ${IDEALISTA_DATA_DIR_KEY} in ${repo}/.env, its one home, and unset ${exported}"
        return 1
    fi
    _data_dir_where_named "$repo" || return 1
    if [ -z "$DATA_DIR_NAMED_IN" ]; then
        # What `${IDEALISTA_DATA_DIR:-./data}` renders with nothing set. Not
        # checked for existence: callers created `<repo>/data` on demand
        # before this file existed and still do.
        printf '%s/data\n' "$repo"
        return 0
    fi

    value="$(idealista_compose_source "$repo")" || return 1
    case "$value" in
        /) _data_dir_refuse "Compose would mount / at /app/data; refusing"; return 1 ;;
        *'$'*) _data_dir_refuse "Compose reports a state directory with a '\$' in it ('${value}'); refusing"; return 1 ;;
        /*) ;;
        *)
            _data_dir_refuse "Compose reports a state directory that is not an absolute path ('${value}')"
            return 1
            ;;
    esac
    if [ ! -d "$value" ]; then
        _data_dir_refuse "Compose would mount '${value}' at /app/data (${IDEALISTA_DATA_DIR_KEY} is named in ${DATA_DIR_NAMED_IN}), and that is not an existing directory; refusing to guess or to create it"
        return 1
    fi
    # Verified, not parsed: Compose decided, and this only checks that what it
    # decided is the text written - on the one line that names the variable.
    if [ "$DATA_DIR_NAMED_TIMES" != "1" ] ||
        ! LC_ALL=C grep -a -q -x -F -- "${IDEALISTA_DATA_DIR_KEY}=${value}" "$DATA_DIR_NAMED_IN"; then
        _data_dir_refuse "Compose would mount '${value}' at /app/data from here, and ${DATA_DIR_NAMED_IN} does not say exactly that on one line naming ${IDEALISTA_DATA_DIR_KEY} (it names it on ${DATA_DIR_NAMED_TIMES}). Another process may be given another directory: write one line, ${IDEALISTA_DATA_DIR_KEY}=/absolute/path, nothing else on it"
        return 1
    fi
    printf '%s\n' "$value"
    return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    idealista_data_dir "$@"
    exit $?
fi
