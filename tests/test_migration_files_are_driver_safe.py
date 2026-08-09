"""Migration SQL must survive psycopg2 before PostgreSQL ever sees it.

Two characters break a migration at the driver layer, and both fail in ways
that point nowhere near the real cause:

* a lone percent sign is read as a parameter marker, and the statement dies
  with "immutabledict is not a sequence" -- a SQLAlchemy internals error with
  no mention of SQL;
* a NUL byte truncates the query string, so psycopg2 sends whatever preceded
  it. A file whose NUL sits inside the leading comment block sends nothing at
  all and raises "can't execute an empty query".

Neither is caught by reading the file, and the comment blocks in this
directory are long enough to hide either. Both were hit while writing
migration 014 -- once in a comment that was *describing* the NUL problem, once
in a comment describing the percent problem.

These checks need no database, so unlike tests/test_postgres_migrations.py
they run on every suite, including the one that gates a push.
"""

from pathlib import Path

import pytest

MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))


def test_the_migration_directory_is_not_empty():
    """Guards the parametrisation below against silently covering nothing."""
    assert MIGRATIONS, "no migration files found: the glob or the layout changed"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_holds_no_nul_byte(path):
    raw = path.read_bytes()
    offset = raw.find(b"\x00")
    assert offset == -1, (
        f"{path.name} contains a NUL byte at offset {offset}: psycopg2 truncates "
        "the query there and sends only what came before it"
    )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_has_no_unescaped_percent_sign(path):
    """Every percent sign must be doubled, comments included.

    psycopg2 does not parse SQL: it scans the whole string for its parameter
    marker, so a percent sign inside a `--` comment fails the statement exactly
    as one in an expression would.
    """
    text = path.read_text(encoding="utf-8")
    offenders = []
    index = 0
    while (index := text.find("%", index)) != -1:
        if text[index : index + 2] == "%%":
            index += 2
            continue
        line = text.count("\n", 0, index) + 1
        offenders.append(f"line {line}: {text.splitlines()[line - 1].strip()!r}")
        index += 1

    assert not offenders, (
        f"{path.name} has a lone percent sign; double it or reword:\n"
        + "\n".join(offenders)
    )
