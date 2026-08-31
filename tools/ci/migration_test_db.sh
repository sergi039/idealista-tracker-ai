#!/bin/bash
# A throwaway PostgreSQL cluster for tests/test_postgres_migrations.py.
#
# Those tests CREATE and DROP databases on whatever server
# TEST_DATABASE_URL_POSTGRES names, as whatever role it carries. Pointed at a
# disposable server that is exactly what it says on the tin. Pointed at a
# cluster somebody's real data lives in, it is a superuser DDL loop next to it
# — which is what happened on 2026-08-31, when a session needed a real
# PostgreSQL for migration 025 and started the one already on this Mac:
# Postgres.app on 5432, inbox-zero's database server.
#
# CONTRIBUTING.md's `docker run … postgres:15-alpine` on 55432 is still the
# documented server and is still correct. It is simply not always available:
# Docker Desktop was not running on the laptop that day, so the only real
# PostgreSQL within reach was the one that must not be used. This script is
# the answer that is always within reach — it runs `initdb` from the
# PostgreSQL binaries Postgres.app already ships and puts a brand-new cluster
# in a temporary directory. Same binaries, different cluster: it has its own
# data directory, its own port, no role but the current user, and nothing in
# it but the databases the tests create.
#
#   eval "$(tools/ci/migration_test_db.sh start)"
#   uv run pytest tests/test_postgres_migrations.py -v
#   tools/ci/migration_test_db.sh stop
#
# `start` prints the export line on stdout and everything else on stderr, so
# the eval above sets exactly one variable. CI needs none of this: it gets a
# PostgreSQL service container and sets both variables itself.

set -euo pipefail

PORT="${MIGRATION_TEST_DB_PORT:-55432}"
DATADIR="${MIGRATION_TEST_DB_DIR:-${TMPDIR:-/tmp}/idealista-migration-test-cluster}"
DATADIR="${DATADIR%/}"
MARKER="$DATADIR/.idealista-migration-test-cluster"
DBNAME=migtest

log() { printf '%s\n' "$*" >&2; }
die() { log "migration_test_db: $*"; exit 1; }

find_bindir() {
    # The repository's own PostgreSQL, in preference order: an explicit
    # override, whatever is on PATH, then the newest Postgres.app version.
    if [ -n "${MIGRATION_TEST_DB_BINDIR:-}" ]; then
        printf '%s' "${MIGRATION_TEST_DB_BINDIR%/}"
        return 0
    fi
    if command -v initdb >/dev/null 2>&1 && command -v pg_ctl >/dev/null 2>&1; then
        dirname "$(command -v initdb)"
        return 0
    fi
    local candidate
    candidate=$(ls -d /Applications/Postgres.app/Contents/Versions/*/bin 2>/dev/null \
        | sort -V | tail -1)
    [ -n "$candidate" ] && [ -x "$candidate/initdb" ] || return 1
    printf '%s' "$candidate"
}

start() {
    local bindir
    bindir=$(find_bindir) || die "no initdb found. Install PostgreSQL, or set MIGRATION_TEST_DB_BINDIR."

    if [ "$PORT" = 5432 ] || [ "$PORT" = 5433 ] || [ "$PORT" = 5434 ]; then
        die "refusing port $PORT: 5432 is inbox-zero's Postgres.app, 5434 is the mini's idealista-db. Use 55432."
    fi

    if [ -e "$DATADIR" ] && [ ! -e "$MARKER" ]; then
        die "$DATADIR exists and was not created by this script. Refusing to touch it."
    fi

    if [ ! -e "$MARKER" ]; then
        if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            die "port $PORT is already in use by something this script did not start."
        fi
        log "migration_test_db: initdb -> $DATADIR ($("$bindir/initdb" --version))"
        mkdir -p "$DATADIR"
        chmod 700 "$DATADIR"
        "$bindir/initdb" -D "$DATADIR" -U "$USER" --auth=trust --encoding=UTF8 >/dev/null
        : > "$MARKER"
    fi

    if ! "$bindir/pg_isready" -h 127.0.0.1 -p "$PORT" -q 2>/dev/null; then
        log "migration_test_db: starting on 127.0.0.1:$PORT"
        "$bindir/pg_ctl" -D "$DATADIR" -l "$DATADIR/server.log" \
            -o "-p $PORT -k '$DATADIR' -c listen_addresses=127.0.0.1" \
            -w start >/dev/null
    fi

    "$bindir/psql" -h 127.0.0.1 -p "$PORT" -U "$USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$DBNAME'" | grep -q 1 \
        || "$bindir/createdb" -h 127.0.0.1 -p "$PORT" -U "$USER" "$DBNAME"

    log "migration_test_db: ready. Stop it with: tools/ci/migration_test_db.sh stop"
    printf 'export TEST_DATABASE_URL_POSTGRES=postgresql://%s@127.0.0.1:%s/%s\n' \
        "$USER" "$PORT" "$DBNAME"
}

stop() {
    local bindir
    bindir=$(find_bindir) || die "no pg_ctl found."
    [ -e "$MARKER" ] || { log "migration_test_db: nothing to stop ($DATADIR is not ours)"; return 0; }
    if "$bindir/pg_isready" -h 127.0.0.1 -p "$PORT" -q 2>/dev/null; then
        "$bindir/pg_ctl" -D "$DATADIR" -m fast -w stop >/dev/null
    fi
    # Only ever a directory this script initdb'd: it carries our marker AND a
    # PG_VERSION, and `rm -rf` is the one command here that cannot be undone.
    [ -e "$MARKER" ] && [ -e "$DATADIR/PG_VERSION" ] \
        || die "$DATADIR does not look like our cluster. Not removing it."
    rm -rf "$DATADIR"
    log "migration_test_db: stopped and removed $DATADIR"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    *) log "usage: $0 {start|stop}"; exit 64 ;;
esac
