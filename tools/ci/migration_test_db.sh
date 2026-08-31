#!/bin/bash
# A throwaway PostgreSQL for tests/test_postgres_migrations.py — in Docker on
# the Mac mini, reached from this MacBook through an ssh tunnel.
#
# The scheme (owner, 2026-08-31): this project runs in Docker on the mini, and
# the MacBook is a client that reaches it over Tailscale. It needs no local
# PostgreSQL of any kind — not the deployment's, and not a test server either.
#
# That rule was written after a session pointed the migration tests at
# 127.0.0.1:5432, which on the MacBook is Postgres.app, the inbox-zero
# project's database server. Nothing was lost, and the cause was that the
# prohibition named no server that was reachable: CONTRIBUTING.md's
# `docker run` needs a Docker daemon, and the MacBook has none. It has one on
# the mini, over ssh, which is where every other container in this project
# already lives.
#
#   eval "$(tools/ci/migration_test_db.sh start)"
#   uv run pytest tests/test_postgres_migrations.py -v
#   tools/ci/migration_test_db.sh stop
#
# `start` prints the export line on stdout and everything else on stderr, so
# the eval above sets exactly one variable. Offline, `start` fails and says so:
# the honest fallback is CI, never a database on this machine.
#
# What it touches on the mini: one `docker run --rm` of the SAME
# postgres:15-alpine the deployment's `idealista-db` runs, under its own name,
# published on the mini's loopback only. It never speaks to `idealista-db`,
# never runs `docker compose`, and carries no compose labels, so
# tools/autopilot/lib/docker_cleanup.sh does not consider it and a deploy does
# not disturb it.

set -euo pipefail

HOST="${MIGRATION_TEST_DB_HOST:-macmini}"
SSH_USER="${MIGRATION_TEST_DB_SSH_USER:-ss}"
DOCKER="${MIGRATION_TEST_DB_DOCKER:-/usr/local/bin/docker}"
CONTAINER="${MIGRATION_TEST_DB_CONTAINER:-idealista-migtest}"
IMAGE="${MIGRATION_TEST_DB_IMAGE:-postgres:15-alpine}"
PORT="${MIGRATION_TEST_DB_PORT:-55432}"
DBNAME=migtest
SOCKET="${TMPDIR:-/tmp}/idealista-migtest-tunnel.sock"
READY_ATTEMPTS=40

log() { printf '%s\n' "$*" >&2; }
die() { log "migration_test_db: $*"; exit 1; }

remote() { ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_USER@$HOST" "$@"; }

check_names() {
    # The deployment's own database must be unreachable from here by
    # construction, not by care: a name or a port that could resolve to it is
    # refused before anything runs.
    case "$CONTAINER" in
        *-db|*_db|idealista-db)
            die "refusing container name '$CONTAINER': that is the deployment's database." ;;
    esac
    if [ "$PORT" = 5432 ] || [ "$PORT" = 5433 ] || [ "$PORT" = 5434 ]; then
        die "refusing port $PORT: 5434 is the mini's idealista-db and 5432 is Postgres.app. Use 55432."
    fi
}

start() {
    check_names
    remote true 2>/dev/null \
        || die "cannot reach $SSH_USER@$HOST. This project keeps no local database; run the migration tests when the mini is reachable, or let CI run them."
    remote "[ -x '$DOCKER' ]" \
        || die "$DOCKER not found on $HOST (the mini's non-interactive PATH has no /usr/local/bin)."

    if [ "$(remote "$DOCKER inspect -f '{{.State.Running}}' $CONTAINER 2>/dev/null || echo no")" != "true" ]; then
        remote "$DOCKER rm -f $CONTAINER >/dev/null 2>&1 || true"
        log "migration_test_db: starting $IMAGE as $CONTAINER on $HOST (127.0.0.1:$PORT)"
        remote "$DOCKER run -d --rm --name $CONTAINER \
            -e POSTGRES_USER=$DBNAME -e POSTGRES_PASSWORD=$DBNAME -e POSTGRES_DB=$DBNAME \
            -p 127.0.0.1:$PORT:5432 $IMAGE" >/dev/null \
            || die "could not start the container on $HOST."
    fi

    local attempt=1
    until remote "$DOCKER exec $CONTAINER pg_isready -U $DBNAME -q" 2>/dev/null; do
        [ "$attempt" -lt "$READY_ATTEMPTS" ] || die "$CONTAINER did not become ready on $HOST."
        attempt=$((attempt + 1))
        sleep 1
    done

    # One multiplexed tunnel, owned by a control socket so `stop` can close
    # exactly this one rather than pattern-matching somebody else's ssh.
    if ! ssh -S "$SOCKET" -O check "$SSH_USER@$HOST" >/dev/null 2>&1; then
        rm -f "$SOCKET"
        ssh -M -S "$SOCKET" -f -N -o ConnectTimeout=15 -o BatchMode=yes \
            -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
            -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$SSH_USER@$HOST" \
            || die "could not open the tunnel to $HOST:$PORT (is $PORT already bound here?)."
        log "migration_test_db: tunnel 127.0.0.1:$PORT -> $HOST:$PORT"
    fi

    log "migration_test_db: ready. Stop it with: tools/ci/migration_test_db.sh stop"
    printf 'export TEST_DATABASE_URL_POSTGRES=postgresql://%s:%s@127.0.0.1:%s/%s\n' \
        "$DBNAME" "$DBNAME" "$PORT" "$DBNAME"
}

stop() {
    check_names
    if ssh -S "$SOCKET" -O check "$SSH_USER@$HOST" >/dev/null 2>&1; then
        ssh -S "$SOCKET" -O exit "$SSH_USER@$HOST" >/dev/null 2>&1 || true
        log "migration_test_db: tunnel closed"
    fi
    rm -f "$SOCKET"
    if remote true 2>/dev/null; then
        remote "$DOCKER rm -f $CONTAINER >/dev/null 2>&1 || true"
        log "migration_test_db: removed $CONTAINER on $HOST"
    else
        log "migration_test_db: $HOST unreachable; $CONTAINER (if any) is --rm and holds no volume."
    fi
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    *) log "usage: $0 {start|stop}"; exit 64 ;;
esac
