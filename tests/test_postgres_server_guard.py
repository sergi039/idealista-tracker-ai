"""The migration tests must refuse a PostgreSQL server that is not theirs.

`tests/postgres_server_guard.py` holds the rule and the incident it comes
from. Two halves are asserted here, because either alone is the defect this
repository keeps rediscovering (#309): the rule's verdicts on the four real
clusters, and that `postgres_url` actually *calls* it — before its first
CREATE DATABASE, not after.

Nothing here needs a PostgreSQL server: the wiring half drives the fixture
with a fake engine that records the SQL it is given.
"""

import pytest

from tests import postgres_server_guard as guard

# The four clusters this rule has to be right about, by their real contents.
POSTGRES_APP = ["postgres", "template0", "template1", "inboxzero", "ss"]
THROWAWAY_CLUSTER = ["migtest", "postgres", "template0", "template1"]
CI_SERVICE_CONTAINER = ["idealista_ci", "postgres", "template0", "template1"]


def test_inbox_zeros_server_is_refused_and_the_databases_are_named():
    found = guard.foreign_databases(POSTGRES_APP, "throwaway_nan_test")
    assert found == ["inboxzero", "ss"]
    message = guard.refusal(found, "throwaway_nan_test")
    assert "inboxzero" in message
    # The peer session that hit this said the failure was not knowing the
    # helper existed while 5432 answered instantly, so the message names both
    # the port and the command rather than only the prohibition.
    assert "tools/ci/migration_test_db.sh" in message
    assert "55432" in message


def test_a_throwaway_cluster_is_accepted():
    assert guard.foreign_databases(THROWAWAY_CLUSTER, "migtest") == []
    assert guard.refusal([], "migtest") is None


def test_the_ci_service_container_is_accepted():
    """CI is on port 5432 too, so the rule cannot be a port number."""
    assert guard.foreign_databases(CI_SERVICE_CONTAINER, "idealista_ci") == []


def test_our_own_leftover_database_is_not_a_stranger():
    """An interrupted run leaves one behind; that is our litter, not a refusal."""
    names = THROWAWAY_CLUSTER + ["idealista_migration_test_abc123def456"]
    assert guard.foreign_databases(names, "migtest") == []


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, names, executed):
        self._names = names
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self._executed.append(sql)
        if "pg_database" in sql:
            return _FakeResult(self._names)
        return _FakeResult([])


class _FakeEngine:
    """Records every statement, so the test can assert what did NOT run."""

    def __init__(self, names):
        self._names = names
        self.executed = []

    def connect(self):
        return _FakeConnection(self._names, self.executed)

    def dispose(self):
        pass


def _drive_the_fixture(monkeypatch, names, url):
    import tests.test_postgres_migrations as module

    monkeypatch.setenv("TEST_DATABASE_URL_POSTGRES", url)
    engine = _FakeEngine(names)
    monkeypatch.setattr(module, "create_engine", lambda *a, **k: engine)
    function = getattr(module.postgres_url, "__wrapped__", module.postgres_url)
    return engine, function()


def test_the_fixture_refuses_before_it_creates_anything(monkeypatch):
    """Removing the guard call from `postgres_url` turns this red.

    The `executed` assertion is the point: a refusal that arrived after the
    CREATE would still raise, and would already have written to somebody
    else's cluster.
    """
    engine, generator = _drive_the_fixture(
        monkeypatch, POSTGRES_APP, "postgresql://ss@localhost:5432/throwaway_nan_test"
    )
    with pytest.raises(pytest.fail.Exception) as failure:
        next(generator)
    assert "inboxzero" in str(failure.value)
    assert not [sql for sql in engine.executed if "CREATE DATABASE" in sql]


def test_the_fixture_creates_and_drops_on_a_throwaway_server(monkeypatch):
    engine, generator = _drive_the_fixture(
        monkeypatch, THROWAWAY_CLUSTER, "postgresql://ss@127.0.0.1:55432/migtest"
    )
    database_url = next(generator)
    assert "/idealista_migration_test_" in database_url
    assert any("CREATE DATABASE" in sql for sql in engine.executed)
    generator.close()
    assert any("DROP DATABASE" in sql for sql in engine.executed)
