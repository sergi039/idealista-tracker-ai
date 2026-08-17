#!/bin/bash
# What a build leaves behind, removed at the one moment it is provably dead:
# after the new image is serving, the page check has passed and the previous
# image is safe under its rollback tag. Never before, and never on the rollback
# path - there the old image is the thing being restored.
#
# Measured on 2026-08-17, both machines, before this existed:
#
#   mini    27 images, 6.03GB, 1% reclaimable, build cache 0B - and three
#           exited one-off containers from backfills a deploy had killed,
#           pinning three image manifests at ~17.5MB of unique bytes each,
#           plus a "Found orphan containers" warning in every deploy since.
#   laptop  17 images, 10.5GB of them referenced by nothing, and 20.24GB of
#           build cache, all of it reclaimable.
#
# The two machines therefore fail differently, and one rule covers both: the
# mini leaks *containers* (with the containerd snapshotter an untagged image is
# collected as soon as nothing holds it, which is why ~21 deploys a day leave
# no pile), the laptop leaks *build cache*, which `docker image prune` does not
# touch at all. A single `docker image prune -f` - the obvious one-liner -
# would have reclaimed 86MB on the mini and nothing whatsoever on the laptop.
#
# Three things this must never become:
#
#   docker image prune -a       -a removes every image no *container* uses, and
#                               ${IMAGE}:autopilot-rollback is precisely that:
#                               a tag on an image nothing runs. It is the
#                               rollback. Without -a only untagged images go,
#                               so both rollback tags survive by construction.
#   docker system prune         the daemon is shared. On the mini vsdb,
#                               virto-property and inbox-zero all own images
#                               and stopped containers; on the laptop a
#                               worktree stack owns a whole second project.
#                               This project cleans up after itself and after
#                               nothing else - that is what the compose project
#                               label is for, and it is read off the running
#                               container rather than guessed from the
#                               directory name, because COMPOSE_PROJECT_NAME
#                               and COMPOSE_CONTAINER_PREFIX live in the
#                               project's .env (docs/DEV_RULES.md).
#   compose up --remove-orphans compose calls a one-off `docker compose run`
#                               sibling an orphan, and that is exactly where a
#                               long backfill deliberately lives, precisely
#                               because a deploy recreates the app container
#                               (#338). --remove-orphans would kill a *running*
#                               job - the opposite of what #283 exists for.
#
# Usage (sourced, never executed - it defines and returns):
#   . "${repo_root}/tools/autopilot/lib/docker_cleanup.sh"
#   deploy_cleanup "$app_container" | while IFS= read -r l; do log "  $l"; done
#
# Unlike render_check.sh, a missing or half-loaded copy of this file is not
# fatal to a deploy, and the two must not be "harmonised" into one policy. A
# page check that did not run would read as one that passed, so that contract
# refuses to run without it; housekeeping that did not run only leaves garbage,
# and failing a healthy deploy over uncollected garbage is the tail wagging the
# dog. Callers therefore guard the call and carry on.

# The switch. Anything other than 0 leaves cleanup on; 0 turns it off and says
# so in the log, because a step that silently stopped running is how a machine
# fills up while its deploy log reads exactly as it always did.
: "${DEPLOY_CLEANUP:=1}"

# How long an exited one-off container is kept before it is swept. It is not
# tidiness that sets this number: a `docker compose run` sibling killed by the
# deploy that just ran is seconds old, and its container log is the only record
# of how far the job got - the in-flight marker says a run was interrupted, not
# what it had finished. So a corpse is given a day for somebody to read it.
: "${DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H:=24}"

# Ceiling for the build cache, handed to `docker buildx prune`, which evicts
# least-recently-used records down to it. Empty leaves the cache alone
# entirely. 5GB is far above what one project's layers need (the dependency
# layer here is ~500MB) and far below the 20.24GB measured above, so a build
# keeps every record it actually reuses.
: "${DEPLOY_CLEANUP_BUILD_CACHE_MAX:=5GB}"

# RFC3339 (docker's `.State.FinishedAt`) to epoch seconds, on BSD date and GNU
# date alike - the deployers run on macOS, the tests on CI's Linux. Docker
# writes 0001-01-01T00:00:00Z for a container that never ran; that is not a
# time, and neither is a string neither date can parse. Both return failure,
# and the caller keeps the container: a probe that could not answer must not
# read as "old enough to delete".
_deploy_cleanup_epoch() {
    _dc_ts="${1%%.*}"
    _dc_ts="${_dc_ts%Z}"
    case "$_dc_ts" in
        '' | 0001-01-01T00:00:00) return 1 ;;
    esac
    date -u -j -f '%Y-%m-%dT%H:%M:%S' "$_dc_ts" '+%s' 2>/dev/null && return 0
    date -u -d "${_dc_ts}Z" '+%s' 2>/dev/null && return 0
    return 1
}

# Exited one-off siblings of this project, and nothing else. Two filters carry
# the whole safety argument and both are load bearing: `status=exited` because
# a running or merely created container may be somebody's job, and the oneoff
# label because the stack's own app/db/redis containers carry the project label
# too - a stopped idealista-db matches on project alone, and removing it is not
# housekeeping.
_deploy_cleanup_oneoffs() {
    local _dc_project="$1"
    _dc_now="$(date -u '+%s' 2>/dev/null || printf '')"
    if [ -z "$_dc_now" ]; then
        printf 'cleanup: the clock did not answer - exited one-off containers kept\n'
        return 0
    fi
    # Validated rather than trusted: "24h" in this variable is an arithmetic
    # syntax error, and under the watcher's `set -e` that would abort mid-sweep
    # instead of saying what was wrong with it.
    case "$DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H" in
        '' | *[!0-9]*)
            printf 'cleanup: DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H=%s is not a whole number of hours - using 24\n' \
                "$DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H"
            DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H=24
            ;;
    esac
    _dc_min_age=$((DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H * 3600))
    _dc_rows="$(docker ps -a \
        --filter "label=com.docker.compose.project=${_dc_project}" \
        --filter 'status=exited' \
        --format '{{.ID}} {{.Names}}' 2>/dev/null || printf '')"
    _dc_removed=0
    _dc_kept=0
    if [ -n "$_dc_rows" ]; then
        # A here-string, not a pipe: bash 3.2 has no lastpipe, and a `while`
        # on the right of a pipe counts in a subshell whose totals die with it.
        while IFS= read -r _dc_row; do
            [ -n "$_dc_row" ] || continue
            _dc_id="${_dc_row%% *}"
            _dc_name="${_dc_row#* }"
            _dc_meta="$(docker inspect "$_dc_id" --format \
                '{{index .Config.Labels "com.docker.compose.oneoff"}} {{.State.FinishedAt}}' \
                2>/dev/null || printf '')"
            _dc_oneoff="${_dc_meta%% *}"
            _dc_finished="${_dc_meta#* }"
            case "$_dc_oneoff" in
                true | True | TRUE | 1) ;;
                *) continue ;;
            esac
            _dc_finished_at="$(_deploy_cleanup_epoch "$_dc_finished" || printf '')"
            if [ -z "$_dc_finished_at" ]; then
                _dc_kept=$((_dc_kept + 1))
                continue
            fi
            if [ "$((_dc_now - _dc_finished_at))" -lt "$_dc_min_age" ]; then
                _dc_kept=$((_dc_kept + 1))
                continue
            fi
            if docker rm "$_dc_id" >/dev/null 2>&1; then
                _dc_removed=$((_dc_removed + 1))
                printf 'cleanup: removed exited one-off container %s\n' "$_dc_name"
            else
                printf 'cleanup: could not remove exited one-off container %s\n' "$_dc_name"
            fi
        done <<<"$_dc_rows"
    fi
    if [ "$_dc_kept" -gt 0 ]; then
        printf 'cleanup: %d exited one-off container(s) kept - younger than %sh, or their end time did not parse\n' \
            "$_dc_kept" "$DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H"
    fi
    return 0
}

# Untagged images of this project. No -a, ever - see the header. An image a
# container still references is skipped by docker itself, which is what keeps
# the pinned manifests of a one-off corpse alive until the sweep above has
# removed the corpse.
_deploy_cleanup_images() {
    local _dc_project="$1"
    if _dc_out="$(docker image prune -f \
        --filter "label=com.docker.compose.project=${_dc_project}" 2>&1)"; then
        _dc_freed="$(printf '%s\n' "$_dc_out" | sed -n 's/^Total reclaimed space: //p' | tail -1)"
        printf 'cleanup: untagged %s images pruned, reclaimed %s\n' \
            "$_dc_project" "${_dc_freed:-an unreported amount}"
    else
        printf 'cleanup: pruning untagged %s images failed: %s\n' \
            "$_dc_project" "$(printf '%s' "$_dc_out" | head -1)"
    fi
    return 0
}

# The build cache, capped rather than emptied. --max-used-space is a recent
# buildx flag; where it is missing the command fails, and the answer is to
# report that and stop. It must never fall back to a bare `docker buildx prune`
# - that empties the cache instead of capping it, which is a silent escalation
# from "keep 5GB" to "keep nothing" and makes every later build cold.
_deploy_cleanup_build_cache() {
    if [ -z "$DEPLOY_CLEANUP_BUILD_CACHE_MAX" ]; then
        printf 'cleanup: build cache left alone (DEPLOY_CLEANUP_BUILD_CACHE_MAX is empty)\n'
        return 0
    fi
    if _dc_out="$(docker buildx prune -f \
        --max-used-space "$DEPLOY_CLEANUP_BUILD_CACHE_MAX" 2>&1)"; then
        _dc_freed="$(printf '%s\n' "$_dc_out" | sed -n 's/^Total:[[:space:]]*//p' | tail -1)"
        printf 'cleanup: build cache capped at %s, freed %s\n' \
            "$DEPLOY_CLEANUP_BUILD_CACHE_MAX" "${_dc_freed:-nothing}"
    else
        printf 'cleanup: capping the build cache at %s failed: %s\n' \
            "$DEPLOY_CLEANUP_BUILD_CACHE_MAX" "$(printf '%s' "$_dc_out" | head -1)"
    fi
    return 0
}

# The entry point. Takes the app container the caller has already resolved -
# both deployers know it, and it is the one place the compose project name can
# be read rather than guessed. Always returns 0: every step reports what it did
# or why it could not, and none of them is entitled to fail a deploy that is
# already serving.
deploy_cleanup() {
    _dc_container="${1:-}"
    if [ "${DEPLOY_CLEANUP}" = "0" ]; then
        printf 'cleanup: skipped (DEPLOY_CLEANUP=0)\n'
        return 0
    fi
    if [ -z "$_dc_container" ]; then
        printf 'cleanup: skipped - no app container was named\n'
        return 0
    fi
    _dc_project="$(docker inspect "$_dc_container" \
        --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null \
        | head -1 | tr -d '\r' || printf '')"
    case "$_dc_project" in
        '' | '<no value>')
            printf 'cleanup: skipped - %s carries no compose project label\n' "$_dc_container"
            return 0
            ;;
    esac
    _deploy_cleanup_oneoffs "$_dc_project"
    _deploy_cleanup_images "$_dc_project"
    _deploy_cleanup_build_cache
    return 0
}
