import re
from pathlib import Path

# `${VAR:-default}` — the only interpolation form docker-compose.yml uses for
# the global Docker names. Rendering the defaults here is how we assert that an
# unset environment still produces exactly the production deployment.
_VAR_WITH_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


def _render_with_defaults(text: str) -> str:
    return _VAR_WITH_DEFAULT.sub(lambda match: match.group(2), text)


def test_docker_compose_defaults_to_production_names_and_port_5001():
    root = Path(__file__).parent.parent
    compose = _render_with_defaults(
        (root / "docker-compose.yml").read_text(encoding="utf-8")
    )

    # Post-cutover: Universal is the primary deployment on port 5001. With no
    # env vars set, the file must still render today's production resources.
    assert "5001:5001" in compose
    assert "container_name: idealista-app" in compose
    assert "container_name: idealista-db" in compose
    assert "idealista-network" in compose
    assert "idealista-universal-pgdata" in compose
    assert "5434:5432" in compose


def test_global_docker_names_are_scoped_by_a_prefix_variable():
    # A hard-coded container_name is claimed process-wide, so a second checkout
    # steals it from production and blocks the deploy watcher (#128). Every
    # global name has to stay derived from COMPOSE_CONTAINER_PREFIX.
    root = Path(__file__).parent.parent
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "container_name: ${COMPOSE_CONTAINER_PREFIX:-idealista}-app" in compose
    assert "container_name: ${COMPOSE_CONTAINER_PREFIX:-idealista}-db" in compose
    assert "name: ${COMPOSE_CONTAINER_PREFIX:-idealista}-network" in compose
    assert "name: ${COMPOSE_CONTAINER_PREFIX:-idealista}-universal-pgdata" in compose

    # Host ports are overridable too, or a worktree cannot start at all; the
    # app bind address is not, because there is no authentication.
    assert '"127.0.0.1:${APP_HOST_PORT:-5001}:5001"' in compose
    assert '"127.0.0.1:${DB_HOST_PORT:-5434}:5432"' in compose

    dev_compose = (root / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert '"${DB_HOST_PORT:-5434}:5432"' in dev_compose


def test_env_example_uses_separate_db_name():
    root = Path(__file__).parent.parent
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    assert "DB_NAME=idealista_universal" in env_example
    assert "IMAP_FOLDER=IdealistaProperties" in env_example
    assert "SESSION_COOKIE_NAME=idealista_universal_session" in env_example


def test_local_dev_entrypoint_defaults_to_5001():
    root = Path(__file__).parent.parent
    main_py = (root / "main.py").read_text(encoding="utf-8")
    assert "5001" in main_py


def test_universal_config_uses_separate_session_cookie_name():
    root = Path(__file__).parent.parent
    config_py = (root / "config.py").read_text(encoding="utf-8")
    assert "SESSION_COOKIE_NAME" in config_py
    assert "idealista_universal_session" in config_py
    assert "IdealistaProperties" in config_py
