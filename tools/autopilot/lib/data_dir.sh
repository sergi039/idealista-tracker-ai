#!/bin/bash
# Where this deployment keeps its state: one answer for every host-side tool
# (#639).
#
# The app container persists exactly one directory, mounted at /app/data: the
# mail cursor and its retry list, the Google spend ledger, attachments, score
# snapshots, the in-flight markers. On the host that directory used to be
# spelled `${REPO_DIR}/data` in seven scripts and `./data` in docker-compose.yml
# - eight copies of one fact, and the fact was "inside the git working copy",
# which is how a `pytest` run in the checkout came to overwrite the production
# mail cursor (2026-11-09, #627).
#
# So the host path has one home: IDEALISTA_DATA_DIR in the checkout's `.env`,
# the file Compose already interpolates docker-compose.yml from. Compose reads
# it for the bind mount; everything else on the host asks this file. Unset, the
# answer is `<repo>/data`, which is what every caller computed before.
#
# The name is deliberately not DATA_DIR. `.env` is also the container's
# `env_file`, config.py reads DATA_DIR, and a host path must never become the
# path the app uses inside the container.
#
# Precedence is Compose's own, so the mount and the tools cannot disagree: the
# process environment first, then the `.env` line. A variable that is set in
# both places to different values is refused rather than resolved - Compose
# would quietly take the environment's, and "quietly" is the problem.
#
# An explicit value has to be an absolute path to a directory that exists.
# Nothing here creates it and nothing falls back to `<repo>/data` when it is
# wrong: a typo that silently became an empty directory would be a second,
# empty copy of the deployment's state.
#
# Usage - run it, do not source it; a subprocess reads one complete version of
# this file even while git replaces it, and its exit status is the verdict:
#
#   data_dir="$(/bin/bash "${repo}/tools/autopilot/lib/data_dir.sh" "$repo")" || exit 1
#
# Prints the directory on stdout. On any refusal prints nothing there, says why
# on stderr and exits 1.

set -u

IDEALISTA_DATA_DIR_KEY="IDEALISTA_DATA_DIR"

_data_dir_refuse() {
    printf 'data_dir: %s\n' "$*" >&2
    return 1
}

# The value of the one key in an env file. Sets DATA_DIR_FILE_SET (0/1) and
# DATA_DIR_FILE_VALUE. The file is never sourced and never printed: only lines
# that name this key are read, and only one spelling of them is accepted,
#
#     IDEALISTA_DATA_DIR=/absolute/path
#
# optionally in one pair of quotes. Compose's own parser accepts more (spaces
# around `=`, `export`, inline comments, interpolation), and each of those is
# a way for this reader and Compose to read the same line differently - so
# anything else that mentions the key is refused instead of interpreted.
_data_dir_read_env_file() {
    local env_file="$1" lines count line value
    DATA_DIR_FILE_SET=0
    DATA_DIR_FILE_VALUE=""

    [ -e "$env_file" ] || return 0
    if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
        _data_dir_refuse "${env_file} exists but cannot be read, so it cannot be ruled out that it names ${IDEALISTA_DATA_DIR_KEY}"
        return 1
    fi

    lines="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${IDEALISTA_DATA_DIR_KEY}[[:space:]]*=" "$env_file" || true)"
    [ -n "$lines" ] || return 0
    count="$(printf '%s\n' "$lines" | wc -l | tr -d '[:space:]')"
    if [ "$count" != "1" ]; then
        _data_dir_refuse "${env_file} sets ${IDEALISTA_DATA_DIR_KEY} ${count} times; keep exactly one line"
        return 1
    fi

    line="${lines%$'\r'}"
    case "$line" in
        "${IDEALISTA_DATA_DIR_KEY}="*) value="${line#"${IDEALISTA_DATA_DIR_KEY}="}" ;;
        *)
            _data_dir_refuse "${env_file}: write the line as ${IDEALISTA_DATA_DIR_KEY}=/absolute/path (no spaces, no 'export')"
            return 1
            ;;
    esac
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    case "$value" in
        *[[:space:]\"\'\#\$\\]*)
            _data_dir_refuse "${env_file}: the value of ${IDEALISTA_DATA_DIR_KEY} must be a plain path - no spaces, quotes inside, '#', '\$' or backslashes"
            return 1
            ;;
    esac

    DATA_DIR_FILE_SET=1
    DATA_DIR_FILE_VALUE="$value"
    return 0
}

# Trailing slashes are not a different directory.
_data_dir_trim() {
    local path="$1"
    while [ "${#path}" -gt 1 ] && [ "${path%/}" != "$path" ]; do
        path="${path%/}"
    done
    printf '%s' "$path"
}

idealista_data_dir() {
    local repo="${1:-}" env_set=0 env_value="" value=""

    if [ -z "$repo" ]; then
        _data_dir_refuse "usage: data_dir.sh <repository root>"
        return 1
    fi
    repo="$(_data_dir_trim "$repo")"

    if [ -n "${IDEALISTA_DATA_DIR+set}" ]; then
        env_set=1
        env_value="$(_data_dir_trim "$IDEALISTA_DATA_DIR")"
    fi

    _data_dir_read_env_file "${repo}/.env" || return 1
    DATA_DIR_FILE_VALUE="$(_data_dir_trim "$DATA_DIR_FILE_VALUE")"

    if [ "$env_set" = "1" ] && [ "$DATA_DIR_FILE_SET" = "1" ] \
        && [ "$env_value" != "$DATA_DIR_FILE_VALUE" ]; then
        _data_dir_refuse "${IDEALISTA_DATA_DIR_KEY} is '${env_value}' in the environment and '${DATA_DIR_FILE_VALUE}' in ${repo}/.env; Compose would take the environment's. Unset one of them"
        return 1
    fi

    if [ "$env_set" = "1" ]; then
        value="$env_value"
    else
        value="$DATA_DIR_FILE_VALUE"
    fi

    # Empty is unset, exactly as `${IDEALISTA_DATA_DIR:-./data}` reads it in
    # docker-compose.yml. The default is not checked for existence: callers
    # created `<repo>/data` on demand before this file existed and still do.
    if [ -z "$value" ]; then
        printf '%s\n' "${repo}/data"
        return 0
    fi

    case "$value" in
        /) _data_dir_refuse "${IDEALISTA_DATA_DIR_KEY} must not be /"; return 1 ;;
        /*) ;;
        *)
            _data_dir_refuse "${IDEALISTA_DATA_DIR_KEY} must be an absolute path, got '${value}'"
            return 1
            ;;
    esac
    if [ ! -d "$value" ]; then
        _data_dir_refuse "${IDEALISTA_DATA_DIR_KEY}='${value}' is not an existing directory; refusing to guess or to create it"
        return 1
    fi

    printf '%s\n' "$value"
    return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    idealista_data_dir "$@"
    exit $?
fi
