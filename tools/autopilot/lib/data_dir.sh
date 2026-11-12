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
# What it answers in this commit: `<repo>/data`. docker-compose.yml binds the
# literal `./data`, so that is where the app's state is, and nothing on this
# machine can make Compose mount another directory. The answer is therefore not
# computed from any configuration - there is none to read, and a reader that
# parsed docker-compose.yml or `.env` on its own could disagree with Compose
# about what they say.
#
# IDEALISTA_DATA_DIR is the name the directory will be configured under, in the
# commit that makes docker-compose.yml mount it; from then on this file asks
# Compose itself what it mounts. Until then a machine that already sets the
# variable expects something no part of the deployment does. Following it here
# would send the host tools to a directory the app is not using; ignoring it
# silently would let the mount switch to it unannounced on the deploy that
# brings that commit. So it is refused, wherever it is named: non-empty in the
# environment, or anywhere in `<repo>/.env` outside a comment line - any
# spelling at all, because recognising "the" spelling is the parsing this file
# does not do.
#
# A linked git worktree is refused as a root: the stack is not started from
# one, so its `data/` is nobody's state.
#
# What this does not see: a second clone or a plain copy of the checkout (it
# looks exactly like the first), a docker-compose.yml edited by hand to bind
# another directory at /app/data, or an override file that does. The seven scripts
# make the same assumption today; tests/test_data_dir_single_home.py holds the
# committed file to it by asking `docker compose config`.
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
# Nothing is parsed. The environment counts when the variable is non-empty.
# `.env` counts when the name occurs on any line that is not a comment line,
# whatever surrounds it - `export`, `KEY: value`, quotes, a longer name that
# contains it, another key's value. Each of those is refused rather than
# understood. The file is never sourced and no line of it is printed.
_data_dir_where_named() {
    local repo="$1" env_file="${1}/.env" hits
    DATA_DIR_NAMED_IN=""

    if [ -n "${IDEALISTA_DATA_DIR:-}" ]; then
        DATA_DIR_NAMED_IN="the environment"
        return 0
    fi

    [ -e "$env_file" ] || [ -L "$env_file" ] || return 0
    if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
        _data_dir_refuse "${env_file} exists but cannot be read, so it cannot be ruled out that it names ${IDEALISTA_DATA_DIR_KEY}"
        return 1
    fi
    # Bytes, not text (`-a`, LC_ALL=C): a NUL or a byte that is not valid in
    # the locale would otherwise make grep call the file binary and stop
    # printing its lines, and the name would go unseen. Counted, not `grep -q`:
    # both greps read their whole input, so neither is cut short by the other.
    hits="$(LC_ALL=C grep -a -v '^[[:space:]]*#' "$env_file" \
        | LC_ALL=C grep -a -c -F "$IDEALISTA_DATA_DIR_KEY")" || true
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
    fi
    return 0
}

idealista_data_dir() {
    local repo="${1:-}"

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

    _data_dir_where_named "$repo" || return 1
    if [ -n "$DATA_DIR_NAMED_IN" ]; then
        _data_dir_refuse "${IDEALISTA_DATA_DIR_KEY} is named in ${DATA_DIR_NAMED_IN}, but docker-compose.yml in this commit binds ./data and does not read the variable: the app's state is ${repo}/data whatever it says. Remove ${IDEALISTA_DATA_DIR_KEY} from ${DATA_DIR_NAMED_IN}; it takes effect from the commit that makes docker-compose.yml mount it"
        return 1
    fi

    # Not checked for existence: callers created `<repo>/data` on demand
    # before this file existed and still do.
    printf '%s/data\n' "$repo"
    return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    idealista_data_dir "$@"
    exit $?
fi
