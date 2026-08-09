# Dev Rules (Ports & Isolation)

Post-cutover, **this repository is the production deployment**: `docker compose
up` here serves `http://127.0.0.1:5001/`, and `tools/autopilot/deploy_watcher.sh`
rebuilds it from `main` on a timer. Anything else that runs `docker compose up`
from a second checkout — a git worktree, a branch build, the legacy
`IdealistaRank` tree — has to stay out of its way.

## The isolation is configuration, not a rule to remember

Container names, the Docker network and the data volume are **global** Docker
resources. Compose scopes almost everything by project name, but a hard-coded
`container_name` is claimed process-wide, so a second checkout used to take the
name `idealista-app` away from production and the deploy watcher then failed
with `Conflict. The container name "/idealista-app" is already in use`.

`docker-compose.yml` derives every global name from one variable, so you set
three values instead of remembering a naming convention:

| Variable                   | Default      | Scopes                                                       |
| -------------------------- | ------------ | ------------------------------------------------------------ |
| `COMPOSE_CONTAINER_PREFIX` | `idealista`  | `<prefix>-app`, `<prefix>-db`, `<prefix>-network`, `<prefix>-universal-pgdata` |
| `APP_HOST_PORT`            | `5001`       | host port for the app                                         |
| `DB_HOST_PORT`             | `5434`       | host port for postgres (also used by `docker-compose.dev.yml`) |

**The main checkout sets none of them.** Unset, the file renders exactly the
production names and ports it always had, so `deploy_watcher.sh`,
`MIGRATION_RUNBOOK.md` (`docker exec idealista-db …`) and the runbooks keep
working unchanged.

A second checkout puts its own values in its own `.env`:

```bash
# .env in the worktree — never in the main checkout
COMPOSE_CONTAINER_PREFIX=wt1
APP_HOST_PORT=5101
DB_HOST_PORT=5534
```

and then gets `wt1-app` / `wt1-db` on `http://127.0.0.1:5101/`, its own network
and its **own empty database volume** — a branch build never migrates or writes
the production data.

Check what you are about to start without starting it:

```bash
docker compose config | grep -E 'container_name|published'
```

`APP_HOST_PORT` is the **host** side only. The app always listens on 5001 inside
the container (`Dockerfile` CMD, and the dev override's gunicorn `--bind`), so
the container port stays literal. Note that `main.py` reads `APP_PORT` for a
bare `python main.py` run outside Docker; that is a different knob, deliberately
named differently.

## Hard rules

- The app is published on **`127.0.0.1` only** — there is no authentication.
  Change the port through `APP_HOST_PORT`; never widen the bind address, and
  never put the app behind a tunnel or proxy without adding auth first.
- Do not reintroduce a hard-coded `container_name`, network `name` or volume
  `name` in `docker-compose.yml`. `tests/test_isolation_rules.py` fails if the
  variables disappear, and it also fails if their defaults stop rendering
  today's production values.
- Legacy `IdealistaRank` (the pre-cutover project), if you still run it, is the
  other side of this contract: it owns whatever names and ports its own tree
  declares, so give this one a prefix rather than editing that one.

## Data Isolation (recommended)

To avoid mixing email sources when running two builds side by side:

- Different IMAP folders/labels (`IMAP_FOLDER=Idealista` in legacy vs
  `IMAP_FOLDER=IdealistaProperties` here)
- Different DB names in `.env` (`DB_NAME=idealista` vs
  `DB_NAME=idealista_universal`)

## Browser Session Isolation (recommended)

Cookies do **not** isolate by port on `localhost`, so a second build needs a
different cookie name:

- Default here: `SESSION_COOKIE_NAME=idealista_universal_session`
