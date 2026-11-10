#!/bin/bash
# Nightly, verified PostgreSQL dump (#640).
#
# The database lives in a named Docker volume on Docker Desktop's disk image,
# which sits on an external SSD that Time Machine excludes. Until this script
# the only dump was a manual one. A dump only counts once it has been read back:
# it is written to a temp file, checked (non-empty and `pg_restore --list`
# succeeds), and only then renamed into place. A failed dump is deleted and
# never rotates an older one away.
#
# Usage:
#     bash tools/backup_db.sh                 # one dump + rotation
#     bash tools/backup_db.sh --verify FILE   # list a dump, print table count
#
# Environment: IDEALISTA_BACKUP_DIR (default $HOME/IdealistaRank-backups/daily),
# IDEALISTA_BACKUP_KEEP (default 14), DB_CONTAINER, DB_USER, DB_NAME, DOCKER_BIN
# (same names and defaults as tools/backup_attachments.sh; no .env is read).
# Status files next to the dumps: last_success, last_failure.
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

db_container="${DB_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-db}"
db_name="${DB_NAME:-idealista_universal}"
db_user="${DB_USER:-idealista}"
docker_bin="${DOCKER_BIN:-/usr/local/bin/docker}"
if ! command -v "$docker_bin" >/dev/null 2>&1; then
    docker_bin="docker"
fi

dest="${IDEALISTA_BACKUP_DIR:-$HOME/IdealistaRank-backups/daily}"
keep="${IDEALISTA_BACKUP_KEEP:-14}"

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# pg_restore --list through the container, dump on stdin. Prints the listing.
list_dump() {
    "$docker_bin" exec -i "$db_container" pg_restore --list < "$1"
}

count_tables() {
    # `|| true`: grep -c exits 1 on zero matches, which is a count, not an error.
    list_dump "$1" | grep -c ' TABLE DATA ' || true
}

if [[ "${1:-}" == "--verify" ]]; then
    file="${2:-}"
    if [[ -z "$file" || ! -s "$file" ]]; then
        echo "usage: $0 --verify <dump-file> (must exist and be non-empty)" >&2
        exit 2
    fi
    if ! listing="$(list_dump "$file")"; then
        echo "NOT a readable dump: $file" >&2
        exit 1
    fi
    tables="$(printf '%s\n' "$listing" | grep -c ' TABLE DATA ' || true)"
    echo "OK $file: $tables tables with data"
    exit 0
fi
if [[ $# -gt 0 ]]; then
    echo "usage: $0 [--verify <dump-file>]" >&2
    exit 2
fi

case "$keep" in
    ''|*[!0-9]*) echo "IDEALISTA_BACKUP_KEEP must be a positive integer, got '$keep'" >&2; exit 2 ;;
esac
# Base 10 on purpose: "00" passes the digit check and is zero, "08" is an
# octal error. A retention of zero would rotate out the dump just made.
keep=$((10#$keep))
if (( keep < 1 )); then
    echo "IDEALISTA_BACKUP_KEEP must be a positive integer, got '$keep'" >&2
    exit 2
fi

mkdir -p "$dest"
chmod 700 "$dest"

write_status() { # name, content
    local tmp
    tmp="$(mktemp "$dest/.status.XXXXXX")"
    printf '%s\n' "$2" > "$tmp"
    mv -f "$tmp" "$dest/$1"
}

tmp_dump=""
# A dump interrupted half-way (SIGTERM from launchd, a closed session) must
# not stay behind as a file that looks like work in progress forever.
cleanup() { [[ -n "$tmp_dump" ]] && rm -f "$tmp_dump"; return 0; }
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
fail() {
    local reason="$1"
    [[ -n "$tmp_dump" ]] && rm -f "$tmp_dump"
    echo "db-backup FAILED: $reason" >&2
    write_status last_failure "$(utc_now) $reason" || true
    exit 1
}

# Single instance: the autopilot's flock(2) helper (fd 9). A refused overlap is
# not a failed backup, so it leaves the status files alone.
# shellcheck source=autopilot/lib/lock.sh
source "$HERE/autopilot/lib/lock.sh"
if ! autopilot_acquire_lock "$dest/.backup.lock"; then
    echo "db-backup: another run holds $dest/.backup.lock; refusing to overlap" >&2
    exit 75
fi

running="$("$docker_bin" inspect -f '{{.State.Running}}' "$db_container" 2>/dev/null || true)"
if [[ "$running" != "true" ]]; then
    fail "container $db_container is not running (this script does not start it)"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$dest/idealista-$stamp.dump"
tmp_dump="$(mktemp "$dest/.idealista-tmp.XXXXXX")"

"$docker_bin" exec "$db_container" pg_dump -U "$db_user" -d "$db_name" --format=custom \
    > "$tmp_dump" || fail "pg_dump exited non-zero"
[[ -s "$tmp_dump" ]] || fail "pg_dump produced an empty file"
list_dump "$tmp_dump" > /dev/null || fail "pg_restore --list rejected the dump"
[[ ! -e "$final" ]] || fail "$final already exists"

chmod 600 "$tmp_dump"
mv "$tmp_dump" "$final"
tmp_dump=""

size="$(wc -c < "$final" | tr -d ' ')"
write_status last_success "$(utc_now) $(basename "$final") ${size} bytes"
echo "db-backup OK: $final (${size} bytes)"

# Retention, only after the new dump is verified and in place. Own pattern,
# own directory; pre-merge-*.dump and anything else never match.
names=()
for f in "$dest"/idealista-*.dump; do
    [[ -f "$f" ]] || continue
    b="$(basename "$f")"
    if [[ "$b" =~ ^idealista-[0-9]{8}T[0-9]{6}Z\.dump$ ]]; then
        names+=("$b")
    fi
done
total=${#names[@]}
if (( total > keep )); then
    # Names sort chronologically; delete the oldest total-keep.
    sorted=()
    while IFS= read -r line; do sorted+=("$line"); done < <(printf '%s\n' "${names[@]}" | sort)
    for ((i = 0; i < total - keep; i++)); do
        rm -f -- "$dest/${sorted[$i]}"
        echo "db-backup: rotated out ${sorted[$i]}"
    done
fi
