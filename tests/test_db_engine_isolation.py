"""Guard: every test app must own a private in-memory database.

For a long time the suite only *looked* isolated. Nearly every module's `app`
fixture read:

    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

The third line never did anything. Flask-SQLAlchemy 3.x builds the engine
inside `init_app()`, which `create_app()` calls, so by the time the config key
is reassigned the engine is already bound -- to `TEST_DATABASE_URL`, which was
`sqlite:///test.db`, i.e. one shared on-disk file (`instance/test.db`) for the
whole run. Isolation rested entirely on `db.drop_all()` in fixture teardown: a
crashed test left its rows behind for the next module, and late in a run the
contention was enough to fail `drop_all()` with 'database is locked'.

The fix routes the URL through the environment, which `create_app()` reads
*before* `init_app()`. These tests pin both halves so the override cannot
silently die again: that the engine really is in-memory, and that no fixture
goes back to setting `SQLALCHEMY_DATABASE_URI` after the fact.
"""

import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app import _is_in_memory_sqlite, create_app, db
from tests import TEST_DATABASE_URL, setup_test_environment

TESTS_DIR = Path(__file__).parent

# `sqlite://` leaves database=None; `sqlite:///:memory:` sets it to ":memory:".
# Anything else is a file on disk.
IN_MEMORY_DATABASES = (None, "", ":memory:")

# The dead pattern this module exists to keep out of tests/.
URI_REASSIGNMENT = re.compile(r"""config\[["']SQLALCHEMY_DATABASE_URI["']\]\s*=""")


def _is_in_memory(url) -> bool:
    return url.get_backend_name() == "sqlite" and url.database in IN_MEMORY_DATABASES


def test_test_database_url_is_in_memory():
    """The constant every fixture inherits must not name a file."""
    assert _is_in_memory(make_url(TEST_DATABASE_URL)), (
        f"TEST_DATABASE_URL is {TEST_DATABASE_URL!r}; a file-backed sqlite URL "
        "puts the whole suite back on one shared database."
    )


def test_standard_fixture_pattern_binds_an_in_memory_engine():
    """The assertion the old override only pretended to make."""
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        url = db.engine.url
        assert _is_in_memory(url), (
            f"db.engine.url is {url!r}, not an in-memory database. The URL has "
            "to reach create_app() through DATABASE_URL; setting "
            "SQLALCHEMY_DATABASE_URI afterwards is too late."
        )


def test_reassigning_the_uri_after_create_app_does_not_move_the_engine():
    """Pin the Flask-SQLAlchemy behaviour that made the old override dead.

    If a future release starts honouring a late `SQLALCHEMY_DATABASE_URI`, this
    test fails and the comment above stops being true -- which is exactly when
    someone should re-read it.
    """
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        bound_url = db.engine.url
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///should-not-be-used.db"
        assert db.engine.url == bound_url


def test_two_apps_do_not_share_a_database():
    """Isolation is structural now, not a promise made by drop_all()."""
    setup_test_environment()
    first = create_app()
    first.config["TESTING"] = True
    second = create_app()
    second.config["TESTING"] = True

    with first.app_context():
        db.session.execute(sa.text("CREATE TABLE leak_probe (x INTEGER)"))
        db.session.commit()
        assert sa.inspect(db.engine).has_table("leak_probe")

    with second.app_context():
        assert not sa.inspect(db.engine).has_table("leak_probe"), (
            "A second app saw the first app's table, so the two share a "
            "database. Every fixture must get its own in-memory engine."
        )

    with first.app_context():
        db.session.execute(sa.text("DROP TABLE leak_probe"))
        db.session.commit()


def test_in_memory_engine_does_not_recycle_its_only_connection():
    """An in-memory database *is* its connection; recycling wipes it.

    Flask-SQLAlchemy pools an in-memory SQLite URL with a StaticPool holding a
    single connection. `pool_recycle` -- correct for the production Postgres
    pool -- would reconnect it once the app outlived the interval, and the new
    connection opens an empty database. Nothing raises; the tables are just
    gone. Only long-lived (module- or session-scoped) app fixtures can reach
    this, which is precisely why it needs a test rather than vigilance.
    """
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    assert "pool_recycle" not in app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    with app.app_context():
        assert isinstance(db.engine.pool, sa.pool.StaticPool)
        assert db.engine.pool._recycle == -1


def test_pool_options_still_apply_to_a_networked_database():
    """The carve-out above must not disarm pooling for production Postgres."""
    assert not _is_in_memory_sqlite("postgresql://user:pw@db:5432/idealista")
    assert not _is_in_memory_sqlite("sqlite:///instance/test.db")
    assert not _is_in_memory_sqlite(None)
    assert _is_in_memory_sqlite("sqlite://")
    assert _is_in_memory_sqlite("sqlite:///:memory:")


def test_no_test_module_reassigns_the_uri_after_create_app():
    """Stop the copied fixture from coming back.

    A reassignment is at best a no-op and at worst a false claim of isolation,
    so the pattern is banned outright rather than checked for placement.
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if URI_REASSIGNMENT.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "SQLALCHEMY_DATABASE_URI is assigned after create_app() has already "
        "bound the engine, so it has no effect. Point DATABASE_URL at the "
        "database instead, before create_app().\n" + "\n".join(offenders)
    )
