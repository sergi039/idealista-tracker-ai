#!/bin/bash
# Back up the database and the attachment bytes, in that order (issue #430).
#
# Until attachments existed, "back up this app" meant one `pg_dump` and the
# runbook said so. It does not any more: the ficha catastral the agency sent is
# a file under `data/attachments/`, it cannot be recomputed from anything, and a
# dump of the database restores rows that point at bytes nobody kept.
#
# **The order is the whole reason this is a script and not two commands.** The
# dump goes first and the bytes second, because the two failure directions are
# not equally bad:
#
#   * dump, then copy  -> a file uploaded in between is copied with no row
#                         referring to it. That is an orphan file: inert, and
#                         `utils/sweep_attachments.py` reclaims it.
#   * copy, then dump  -> a file uploaded in between has a ROW in the dump and
#                         no bytes in the archive. That is a download that 404s
#                         after a restore, and nothing in the restored system
#                         can tell it from a file somebody deleted.
#
# The same asymmetry the upload path is built on (`services/attachments.py`):
# always leave the harmless failure, never the confusing one.
#
# It refuses rather than guessing where to write, and it does not delete
# anything, ever.
#
# Usage, on the machine that holds the deployment:
#
#     bash tools/backup_attachments.sh ~/IdealistaRank-backups
#
set -euo pipefail

destination="${1:-}"
if [[ -z "$destination" ]]; then
    echo "usage: $0 <destination-directory>" >&2
    echo "  e.g. $0 ~/IdealistaRank-backups" >&2
    exit 2
fi

if [[ ! -d "$destination" ]]; then
    echo "no such directory: $destination" >&2
    echo "create it first -- this script does not decide where backups live" >&2
    exit 2
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
attachments_dir="${ATTACHMENTS_DIR:-$project_dir/data/attachments}"
stamp="$(date -u +%Y%m%d-%H%M%S)"

db_container="${DB_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-db}"
db_name="${DB_NAME:-idealista_universal}"
db_user="${DB_USER:-idealista}"
docker_bin="${DOCKER_BIN:-/usr/local/bin/docker}"
if ! command -v "$docker_bin" >/dev/null 2>&1; then
    docker_bin="docker"
fi

dump_path="$destination/attachments-backup-$stamp.dump"
archive_path="$destination/attachments-backup-$stamp.tar.gz"

# 1. The database FIRST. A row written after this point is simply not in the
#    backup; a row written before it will have its bytes picked up below.
echo "1/2  dumping $db_name from $db_container"
"$docker_bin" exec "$db_container" pg_dump -U "$db_user" -d "$db_name" -Fc \
    > "$dump_path"
echo "     -> $dump_path ($(du -h "$dump_path" | cut -f1))"

# 2. The bytes SECOND, and everything under the root: a file whose row is not
#    in the dump is inert, which is the failure this order chooses.
if [[ -d "$attachments_dir" ]]; then
    echo "2/2  archiving $attachments_dir"
    tar -czf "$archive_path" -C "$(dirname "$attachments_dir")" \
        "$(basename "$attachments_dir")"
    echo "     -> $archive_path ($(du -h "$archive_path" | cut -f1))"
else
    # Said out loud rather than passed over: "no attachments yet" and "the
    # directory moved" look identical in a log that stays silent.
    echo "2/2  no attachment directory at $attachments_dir -- nothing to archive"
fi

echo
echo "restore, in the same order:"
echo "  $docker_bin exec -i $db_container pg_restore -U $db_user -d $db_name --clean < $dump_path"
if [[ -f "$archive_path" ]]; then
    echo "  tar -xzf $archive_path -C $(dirname "$attachments_dir")"
fi
