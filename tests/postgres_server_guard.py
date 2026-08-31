"""`TEST_DATABASE_URL_POSTGRES` must name a server nobody else is using.

`tests/test_postgres_migrations.py` CREATEs and DROPs databases on whatever
server that variable names, as whatever role it carries. Pointed at a
disposable server that is exactly what it says on the tin. Pointed at a
cluster somebody's real data lives in, it is a superuser DDL loop next to it.

On 2026-08-31 a session needed a real PostgreSQL for migration 025 and started
the only one on this Mac: `open -a Postgres`, then `createdb -U ss
throwaway_nan_test` on 127.0.0.1:5432 — Postgres.app, which is the inbox-zero
project's database server and holds its live `inboxzero` database. Nothing was
lost; only the `throwaway_*` databases were created and dropped. What made it
happen is that the prohibition named no server that was actually reachable:
CONTRIBUTING.md's `docker run` needs a Docker daemon and the MacBook has none.
It has one on the mini, which is where every other container in this project
already lives — `tools/ci/migration_test_db.sh` raises the throwaway there and
tunnels it to 127.0.0.1:55432, so this machine still runs no database of its
own. This module is what makes the prohibition mechanical rather than
remembered.

The refusal is a **failure, never a skip**: a skip reads as success, which is
the whole reason `tests/skip_guard.py` exists.

What it can see is a cluster holding databases that are not this run's —
`inboxzero` and `ss` on Postgres.app, against `migtest` alone in the throwaway
cluster and `idealista_ci` alone in CI. What it cannot see is an *empty*
foreign cluster: there is nothing in it to recognise, and equally nothing in
it to lose. That is the honest limit of the check, and it is stated here so
that its absence is not read as coverage.
"""

# PostgreSQL's own three. Every cluster has them and none of them is anybody's
# data, so their presence says nothing about who the server belongs to.
MAINTENANCE_DATABASES = frozenset({"postgres", "template0", "template1"})

# The prefix `postgres_url` gives the databases it creates and drops. A run
# interrupted mid-test can leave one behind; that is our own litter, not a
# reason to refuse the next run.
TEST_DATABASE_PREFIX = "idealista_migration_test_"


def foreign_databases(names, target):
    """The databases on this server that are neither ours nor PostgreSQL's.

    `target` is the database named in `TEST_DATABASE_URL_POSTGRES` itself —
    the maintenance database the tests connect to in order to issue CREATE
    DATABASE. It is the server's reason for existing under our URL, so it is
    ours by definition whatever it is called (`migtest` locally,
    `idealista_ci` in CI).
    """
    return sorted(
        {
            name
            for name in names
            if name not in MAINTENANCE_DATABASES
            and name != target
            and not name.startswith(TEST_DATABASE_PREFIX)
        }
    )


def refusal(found, target):
    """The message for a server that is somebody else's, or None for ours."""
    if not found:
        return None
    return (
        "TEST_DATABASE_URL_POSTGRES points at a PostgreSQL server that holds "
        "databases which are not this test run's: "
        + ", ".join(found)
        + ". These tests CREATE and DROP databases on that server, so it must "
        "be a throwaway nobody else is using — 127.0.0.1:5432 is inbox-zero's "
        "Postgres.app and 127.0.0.1:5434 is the mini's idealista-db, and "
        "neither is one. This project keeps no local database at all: the "
        "throwaway server is a container on the mini, tunnelled to "
        "127.0.0.1:55432 and removed by `stop`:\n"
        '    eval "$(tools/ci/migration_test_db.sh start)"\n'
        "    uv run pytest tests/test_postgres_migrations.py -v\n"
        "    tools/ci/migration_test_db.sh stop\n"
        f"(the database named in the URL, {target!r}, is not the problem — "
        "the ones listed above are.)"
    )
