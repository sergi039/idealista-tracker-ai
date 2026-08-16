"""The web entry point, and the only thing that starts the scheduler.

gunicorn loads `main:app` (see the Dockerfile CMD); nothing under `utils/`
imports this module, which is exactly why the decision lives here rather than
in `create_app()`. See `app.should_start_scheduler` for what went wrong when it
lived in the factory (issue #333).
"""

import os

from app import create_app, should_start_scheduler

app = create_app()

# Runs after `python -m migrations.runner` has completed, because the Dockerfile
# runs the migrations first and only then execs gunicorn against this module.
if should_start_scheduler(app):
    from services.scheduler_service import init_scheduler

    init_scheduler(app)

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "5001")
    except Exception:
        port = 5001
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("DEV_MODE", "").lower() == "true",
    )
