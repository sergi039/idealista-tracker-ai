#!/bin/bash
# The page-check contract: a deploy is healthy when a page renders, not when
# healthz answers (#283, unified under one name by #292).
#
# /api/healthz reports database, scheduler and schema and renders no template,
# so it cannot see a broken one. On 2026-08-14 a TemplateSyntaxError turned
# every /properties/<id> into a redirect for 15 minutes while healthz stayed
# green - routes/main_routes.py catches the error and redirects. So a build is
# not accepted until a page that really renders a template answers 200, and a
# 3xx is the failure being looked for rather than a detail to follow.
#
# Both deployers reached that rule the same day and wrote it down twice:
# AUTOPILOT_PAGE_URL in tools/autopilot/deploy_watcher.sh and
# AUTO_REBUILD_RENDER_PATH in .githooks/post-merge. Two names for one idea is
# one too many - the shape of defect that eventually ships half-changed - so
# the page, the join and what counts as a pass live here, once, and both read
# them from here.
#
# What is deliberately NOT shared is where each caller gets its origin: the
# hook asks `docker compose port` (APP_HOST_PORT lives in the project .env),
# the watcher takes the origin of its health URL (so a harness pointing healthz
# at a stub points the page check at the same stub). Those answer "which stack
# is this", not "what proves it renders", and they cannot be unified without
# breaking one of them.
#
# Usage:
#   . "${repo_root}/tools/autopilot/lib/render_check.sh"
#   url="$(deploy_render_url "$origin")"   # empty when the check is turned off
#   if deploy_render_ok "$url"; then ... fi # $DEPLOY_RENDER_STATUS holds the code
#
# Sourced, never executed: it defines and returns.

# The page. It has to render a template - that is the whole point - and
# /properties is the one listing surface (CLAUDE.md). Empty turns the check
# off; both callers then say the build is unverified rather than saying
# nothing, because a check that did not run must never read as one that passed.
: "${DEPLOY_RENDER_PATH=/properties}"

# Ceiling for the fetch. The generous end of what the two callers used (the
# watcher polled at 10s, the hook fetched at 15s): a timeout too short is a
# false failure, and a false failure here rolls back a good build.
: "${DEPLOY_RENDER_MAX_TIME:=15}"

# The status code of the last deploy_render_ok, for the caller's log line.
DEPLOY_RENDER_STATUS=""

# scheme://host[:port] of a URL, dropping any path. The watcher has only a
# health URL to start from.
deploy_render_origin() {
    local url="$1" rest
    case "$url" in
        *://*)
            rest="${url#*://}"
            printf '%s://%s' "${url%%://*}" "${rest%%/*}"
            ;;
        *)
            printf '%s' "${url%%/*}"
            ;;
    esac
    return 0
}

# The URL to fetch, from an origin (or an origin plus a path prefix, which is
# what AUTO_REBUILD_BASE_URL may carry). Prints nothing when the check is off,
# so `[ -z "$url" ]` is how a caller detects that.
deploy_render_url() {
    local base="${1%/}" path="$DEPLOY_RENDER_PATH"
    [ -n "$path" ] || return 0
    case "$path" in
        /*) ;;
        *) path="/${path}" ;;
    esac
    printf '%s%s' "$base" "$path"
    return 0
}

# True only on 200. No -L and no -f: a redirect is the failure being looked
# for, so the status has to be read rather than followed or swallowed.
deploy_render_ok() {
    DEPLOY_RENDER_STATUS="$(
        curl -sS -o /dev/null -w '%{http_code}' \
            --max-time "$DEPLOY_RENDER_MAX_TIME" "$1" 2>/dev/null || true
    )"
    [ "$DEPLOY_RENDER_STATUS" = "200" ]
}

# Names of the pre-#292 variables that are still set in this environment. They
# are no longer read - ignoring them can only make the check stricter, never
# weaker - but an operator who set one deliberately deserves to be told, so
# each caller logs whatever this prints.
deploy_render_legacy_vars() {
    local name
    for name in AUTOPILOT_PAGE_URL AUTO_REBUILD_RENDER_PATH; do
        if [ -n "${!name+set}" ]; then
            printf '%s\n' "$name"
        fi
    done
    return 0
}
