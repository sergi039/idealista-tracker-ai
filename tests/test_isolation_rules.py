from pathlib import Path


def test_docker_compose_uses_production_port_5001():
    root = Path(__file__).parent.parent
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

    # Post-cutover: Universal is the primary deployment on port 5001.
    assert "5001:5001" in compose
    assert "container_name: idealista-app" in compose
    assert "container_name: idealista-db" in compose
    assert "idealista-network" in compose
    assert "idealista-universal-pgdata" in compose


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
